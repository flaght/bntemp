"""直连08零成交后的单次选约维护；不连接柜台、不报单、不修改持仓或额度。"""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, fields
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
import argparse
import hashlib
import json
import os
import tempfile
import time

from bomber.framework.trader.persistence import JsonStateStore
from bomber.framework.trader.runtime.live.local import strategy_state_lock
from .vega_signal import OptionVegaConfig

DEMO = Path(__file__).resolve().parent
TERMINAL = {'FILLED', 'CANCELED', 'REJECTED', 'EXPIRED'}


def prepare_rearm(account, signal, config, *, strategy_id, trading_day, option_slot, now):
    """仅生成候选；必须同日、零成交、无待执行差额及未消费回报。"""
    day = date.fromisoformat(trading_day)
    stamp = datetime.combine(day, datetime.strptime(option_slot, '%H:%M').time(), ZoneInfo('Asia/Shanghai'))
    if stamp.strftime('%H:%M') != option_slot or now.tzinfo is None:
        raise ValueError('选约时间须为HH:MM，当前时钟须含时区')
    if now.astimezone(ZoneInfo('Asia/Shanghai')).date() != day or now >= stamp:
        raise ValueError('仅能恢复今天尚未到达的选约时点；不会立即补开或自动改到下一天')
    compact_day = day.strftime('%Y%m%d')
    updated, revised, parameters = deepcopy(account), deepcopy(signal), deepcopy(config)
    components = updated['component_states']
    identity = components['session_identity']['identity']
    if identity['trading_day'] != compact_day or set(identity['strategies']) != {strategy_id}:
        raise ValueError('须为本交易日单策略直连08账户检查点')
    previous = identity['strategies'][strategy_id]
    if (previous['kind'] != 'option_vega' or previous['environment'] != 'realtime'
            or previous['trading_day'] != compact_day or previous != revised['identity']):
        raise ValueError('账户与信号的08策略身份或交易日不一致')
    reference = previous['reference']
    if (reference['mode'] != 'simnow' or reference['environment'] != 'realtime'
            or reference['td_day'] != compact_day):
        raise ValueError('维护仅支持实时SimNow直连08')
    parameters['option_slot'] = option_slot
    names = {field.name for field in fields(OptionVegaConfig)}
    effective = json.loads(json.dumps(asdict(OptionVegaConfig(**{
        key: value for key, value in parameters.items() if key in names})), default=str))
    expected = {**reference['config'], 'option_slot': option_slot}
    if effective != expected:
        raise ValueError('除option_slot之外的策略JSON参数必须与原检查点一致')
    for key in ('max_notional', 'limit_offset_ticks'):
        if key in parameters and Decimal(str(parameters[key])) != Decimal(str(reference[key])):
            raise ValueError(f'不能同时修改风险或执行参数: {key}')
    shared = components['shared_account']
    state = shared['shared']
    if state['execution_mode'] != 'independent' or shared.get('pending') or shared.get('failed'):
        raise ValueError('执行不是独立模式或存在未决/失败状态，须先对账')
    if state.get('execution_stop'):
        raise ValueError('执行仍处于停止状态，不能通过重新选约绕过')
    if components['strategy_buckets']['frozen']:
        raise ValueError('仍有冻结委托')
    if any(Decimal(row['quantity']) for row in updated['positions']['working_quantities']):
        raise ValueError('仍有在途委托量')
    algo = components.get('nt_algo', {})
    if algo.get('tasks') or algo.get('stops'):
        raise ValueError('仍有算法任务或停止待办')
    if any(driver['trading_day'] != compact_day or driver['orders']
           for driver in updated['ctp_drivers'].values()):
        raise ValueError('Driver交易日不一致或仍有未结束委托关联')
    records = components['ctp_order_owners']['records']
    owned = [row for row in records.values() if row['strategy_id'] == strategy_id]
    today = [row for row in owned if row['day'] == compact_day]
    if any(row['terminal'] is not True for row in owned):
        raise ValueError('原委托尚未结束')
    if any(row['trades'] for row in today):
        raise ValueError('今天已有成交，不能按零成交方式恢复选约')
    today_ids = {row['client_order_id'] for row in today}
    orders = {row['client_order_id']: row for rows in updated['orders'].values() for row in rows}
    if any(row['status'] not in TERMINAL for row in orders.values()):
        raise ValueError('订单状态机仍有未结束委托')
    if any(key not in orders or Decimal(orders[key]['filled_quantity']) for key in today_ids):
        raise ValueError('今天委托状态缺失或累计成交不为零')
    journal = state['event_journal']
    events = journal['events'][strategy_id]
    cursor = revised['cursor']
    if (type(cursor) is not int or cursor != len(events)
            or journal['ack'][strategy_id] != cursor):
        raise ValueError('仍有未消费回报，须先正常恢复并处理回报')
    for row in events:
        if row['type'] != 'FillEvent':
            continue
        event = row['event']
        event_day = datetime.fromtimestamp(event['ts_event'] / 1e9, ZoneInfo('Asia/Shanghai')).date()
        if event_day == day or event['identity'].get('client_order_id') in today_ids:
            raise ValueError('事件日志记录了今天成交，不能恢复零成交机会')
    source = shared['sources'][strategy_id]
    if revised['revision'] != source['revision']:
        raise ValueError('信号与账户目标版本不一致，须先正常恢复对账')
    business = revised['signal']
    if trading_day not in business['sent_days'] or business['flatten_requested']:
        raise ValueError('今天尚未标记为已选约，或已请求清仓；无需此维护')
    routes = state['routes'][strategy_id]['routes']
    positions = {}
    for row in updated['positions']['attribution']['owned']:
        if row['strategy_id'] == strategy_id:
            key = row['instrument_id']
            positions[key] = positions.get(key, Decimal(0)) + Decimal(row['quantity'])
    goals = {key + '.CFFEX': Decimal(value) for name in ('option_goals', 'future_goals')
             for key, value in business[name].items()}
    source_rows, contributions = {}, {}
    for row in source['rows']:
        key = row['instrument_id']
        source_rows[key] = source_rows.get(key, Decimal(0)) + Decimal(row['quantity'])
    for row in updated['portfolio']['contributions'][strategy_id]:
        key = row['key']['instrument_id']
        contributions[key] = contributions.get(key, Decimal(0)) + Decimal(row['quantity'])
    for targets in (goals, source['targets'], source_rows, contributions):
        keys = set(targets) | set(positions)
        if (not keys <= set(routes.values()) or
                any(Decimal(targets.get(key, 0)) != positions.get(key, 0) for key in keys)):
            raise ValueError('原目标仍有未完成差额；先处理原目标，不能重新选约叠加')
    # 同代且内容一致的维护副本同步同一字段。信号版本已与账户相等，恢复不会
    # 使用旧alignment/bootstrap；这些历史副本保持原样，不猜测或重建其业务状态。
    copies = [business]
    alignment = source.get('metadata', {}).get('checkpoint_alignment')
    if (alignment is not None and alignment['revision'] == revised['revision']
            and alignment['signal'] == signal['signal']):
        copies.append(alignment['signal'])
    bootstrap = state.get('bootstrap_signals', {}).get(strategy_id)
    if bootstrap == signal['signal']:
        copies.append(bootstrap)
    for item in copies:
        item['sent_days'] = [value for value in item['sent_days'] if value != trading_day]
    for item in (reference, revised['identity']['reference']):
        item['config']['option_slot'] = option_slot
    evidence = dict(strategy_id=strategy_id, trading_day=trading_day, option_slot=option_slot,
        previous_slot=signal['identity']['reference']['config']['option_slot'],
        previous_order_count=len(today), previous_order_ids=sorted(today_ids),
        fills_today=0, orders_submitted=0, positions_preserved=True, daily_counts_preserved=True)
    return updated, revised, parameters, evidence


