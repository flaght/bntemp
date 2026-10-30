"""SimNow手工平仓后的账本对齐及5手/每批1手迁移；不发送订单。"""
from __future__ import annotations

import argparse
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time

from bomber.framework.trader.execution.attribution import PositionAttributionLedger
from bomber.framework.trader.persistence import (
    JsonStateStore, _decode_orders, _encode_positions, _encode_portfolio)
from bomber.framework.trader.portfolio import PositionManager, PortfolioCoordinator
from bomber.framework.trader.runtime.live.channels.ctp import account_lock
from bomber.framework.trader.runtime.live.local import strategy_state_lock

from . import run_live as live


def aligned_payload(payload, *, trading_day):
    """建立观察到空仓后的新运行基线；完整订单/归属计划历史仍保留用于去重及当日额度。"""
    result = deepcopy(payload)
    client = live.CLIENT_ID
    if any(set(payload[key]) != {client} for key in ('orders', 'ctp_ledgers', 'ctp_drivers')):
        raise ValueError('手工平仓对齐仅支持当前单策略、单CTP账户检查点')
    hybrid = result['component_states']['hybrid']
    config, holdings = hybrid['config'], hybrid['holdings']
    if (hybrid['version'] != 1 or config['product'] != 'RB' or config['venue'] != 'SHFE'
            or config['target_key'] != 'rb_main' or config['target_keys'] is not None
            or config['hold_bars'] != 5 or holdings['holding_bars'] != 5
            or (config['max_contracts'], config.get('max_entry_contracts')) not in ((10, None), (5, None), (5, 1))
            or holdings['max_contracts'] != config['max_contracts']
            or holdings.get('max_entry_contracts') != config.get('max_entry_contracts')):
        raise ValueError('不是预期的RB单合约五分钟检查点，不能执行手工平仓对齐')
    components = result['component_states']
    old_day = components['live_session']['identity']['trading_day']
    if old_day != trading_day or any(result[key][client]['trading_day'] != trading_day
            for key in ('ctp_ledgers', 'ctp_drivers')):
        raise ValueError('此维护操作须在原检查点同一交易日执行，不能猜测跨日账户状态')
    machines = {key: _decode_orders(rows) for key, rows in payload['orders'].items()}
    if any(not item.state.status.is_terminal for rows in machines.values() for item in rows):
        raise ValueError('旧检查点仍有未结束订单，先核对订单后再对齐')
    PositionAttributionLedger.validate_checkpoints(payload['positions']['attribution'], machines)
    if payload['ctp_drivers'][client]['orders']:
        raise ValueError('旧检查点仍有CTP在途订单关联，不能丢弃')
    if hybrid['targets_pending'] or any(Decimal(row['quantity']) != 0
            for row in payload['positions']['working_quantities']):
        raise ValueError('旧检查点有待提交目标或工作量，不能直接对齐')
    positions = payload['positions']
    if (any(row['strategy_id'] != client for row in positions['strategy_positions'])
            or any(row['key']['client_id'] != client for key in ('account_positions','working_quantities')
                   for row in positions[key])
            or any(row['strategy_id'] != client or row['client_id'] != client
                   for row in positions['attribution']['owned'])
            or any(row['strategy_id'] != client for row in payload['targets'])
            or set(payload['portfolio']['contributions']) - {client}
            or set(payload['portfolio']['strategy_revisions']) - {client}):
        raise ValueError('检查点存在其他策略归属，不能清理其状态')
    # 这里是明确的“外部手工平仓已核实”基线调整，不向订单历史补造任何成交。
    # 旧持仓/批次/成本等原始事实在提交前的完整备份及审计文件中保留。
    result['positions'] = _encode_positions(PositionManager().state())
    result['positions']['attribution']['orders'] = deepcopy(positions['attribution']['orders'])
    result['positions']['attribution']['transfers'] = deepcopy(positions['attribution']['transfers'])
    result['portfolio'] = _encode_portfolio(PortfolioCoordinator().state())
    result['targets'] = []
    result['ctp_ledgers'][client]['positions'] = []
    config.update(max_contracts=5, max_entry_contracts=1)
    holdings.update(max_contracts=5, max_entry_contracts=1,
                    bar_index=-1, last_bar_ns=-1, last_check_ns=-1, active=[])
    hybrid.update(revision=0, last_processed_ns=-1, bars_used=0, last_main=None, last_targets={},
                  targets_pending=False, order_updates_received=0, fills_received=0)
    return result


