"""已终结订单的跨日续接：保留归属，按当日权威今昨仓及成本更新执行账本。"""

from decimal import Decimal

from bomber.framework.trader import persistence as storage
from bomber.framework.trader.execution.attribution import PositionAttributionLedger
from bomber.framework.trader.portfolio import PositionManager
from ...ctp_account import position_buckets
from .ctp import occupied_positions, assert_account_ready

BUCKETS = ("long_today", "long_yesterday", "short_today", "short_yesterday")


def observe_rollover(payload, *, driver, transport, day, multipliers, scope=None,
                     preserve_external=False, generic_close_venues=()):
    """禁单阶段观察；不移动日期猜分桶，不从柜台仓位推断策略所有者。"""
    client_id = driver.driver_id
    if set(payload["ctp_ledgers"]) != {client_id} or set(payload["ctp_drivers"]) != {client_id}:
        raise ValueError("跨日续接只允许同一CTP账户执行客户端")
    ledger = storage._decode_ctp_ledger(payload["ctp_ledgers"][client_id])
    checkpoint = storage._decode_ctp_driver(payload["ctp_drivers"][client_id])
    if not ledger.trading_day < day or checkpoint.trading_day != ledger.trading_day or checkpoint.orders:
        raise ValueError("跨日须向前更新且原Driver无活动订单")
    if (checkpoint.account_id != driver.account_id or checkpoint.broker_id != transport.broker_id
            or checkpoint.investor_id != transport.investor_id or checkpoint.driver_id != client_id):
        raise ValueError("跨日恢复账户身份与原归属账本不一致")
    orders = {key: storage._decode_orders(rows) for key, rows in payload["orders"].items()}
    if set(orders) != {client_id} or any(not row.state.status.is_terminal for rows in orders.values() for row in rows):
        raise ValueError("跨日恢复要求原订单已结束；未决发送不能自动续接")
    positions = storage._decode_positions(payload["positions"])
    PositionAttributionLedger.validate_checkpoints(positions.attribution, orders)
    if any(positions.snapshot.working_quantities.values()):
        raise ValueError("跨日恢复存在未完成委托量")
    manager = PositionManager(); manager.restore(positions)
    expected = {str(i): (row.long_total, row.short_total) for i, row in ledger.positions.items()
                if row.long_total or row.short_total}
    if any((row.long_total or row.short_total) and instrument not in multipliers
           for instrument, row in ledger.positions.items()):
        raise ValueError("跨日恢复缺少原持仓合约的当日路由和乘数")
    for instrument, row in ledger.positions.items():
        if (manager.account_position(client_id, instrument) != row.net_position
                or manager.unassigned_position(client_id, instrument)):
            raise ValueError("原成交归属与执行账本不一致")
    driver.start(lambda report: None)
    try:
        if driver.trading_day != day:
            raise RuntimeError("跨日恢复TD日与请求日不一致")
        assert_account_ready(driver)
        gross = occupied_positions(transport)
        managed = scope.select(gross) if scope is not None else gross
        if not preserve_external and managed != expected:
            raise RuntimeError("跨日恢复柜台总仓与原成交账本不一致")
        rows = transport.query_position_details()
        if preserve_external:
            from .ctp_owned import external_partition, validate_saved_ownership
            validate_saved_ownership(payload, client_id)
            # 只结转系统自己的成交成本，不用包含外部仓位的柜台均价覆盖归属成本。
            encoded = storage._encode_ctp_ledger(ledger)
            encoded["trading_day"] = day
            for row in encoded["positions"]:
                for side in ("long", "short"):
                    today, yesterday = (Decimal(row[side+suffix]) for suffix in ("_today", "_yesterday"))
                    total = today+yesterday
                    basis = ((today*Decimal(row[side+"_today_basis"])+yesterday*Decimal(row[side+"_yesterday_basis"]))
                             / total if total else Decimal(0))
                    row.update({side+"_today": "0", side+"_today_basis": "0",
                                side+"_yesterday": str(total), side+"_yesterday_basis": str(basis)})
            rolled = storage._decode_ctp_ledger(encoded)
            all_buckets = position_buckets(rows, gross)
            external_partition(rolled, gross, {key: row.quantities for key, row in all_buckets.items()},
                               generic_close_venues)
            assert_account_ready(driver)
            if occupied_positions(transport) != gross:
                raise RuntimeError("跨日归属查询期间柜台持仓变化")
            payload["ctp_ledgers"][client_id] = encoded
            return {"from_day": ledger.trading_day, "to_day": day, "gross": gross,
                "owned_cost_basis": "persisted_strategy_fills_not_account_average",
                "bucket_basis": "owned_logical_rollover_with_authoritative_capacity_check"}
        allowed = {str(i) for i in multipliers}
        managed_rows = [row for row in rows if f"{row['InstrumentID']}.{row['ExchangeID']}" in allowed]
        buckets = position_buckets(managed_rows, multipliers, require_cost=True)
        if {key: (r.quantities[0] + r.quantities[1], r.quantities[2] + r.quantities[3])
                for key, r in buckets.items()} != managed:
            raise RuntimeError("跨日恢复权威总仓与今昨仓明细不一致")
        ledger_rows = []
        for instrument, bucket in sorted(buckets.items()):
            multiplier = Decimal(str(multipliers[next(i for i in multipliers if str(i) == instrument)]))
            if not multiplier.is_finite() or multiplier <= 0:
                raise ValueError("跨日持仓乘数无效")
            record = {"instrument_id": instrument}
            for name, quantity, cost in zip(BUCKETS, bucket.quantities, bucket.costs):
                record[name] = str(quantity)
                record[name + "_basis"] = str(cost / (quantity * multiplier) if quantity else Decimal(0))
            ledger_rows.append(record)
        # 查询再次核对，无活动单/资金/总仓变化才能暂存恢复。
        assert_account_ready(driver)
        if occupied_positions(transport) != gross:
            raise RuntimeError("跨日恢复查询期间柜台持仓发生变化")
        payload["ctp_ledgers"][client_id] = {"trading_day": day, "positions": ledger_rows}
        return {"from_day": ledger.trading_day, "to_day": day, "gross": gross,
                "authoritative_buckets": {key: [str(v) for v in r.quantities] for key, r in buckets.items()}}
    finally:
        driver.stop()