def _atomic_write(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def rearm(*, account_state_file, signal_file, strategy_config, strategy_id,
          trading_day, option_slot, apply=False, now=None):
    paths = [Path(value).resolve() for value in (account_state_file, signal_file, strategy_config)]
    if len(set(paths)) != 3:
        raise ValueError('账户、信号和策略JSON须为不同文件')
    account_path, signal_path, config_path = paths
    receipt = signal_path.with_name(signal_path.name + f'.entry-rearm-{trading_day}.json')
    with ExitStack() as resources:
        for path in (str(account_path) + '.direct-owner', str(account_path) + '.continuous-owner',
                     str(signal_path) + '.worker-owner', *paths):
            resources.enter_context(strategy_state_lock(path))
        if receipt.exists():
            raise ValueError(f'今天已执行或准备过此维护，请核对原审计记录: {receipt}')
        originals = [path.read_bytes() for path in paths]
        account_store, signal_store = JsonStateStore(account_path), JsonStateStore(signal_path)
        account, signal = account_store.load(), signal_store.load()
        candidate, business, config, evidence = prepare_rearm(account.payload, signal.payload,
            json.loads(originals[2]), strategy_id=strategy_id, trading_day=trading_day,
            option_slot=option_slot, now=now or datetime.now(ZoneInfo('Asia/Shanghai')))
        evidence['status'] = 'checked'
        if not apply:
            return evidence
        token = str(time.time_ns())
        backups = [path.with_name(path.name + f'.before-entry-rearm-{token}.bak') for path in paths]
        for path, data in zip(backups, originals):
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        evidence.update(status='prepared', backups=[str(path) for path in backups],
            original_sha256=[hashlib.sha256(data).hexdigest() for data in originals])
        # 先写不可覆盖的审计标记。中断后原配置/身份不匹配会闭闸，不能重复应用。
        with os.fdopen(os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                       'w', encoding='utf-8') as handle:
            json.dump(evidence, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        for path, data in zip(paths, originals):
            if path.read_bytes() != data:
                raise RuntimeError('维护期间文件已变化，拒绝覆盖')
        written = []
        try:
            account_store.save(candidate, expected_generation=account.generation)
            written.append((account_path, originals[0]))
            signal_store.save(business, expected_generation=signal.generation)
            written.append((signal_path, originals[1]))
            if config_path.read_bytes() != originals[2]:
                raise RuntimeError('策略JSON已变化，拒绝覆盖')
            _atomic_write(config_path, (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode())
            written.append((config_path, originals[2]))
            evidence['status'] = 'applied'
            _atomic_write(receipt, (json.dumps(evidence, ensure_ascii=False, indent=2) + '\n').encode())
        except BaseException:
            for path, data in reversed(written):
                _atomic_write(path, data)
            # 保留prepared审计，要求先核对备份，不能无提示重试维护。
            raise
        return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account-state-file', type=Path, default=DEMO/'results/direct/v4/state.json')
    parser.add_argument('--signal-file', type=Path, default=DEMO/'results/direct/v4/signal.json')
    parser.add_argument('--strategy-config', type=Path, default=DEMO/'config/vega_live.json')
    parser.add_argument('--strategy-id', default='option-vega-direct')
    parser.add_argument('--trading-day', required=True, help='YYYY-MM-DD，须为北京时间今天')
    parser.add_argument('--option-slot', required=True, help='HH:MM，须为今天未来时点')
    parser.add_argument('--apply', action='store_true', help='通过全部核验后备份并保存；省略则只读预检')
    options = vars(parser.parse_args(argv))
    print(json.dumps(rearm(**options), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
