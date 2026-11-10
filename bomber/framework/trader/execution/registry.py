"""账户持久化的纯执行登记：合约、风险及市场时段，不读取策略目录。"""
from copy import deepcopy
from decimal import Decimal
import re

from .risk import RiskLimits
from .shared import SharedRiskBudget


def product_key(instrument):
    symbol, venue = str(instrument).rsplit('.', 1)
    match = re.match(r'[A-Za-z]+', symbol)
    if match is None:
        raise ValueError('合约缺少产品代码')
    return match.group().upper()+'.'+venue


def same_spec(left, right):
    return set(left) == set(right) == {'tick','multiplier'} and all(
        Decimal(str(left[key])) == Decimal(str(right[key])) for key in left)


def limits_from_wire(row):
    values = dict(row)
    if values.get('max_market_age_ns') is not None:
        values['max_market_age_ns'] = int(values['max_market_age_ns'])
    result = RiskLimits(**values)
    for value in vars(result).values():
        if value is not None and not Decimal(str(value)).is_finite():
            raise ValueError('执行限额必须有限')
    return result


def validate_registry(state):
    if not isinstance(state, dict) or set(state) != {'version','instruments','ports','sessions'} or state['version'] != 1:
        raise ValueError('账户缺少有效的纯执行登记，须先执行独立迁移')
    if not state['instruments'] or not state['ports'] or not state['sessions']:
        raise ValueError('执行登记须包含合约、端口及市场时段')
    for instrument, spec in state['instruments'].items():
        product_key(instrument)
        if set(spec) != {'tick','multiplier'}:
            raise ValueError('合约执行条款只能包含tick及multiplier')
        if any(not Decimal(str(v)).is_finite() or Decimal(str(v)) <= 0 for v in spec.values()):
            raise ValueError('合约价格步长和乘数须为有限正数')
    for sid, port in state['ports'].items():
        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', sid):
            raise ValueError('执行登记策略ID无效')
        if set(port) != {'routes','limits','budget'} or not port['routes'] or len(set(port['routes'].values())) != len(port['routes']):
            raise ValueError('执行登记端口格式无效')
        if set(port['limits']) != set(port['routes'].values()) or not set(port['limits']) <= state['instruments'].keys():
            raise ValueError('执行登记路由、合约及风控不一致')
        budget = SharedRiskBudget(**port['budget'])
        if budget.is_unlimited:
            raise ValueError('策略登记预算必须有限')
        for instrument, row in port['limits'].items():
            limit = limits_from_wire(row)
            if limit.contract_multiplier != Decimal(state['instruments'][instrument]['multiplier']):
                raise ValueError('登记风控与合约乘数不一致')
    for session in state['sessions']:
        from bomber.framework.calendars.state import calendar_from_state
        calendar_from_state(session)
    return deepcopy(state)


class ExecutionRegistry:
    def __init__(self, state):
        self.state = validate_registry(state)

    def snapshot_state(self):
        return deepcopy(self.state)

    def restore_state(self, state):
        if validate_registry(state) != self.state:
            raise ValueError('账户执行登记恢复不一致')

    def validate_change(self, sid, configuration, specifications):
        """已登记策略仅能在原产品及限额内更新空仓路由，不新增策略身份。"""
        if sid not in self.state['ports']:
            raise ValueError('新策略身份须显式登记，不能通过运行时接管')
        old = self.state['ports'][sid]
        if set(specifications) != set(configuration.get('limits', {})):
            raise ValueError('合约执行条款须恰好覆盖本策略路由')
        candidate = self.snapshot_state()
        candidate['ports'][sid] = deepcopy(configuration)
        for instrument, spec in specifications.items():
            previous = candidate['instruments'].get(instrument)
            if previous is not None and not same_spec(previous,spec):
                raise ValueError('已有合约执行条款不能改变')
            if previous is None:
                candidate['instruments'][instrument] = deepcopy(spec)
        validate_registry(candidate)
        old_products = {product_key(i) for i in old['limits']}
        for instrument, limits in configuration['limits'].items():
            product = product_key(instrument)
            previous = [i for i in old['limits'] if product_key(i) == product]
            if product not in old_products or not previous:
                raise ValueError('不能通过路由刷新新增交易产品')
            if not any(limits_from_wire(old['limits'][i]) == limits_from_wire(limits)
                    and same_spec(self.state['instruments'][i],candidate['instruments'][instrument]) for i in previous):
                raise ValueError('路由刷新不能改变产品限额、价格步长或乘数')
        if Decimal(configuration['budget']['max_gross_notional']) != Decimal(old['budget']['max_gross_notional']):
            raise ValueError('路由刷新不能改变策略名义预算')
        # 总数量保留固定部分；逐期货合约部分随候选数量变化，期权总手数不随链长膨胀。
        def future_capacity(port):
            return sum((limits_from_wire(row).max_abs_position or Decimal(0)
                for i,row in port['limits'].items() if not re.search(r'-[CP]-', i, re.I)), Decimal(0))
        fixed = Decimal(old['budget']['max_gross_quantity'])-future_capacity(old)
        expected = fixed+future_capacity(configuration)
        if fixed < 0 or Decimal(configuration['budget']['max_gross_quantity']) != expected:
            if configuration['budget'] != old['budget']:
                raise ValueError('路由刷新不能改变策略总数量政策')
        return candidate

    def configure(self, account, sid, configuration, specifications):
        from bomber.framework.market.basic.base import InstrumentId
        from bomber.framework.trader.runtime.live.recovery import InstrumentPositionScope
        service, execution = account.service, account.execution
        candidate = self.validate_change(sid, configuration, specifications)
        account._validate_account()  # 柜台查询不持提交锁，允许异步成交回报完成入账。
        with service.client._submit_lock:
            routes = {key:InstrumentId.from_str(i) for key,i in configuration['routes'].items()}
            limits = {InstrumentId.from_str(i):limits_from_wire(row) for i,row in configuration['limits'].items()}
            service.reconfigure_strategy(sid, routes=routes,
                budget=SharedRiskBudget(**configuration['budget']), instrument_limits=limits)
            ticks, multipliers = {}, {}
            for raw,spec in candidate['instruments'].items():
                instrument = InstrumentId.from_str(raw)
                ticks[instrument], multipliers[instrument] = Decimal(spec['tick']), Decimal(spec['multiplier'])
                if instrument not in service.instrument_limits:
                    service.instrument_limits[instrument] = RiskLimits(
                        max_order_quantity=service.budget.max_gross_quantity,
                        max_abs_position=service.budget.max_gross_quantity,
                        max_order_notional=service.budget.max_gross_notional,
                        max_abs_position_notional=service.budget.max_gross_notional,
                        max_market_age_ns=120_000_000_000, contract_multiplier=multipliers[instrument])
            execution.client.risk_manager.instrument_limits.update(service.instrument_limits)
            execution.client.planner.planner._tick_size.update(ticks)
            execution.accounting.multipliers.update(multipliers)
            scope = InstrumentPositionScope(candidate['instruments'])
            account.position_scope = service.instrument_scope = execution.backend.instrument_scope = scope
            self.state = candidate
            try:
                service.validate_owned_state()
                service._save()  # 执行登记、路由、归属和回报日志在同一代提交。
            except BaseException:
                service._failed = True
                service.client.disarm('execution_registry_commit_failed')
                raise
