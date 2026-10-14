"""独立订单按来源归属检查平仓；账户资金与总归属预算仍由原服务检查。"""

from decimal import Decimal

from ..risk import PreTradeRiskManager


class IndependentCtpRiskManager:
    def __init__(self, risk, positions):
        self.risk, self.positions = risk, positions

    def __getattr__(self, name):
        return getattr(self.risk, name)

    def check(self, orders, *, now_ns, mode_override=None):
        owners = {order.strategy_id for order in orders}
        if not orders:
            return self.risk.check(orders, now_ns=now_ns, mode_override=mode_override)
        if len(owners) != 1:
            raise ValueError("独立订单风控批次只能包含一个策略")
        sid = next(iter(owners))
        positions = self.positions
        class OwnedPositions:
            def effective_position(self, client_id, instrument):
                return sum((Decimal(row["quantity"]) for row in positions.attribution.state()["owned"]
                    if row["client_id"] == client_id and row["strategy_id"] == sid
                    and row["instrument_id"] == str(instrument)), Decimal(0))
        scoped = PreTradeRiskManager(self.risk.client_id, OwnedPositions(), self.risk.price_store,
            default_limits=self.risk.default_limits, instrument_limits=self.risk.instrument_limits)
        with self.risk._lock:
            scoped.set_mode(self.risk.mode)
            scoped.check(orders, now_ns=now_ns, mode_override=mode_override)
