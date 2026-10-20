"""从账户目标与有效仓位生成中立订单意图。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from bomber.framework.trader.contracts import ExecutionRequest
from bomber.framework.trader.execution.contracts import OrderIntent, OrderSide, PositionEffect
from bomber.framework.trader.portfolio import PositionManager


@dataclass(frozen=True)
class NetTargetOrderPlanner:
    """通用净持仓目标规划器。

    跨越零轴时拆成“先平、后开”两张订单，避免一张reduce-only订单既平仓又反向
    开仓。CTP今昨仓需要更细粒度的Planner，不能使用本类替代。
    """

    position_manager: PositionManager
    planner_id: str = "net-target"

    def plan(self, request: ExecutionRequest) -> Sequence[OrderIntent]:
        orders: list[OrderIntent] = []
        for instrument_id, target in request.targets.items():
            current = self.position_manager.effective_position(
                request.client_id,
                instrument_id,
            )
            target = Decimal(target)
            if target == current:
                continue
            metadata = {
                **dict(request.metadata),
                "target": str(target),
                "current": str(current),
                "request_revision": request.revision,
                "request_ts": request.ts_event,
            }
            if current > 0 and target < current:
                close_quantity = min(current, current - target)
                orders.append(
                    self._order(
                        request,
                        instrument_id,
                        OrderSide.SELL,
                        close_quantity,
                        PositionEffect.CLOSE,
                        True,
                        metadata,
                    ),
                )
                remainder = target - (current - close_quantity)
                if remainder < 0:
                    orders.append(
                        self._order(
                            request,
                            instrument_id,
                            OrderSide.SELL,
                            abs(remainder),
                            PositionEffect.OPEN,
                            False,
                            metadata,
                        ),
                    )
                continue
            if current < 0 and target > current:
                close_quantity = min(abs(current), target - current)
                orders.append(
                    self._order(
                        request,
                        instrument_id,
                        OrderSide.BUY,
                        close_quantity,
                        PositionEffect.CLOSE,
                        True,
                        metadata,
                    ),
                )
                remainder = target - (current + close_quantity)
                if remainder > 0:
                    orders.append(
                        self._order(
                            request,
                            instrument_id,
                            OrderSide.BUY,
                            remainder,
                            PositionEffect.OPEN,
                            False,
                            metadata,
                        ),
                    )
                continue

            delta = target - current
            orders.append(
                self._order(
                    request,
                    instrument_id,
                    OrderSide.BUY if delta > 0 else OrderSide.SELL,
                    abs(delta),
                    PositionEffect.OPEN,
                    False,
                    metadata,
                ),
            )
        return tuple(orders)

    @staticmethod
    def _order(
        request: ExecutionRequest,
        instrument_id,
        side: OrderSide,
        quantity: Decimal,
        effect: PositionEffect,
        reduce_only: bool,
        metadata: dict,
    ) -> OrderIntent:
        return OrderIntent(
            strategy_id=request.strategy_id,
            backend_id=request.client_id,
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            position_effect=effect,
            reduce_only=reduce_only,
            metadata=metadata,
        )


class LimitTargetOrderPlanner:
    """通用净目标的保护限价；先确认平仓，再由下一目标推进反向开仓。"""
    planner_id = "limit-net-target"

    def __init__(self, positions, prices, ticks, offset_ticks=1):
        self.positions, self.prices = positions, prices
        self.planner = NetTargetOrderPlanner(positions)
        self.ticks = {i: Decimal(str(v)) for i, v in ticks.items()}
        if not self.ticks or any(not v.is_finite() or v <= 0 for v in self.ticks.values()):
            raise ValueError("价格步长须为正且有限")
        if type(offset_ticks) is not int or offset_ticks < 0:
            raise ValueError("限价偏移须为非负整数")
        self.offset_ticks = offset_ticks

    def plan(self, request):
        from dataclasses import replace
        from decimal import ROUND_CEILING, ROUND_FLOOR
        from .contracts import OrderType
        planned = self.planner.plan(request)
        closing = {o.instrument_id for o in planned if o.position_effect is not PositionEffect.OPEN}
        result = []
        for order in planned:
            if self.positions.working_quantity(request.client_id, order.instrument_id):
                continue
            if order.position_effect is PositionEffect.OPEN and order.instrument_id in closing:
                continue
            tick = self.ticks[order.instrument_id]
            ref = self.prices.get(order.instrument_id)
            if ref is None or not ref.price.is_finite():
                raise ValueError("执行缺少有效参考价")
            raw = ref.price + tick * self.offset_ticks * (1 if order.side is OrderSide.BUY else -1)
            price = (raw / tick).to_integral_value(rounding=ROUND_CEILING if order.side is OrderSide.BUY else ROUND_FLOOR) * tick
            if price <= 0:
                raise ValueError("保护限价须为正数")
            result.append(replace(order, order_type=OrderType.LIMIT, price=price))
        return tuple(result)
