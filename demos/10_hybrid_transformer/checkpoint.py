"""Hybrid 批次与目标状态；接入框架 RuntimeStateManager 的同一检查点。

模型窗口由历史服务或完整实时分钟重新积累；这里只恢复持仓义务，不重放历史委托。
"""
from decimal import Decimal

from .holding import PeriodHoldings, completed_minute_ns


class HybridCheckpoint:
    def __init__(self, strategy):
        self.strategy = strategy

    def config(self):
        s, c = self.strategy, self.strategy.config
        return dict(product=c.product, venue=c.venue, signal_id=c.signal_id,
                    hold_bars=c.hold_bars, max_contracts=c.max_contracts,
                    rebalance_minutes=c.rebalance_minutes, contract_multiplier=str(c.contract_multiplier),
                    target_key=c.target_key, target_keys=s.target_keys)

    def snapshot_state(self):
        s = self.strategy
        return dict(version=1, config=self.config(), holdings=s.holdings.snapshot_state(),
                    revision=s._revision, last_processed_ns=s.last_processed_ns,
                    bars_used=s.bars_used, last_main=s.last_main,
                    last_targets={k: str(v) for k, v in s.last_targets.items()},
                    targets_pending=s._targets_pending,
                    order_updates_received=s.order_updates_received, fills_received=s.fills_received)

    def restore_state(self, state):
        s = self.strategy
        if s.is_started:
            raise ValueError("须在策略启动前恢复 Hybrid 检查点")
        if state.get('version') != 1 or state.get('config') != self.config():
            raise ValueError("Hybrid 检查点版本、策略配置或执行路由不一致")
        ledger = PeriodHoldings(s.config.hold_bars, s.config.max_contracts, calendar=s.calendar)
        ledger.restore_state(state['holdings'])
        counters = ('revision', 'bars_used', 'order_updates_received', 'fills_received')
        if any(type(state.get(k)) is not int or state[k] < 0 for k in counters):
            raise ValueError("Hybrid 检查点计数无效")
        stamp, main, pending = state['last_processed_ns'], state['last_main'], state['targets_pending']
        if (type(stamp) is not int or stamp < -1 or type(pending) is not bool
                or (stamp == -1) != (ledger.last_bar_ns == -1)
                or stamp >= 0 and completed_minute_ns(stamp) != ledger.last_bar_ns
                or state['bars_used'] > ledger.bar_index + 1
                or main is not None and (not isinstance(main, str) or not main)):
            raise ValueError("Hybrid 检查点行情位置无效")
        try:
            targets = {key: Decimal(value) for key, value in state['last_targets'].items()}
            if (any(not isinstance(key, str) or not key or not value.is_finite()
                    or value != value.to_integral_value() for key, value in targets.items())
                    or sum((abs(v) for v in targets.values()), Decimal(0)) > s.config.max_contracts
                    or s.target_keys is not None and any(k not in s.target_keys for k in targets)):
                raise ValueError("目标字段无效")
            expected = {}
            for batch in ledger.active:
                expected[batch.instrument] = expected.get(batch.instrument, Decimal(0)) + batch.quantity
            if ({k: v for k, v in targets.items() if v} != {k: v for k, v in expected.items() if v}
                    or bool(targets) != bool(state['revision'])):
                raise ValueError("已发布目标与批次账本不一致")
        except (AttributeError, TypeError, ArithmeticError) as exc:
            raise ValueError("Hybrid 检查点目标格式无效") from exc
        # 校验完成后一次性替换；框架负责同时恢复 TargetStore/账户/订单。
        s.restore_target_revision(state['revision'])
        s.holdings = ledger
        s.last_processed_ns, s.bars_used, s.last_main = stamp, state['bars_used'], main
        s.last_targets, s._targets_pending = targets, pending
        s.last_target = sum(targets.values(), Decimal(0)) if targets else None
        s.last_target_main = main if targets else None
        s.order_updates_received = state['order_updates_received']
        s.fills_received = state['fills_received']
        s.last_prediction = None

    def validate_targets(self, store):
        """恢复时由 RuntimeStateManager 对照统一目标仓位检查点。"""
        s = self.strategy
        target = store.get(s.strategy_id)
        routed = {(s.target_keys[k] if s.target_keys is not None else s.config.target_key): v
                  for k, v in s.last_targets.items()}
        if target is None:
            if s._revision or routed:
                raise ValueError("Hybrid 检查点缺少对应 TargetStore 状态")
        elif target.revision != s._revision or dict(target.targets) != routed:
            raise ValueError("Hybrid 检查点目标/版本与 TargetStore 不一致")