def _write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def reconcile(*, state_file, transport):
    """持锁、只读核对柜台空仓、备份，然后原子提交；任何非零总仓或活动订单均拒绝。"""
    path = Path(state_file).resolve()
    with strategy_state_lock(str(path) + '.continuous-owner'), strategy_state_lock(path), account_lock(transport.account_id):
        store = JsonStateStore(path)
        saved = store.load()
        if saved is None:
            raise FileNotFoundError(path)
        driver = saved.payload['ctp_drivers'][live.CLIENT_ID]
        if (driver['account_id'] != transport.account_id or driver['broker_id'] != transport.broker_id
                or driver['investor_id'] != transport.investor_id or transport.broker_id != '9999'):
            raise ValueError('本次只读连接账户与旧检查点不一致')
        try:
            context = transport.connect(lambda _: None, lambda _: None, lambda _: None)
            gross = {str(key): tuple(value) for key, value in transport.query_gross_positions().items()
                     if any(value)}
            if gross:
                raise RuntimeError(f'柜台仍有多空总仓，不执行对齐: {gross}')
            if transport.query_active_orders().orders:
                raise RuntimeError('柜台仍有活动订单，不执行对齐')
            payload = aligned_payload(saved.payload, trading_day=context.trading_day)
            if payload == saved.payload:
                print('柜台已核实空仓；本地账本已对齐为5手/每批1手，无需重复迁移')
                return saved
            original = path.read_bytes()
            suffix = f'g{saved.generation}-{time.time_ns()}'
            backup = path.with_name(path.name + '.before-manual-flat-' + suffix + '.bak')
            audit = path.with_name(path.name + '.manual-flat-' + suffix + '.json')
            _write_new(backup, original)
            old = saved.payload['component_states']['hybrid']
            evidence = dict(operation='operator_manual_close_then_verified_broker_flat',
                observed_at_ns=time.time_ns(), trading_day=context.trading_day,
                previous_generation=saved.generation, original_sha256=hashlib.sha256(original).hexdigest(),
                backup=str(backup), previous_targets=old['last_targets'], previous_batches=old['holdings']['active'],
                broker_gross={}, active_orders=0, retained_order_records=sum(map(len, payload['orders'].values())),
                actual_close_prices=None, actual_close_times=None, realized_pnl_reconstructed=False,
                new_max_contracts=5, new_max_entry_contracts=1)
            _write_new(audit, (json.dumps(evidence, ensure_ascii=False, indent=2) + '\n').encode())
            updated = store.save(payload, expected_generation=saved.generation)
            print(f'柜台空仓及无活动订单已核实，TD={context.trading_day}')
            print(f'原检查点备份: {backup}\n外部手工平仓审计: {audit}')
            print(f'账本对齐及5手/每批1手迁移完成，generation={updated.generation}；未发送订单')
            return updated
        finally:
            transport.close()


def main(argv=None):
    live.load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-file', type=Path, required=True)
    parser.add_argument('--confirm-manual-flat', action='store_true')
    args = parser.parse_args(argv)
    if not args.confirm_manual_flat:
        parser.error('须 --confirm-manual-flat，明确已在柜台手工平仓')
    transport = live.build_simnow_transport(live.CtpTdApiTransport, client_id=live.CLIENT_ID,
        flow_path='/tmp/bomber-hybrid-manual-flat', timeout_seconds=15)
    reconcile(state_file=args.state_file, transport=transport)


if __name__ == '__main__':
    main()
