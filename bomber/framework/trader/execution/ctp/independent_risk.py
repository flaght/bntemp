"""独立订单按来源归属检查平仓；账户资金与总归属预算仍由原服务检查。"""

from decimal import Decimal

from ..risk import PreTradeRiskManager


class IndependentCtpRiskManager:
    def __init__(self, risk, positions, *, strategy_limits=None):
        self.risk, self.positions = risk, positions
        self.strategy_limits = strategy_limits

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
            if self.strategy_limits is not None:
                # 共享合约的策略可以有不同手数/金额限制，逐笔发送也按来源复核。
                # 原账户公共闸门仍先检查；账户聚合预算关闭不等于策略风控关闭。
                limits = self.strategy_limits(sid)
                if any(order.instrument_id not in limits for order in orders):
                    raise ValueError("独立订单超出来源策略的风险声明")
                owner = PreTradeRiskManager(self.risk.client_id, OwnedPositions(), self.risk.price_store,
                    instrument_limits=limits)
                owner.set_mode(self.risk.mode)
                owner.check(orders, now_ns=now_ns, mode_override=mode_override)
