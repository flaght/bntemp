"""策略成交账本与物理账户的分解；候选路由不表示仓位所有权。"""
from decimal import Decimal
import logging

from bomber.framework.market.basic.base import InstrumentId
from bomber.framework.trader import persistence as storage
from bomber.framework.trader.portfolio import PositionManager
from bomber.framework.trader.execution.attribution import PositionAttributionLedger

BUCKETS = ("long_today", "long_yesterday", "short_today", "short_yesterday")


def external_partition(ledger, gross, buckets, generic_close_venues):
    """从柜台扣除已知成交份额。通用平仓今昨桶可互换，归属数量和成本不变。"""
    remaining = {key: tuple(values) for key, values in buckets.items()}
    for instrument, row in ledger.positions.items():
        key = str(instrument)
        actual = list(remaining.get(key, (Decimal(0),) * 4))
        wanted = [getattr(row, name) for name in BUCKETS]
        if key.rsplit(".", 1)[-1] in generic_close_venues:
            for offset in (0, 2):
                total = wanted[offset] + wanted[offset+1]
                # 通用CLOSE不能指定柜台的某一物理今昨桶；仅分配数量，不借用外部成本。
                today = min(wanted[offset], actual[offset])
                yesterday = min(total-today, actual[offset+1])
                today = total-yesterday
                wanted[offset:offset+2] = [today, yesterday]
        for n, quantity in enumerate(wanted):
            actual[n] -= quantity
        if any(q < 0 for q in actual):
            raise RuntimeError(f"柜台仓位不足以覆盖已归属成交: {key}")
        if any(actual):
            remaining[key] = tuple(actual)
        else:
            remaining.pop(key, None)
    external_gross = {key: (q[0]+q[1], q[2]+q[3]) for key, q in remaining.items() if any(q)}
    # 同时验证原始权威总仓与明细，防止按净仓掩盖多空双向变化。
    if {key: (q[0]+q[1], q[2]+q[3]) for key, q in buckets.items() if any(q)} != gross:
        raise RuntimeError("柜台总仓与今昨明细不一致")
    return external_gross, remaining


def validate_saved_ownership(payload, client_id):
    orders = {key: storage._decode_orders(rows) for key, rows in payload["orders"].items()}
    if set(orders) != {client_id} or any(not row.state.status.is_terminal
            for rows in orders.values() for row in rows):
        raise ValueError("归属恢复要求历史订单终结；不能把漏回报解释为外部仓位")
    positions = storage._decode_positions(payload["positions"])
    PositionAttributionLedger.validate_checkpoints(positions.attribution, orders)
    if any(positions.snapshot.working_quantities.values()):
        raise ValueError("归属恢复存在未完成委托量")
    manager = PositionManager(); manager.restore(positions)
    ledger = storage._decode_ctp_ledger(payload["ctp_ledgers"][client_id])
    owned = positions.attribution.get("owned", [])
    keys = set(ledger.positions) | {InstrumentId.from_str(r["instrument_id"]) for r in owned}
    for instrument in keys:
        row = ledger.positions.get(instrument)
        quantities = [Decimal(r["quantity"]) for r in owned
                      if r["client_id"] == client_id and r["instrument_id"] == str(instrument)]
        long = sum((max(q, Decimal(0)) for q in quantities), Decimal(0))
        short = sum((max(-q, Decimal(0)) for q in quantities), Decimal(0))
        if (long, short) != ((row.long_total, row.short_total) if row else (0, 0)):
            raise ValueError("策略成交归属多空总仓与CTP账本不一致")
        if manager.account_position(client_id, instrument) != long-short:
            raise ValueError("策略成交归属与执行净仓不一致")
    return ledger


def restore_external_baseline(account, payload, *, refresh=False):
    """旧scope检查点迁入归属账本；新检查点同日不隐式接受未知仓位变化。"""
    external = payload["component_states"].get("external_positions")
    if external is None:
        raise ValueError("归属恢复缺少外部基线检查点")
    if external["version"] == 2 and not refresh:
        return payload
    ledger = validate_saved_ownership(payload, account.service.client_id)
    from .ctp import occupied_positions, assert_account_ready
    from ...ctp_account import position_buckets
    account.driver.start(lambda report: None)
    try:
        if account.driver.trading_day != account.expected_day:
            raise RuntimeError("归属恢复柜台交易日不一致")
        assert_account_ready(account.driver)
        gross = occupied_positions(account.transport)
        rows = position_buckets(account.transport.query_position_details(), gross)
        buckets = {key: row.quantities for key, row in rows.items()}
        remaining, residual = external_partition(ledger, gross, buckets, account.generic_close_venues)
        if occupied_positions(account.transport) != gross:
            raise RuntimeError("归属恢复查询期间柜台持仓变化")
        assert_account_ready(account.driver)
    finally:
        account.driver.stop()
    old = external["gross"]
    external.update(version=2, gross={key: [str(q) for q in values] for key, values in remaining.items()},
        buckets={key: [str(q) for q in values] for key, values in residual.items()})
    # 原strict模式的范围外账户净仓是观测值；observe模式只保留系统成交净仓。
    payload["positions"]["account_positions"] = [
        {"key": {"client_id": account.service.client_id, "instrument_id": str(i)},
         "quantity": str(row.net_position)} for i, row in ledger.positions.items()]
    if account.rollover_audit is None:
        account.rollover_audit = {"from_day": ledger.trading_day, "to_day": account.expected_day}
    account.rollover_audit["ownership_baseline"] = {"previous_external_gross": old,
        "external_gross": external["gross"], "ownership_source": "persisted_attributed_fills",
        "physical_buckets": "generic_close_is_fungible"}
    logging.getLogger("SharedAccountOwnership").info(
        "成交归属与外部基线已核验，待恢复最终对账提交: system_gross=%s external_gross=%s",
        {str(i): (str(row.long_total), str(row.short_total)) for i, row in ledger.positions.items()
         if row.long_total or row.short_total}, external["gross"])
    return payload
