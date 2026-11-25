"""期权策略空仓维护：对齐组合目标，保留选约日期和已执行对冲时点。"""
import argparse
from copy import deepcopy
from pathlib import Path
from datetime import date, time
from decimal import Decimal

from bomber.framework.trader.runtime.live.channels.ctp_maintenance import (
    align_flat_execution, reconcile_flat_checkpoint, reconcile_shared_flat)


def flat_signal(state, instruments):
    if state.get('version') != 1 or state.get('logic_version', 'strategy1-noddb-v1') != 'strategy1-noddb-v1':
        raise ValueError('不支持的期权业务检查点')
    symbols = {i.rsplit('.', 1)[0] for i in instruments}
    if set(state['option_goals']) | set(state['future_goals']) != symbols:
        raise ValueError('期权及对冲目标与原路由不一致')
    for name in ('submissions', 'entry_decisions'):
        if type(state[name]) is not int or state[name] < 0:
            raise ValueError('原期权业务计数无效')
    if type(state['flatten_requested']) is not bool:
        raise ValueError('原清仓标记无效')
    for day in state['sent_days']:
        date.fromisoformat(day)
    for token in state.get('hedge_sent', ()):
        day, slot = token.split('_', 1)
        date.fromisoformat(day)
        if time.fromisoformat(slot).strftime('%H:%M') != slot:
            raise ValueError('原对冲时点无效')
    for name in ('option_goals', 'future_goals'):
        for value in state[name].values():
            quantity = Decimal(value)
            if not quantity.is_finite() or quantity != quantity.to_integral_value():
                raise ValueError('原期权组合目标无效')
    result = deepcopy(state)
    for key in ('option_goals', 'future_goals'):
        result[key] = {symbol:'0' for symbol in state[key]}
    # sent_days、hedge_sent、flatten_requested和计数保持原值；不重放已处理的选约/对冲。
    return result


def aligned_payload(payload, *, trading_day, archive_unknown_close=False):
    identity = payload['component_states']['session_identity']['identity']
    if identity['mode'] != 'simnow' or identity['environment'] != 'realtime':
        raise ValueError('期权维护只支持原实时SimNow检查点')
    sid = identity['strategy_id']
    state = payload['component_states']['vega_signal']
    instruments = {symbol + '.CFFEX' for name in ('option_goals', 'future_goals') for symbol in state[name]}
    result, evidence = align_flat_execution(payload, strategy_id=sid, instruments=instruments,
        archive_unknown_close=archive_unknown_close)
    driver = next(iter(payload['ctp_drivers'].values()))
    if identity['td_day'] != driver['trading_day']:
        raise ValueError('原会话与Driver交易日不一致')
    if (not archive_unknown_close and trading_day != driver['trading_day']
            or archive_unknown_close and trading_day <= driver['trading_day']):
        raise ValueError('普通对齐须同日，旧未决平仓单归档须后续交易日')
    result['component_states']['vega_signal'] = flat_signal(state, instruments)
    evidence.update(previous_signal=state)
    return result, evidence


def align_shared_signal(signal, revision, routes, source, *, signal_revision=None):
    if signal_revision is not None and signal_revision != source['revision']:
        raise ValueError('期权业务文件落后且缺少账户同代副本，不能猜测已执行选约或对冲时点')
    return flat_signal(signal, routes.values()), {}


def reconcile(*, state_file, transport, archive_unknown_close=False):
    return reconcile_flat_checkpoint(state_file=state_file, transport=transport,
        prepare=lambda payload, day: aligned_payload(payload, trading_day=day,
            archive_unknown_close=archive_unknown_close), archive_unknown_close=archive_unknown_close)


def reconcile_shared(config, *, strategy_id, transport, archive_unknown_close=False):
    if not any(r['strategy_id'] == strategy_id and r['kind'] == 'option_vega' for r in config['strategies']):
        raise ValueError('须指定option_vega策略')
    return reconcile_shared_flat(config, strategy_id=strategy_id, transport=transport,
        align_signal=align_shared_signal, archive_unknown_close=archive_unknown_close)


def main(argv=None):
    from dotenv import load_dotenv
    from bomber.framework.trader.execution.ctp import CtpTdApiTransport
    from bomber.framework.trader.runtime.live.channels.ctp import build_simnow_transport
    load_dotenv(Path(__file__).resolve().parents[2]/'.env')
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--state-file', type=Path)
    group.add_argument('--config', type=Path, help='共享账户配置')
    parser.add_argument('--strategy-id', help='共享账户中要对齐的策略')
    parser.add_argument('--confirm-manual-flat', action='store_true')
    parser.add_argument('--archive-unknown-close', action='store_true', help='显式归档后续交易日无法重建的旧平仓单')
    args = parser.parse_args(argv)
    if not args.confirm_manual_flat or bool(args.config) != bool(args.strategy_id):
        parser.error('须confirm-manual-flat；config与strategy-id一起提供')
    if args.config:
        from scripts.integration.shared_account.config import read_config
        config = read_config(args.config)
        client = config['account']['client_id']
    else:
        from bomber.framework.trader.persistence import JsonStateStore
        saved = JsonStateStore(args.state_file).load()
        if saved is None:
            raise FileNotFoundError(args.state_file)
        client = next(iter(saved.payload['ctp_drivers']))
    transport = build_simnow_transport(CtpTdApiTransport, client_id=client,
        flow_path='/tmp/bomber-option-flat', timeout_seconds=30)
    if args.config:
        return reconcile_shared(config, strategy_id=args.strategy_id, transport=transport,
            archive_unknown_close=args.archive_unknown_close)
    return reconcile(state_file=args.state_file, transport=transport, archive_unknown_close=args.archive_unknown_close)


if __name__ == '__main__':
    main()
