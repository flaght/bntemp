"""纯账户装配：只恢复账户账本里的执行登记，不导入策略工厂或模型。"""
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

from .account_config import account_identity


def rollover(account, payload):
    from bomber.framework.trader.runtime.live.channels.ctp_rollover import observe_rollover
    from bomber.framework.trader.runtime.live.channels.ctp_owned import restore_external_baseline
    old = payload['component_states']['session_identity']['identity']
    new = account.manager.state_components['session_identity'].identity
    candidate = {**deepcopy(old), 'trading_day':new['trading_day']}
    if old['trading_day'] >= new['trading_day'] or candidate != new:
        raise ValueError('账户跨日只能前进交易日，不能隐式更改执行身份、额度或登记策略')
    shared = payload['component_states']['shared_account']
    if shared.get('failed') or shared.get('pending'):
        raise ValueError('账户存在未决发送或故障，保留原账本')
    journal = shared['shared']['event_journal']
    if any(row['status'] != 'completed' for row in journal['requests'].values()):
        raise ValueError('账户存在未决执行请求')
    account.rollover_audit = observe_rollover(payload, driver=account.driver, transport=account.transport,
        day=account.expected_day, multipliers={i:r.contract_multiplier for i,r in account.service.instrument_limits.items()},
        scope=account.position_scope, preserve_external=True, generic_close_venues=account.generic_close_venues)
    payload['component_states']['session_identity']['identity'] = new
    return restore_external_baseline(account, payload, refresh=True)


def build_host(config, *, resume, diagnostics=None, session_order_budget=None, **unused):
    from bomber.framework.market.basic.base import InstrumentId
    from bomber.framework.trader.persistence import JsonStateStore
    from bomber.framework.trader.execution.ctp import CtpNativeTraderDriver, CtpTdApiTransport
    from bomber.framework.trader.execution.builders import build_ctp_portfolio_execution
    from bomber.framework.trader.execution.shared import SharedAccountExecutionService, SharedRiskBudget
    from bomber.framework.trader.execution.registry import ExecutionRegistry, limits_from_wire
    from bomber.framework.trader.execution.ipc import ExecutionEventJournal
    from bomber.framework.trader.execution.risk import RiskLimits
    from bomber.framework.trader.execution.policy import AccountReconciliationMode
    from bomber.framework.trader.runtime.live.channels.ctp import build_simnow_transport, assert_account_ready
    from bomber.framework.trader.runtime.live.channels.ctp_shared import CtpSharedAccountLifecycle
    from bomber.framework.trader.runtime.live.recovery import InstrumentPositionScope
    from bomber.framework.trader.runtime.live.local import strategy_state_lock
    if not resume:
        raise ValueError('账户服务须从已核验的账户执行登记恢复；首次使用独立迁移入口')
    saved = JsonStateStore(config['account_state_file']).load()
    if saved is None:
        raise ValueError('公共账户账本不存在；先执行migrate_account，不自动创建空账本接管旧仓')
    registry = ExecutionRegistry(saved.payload['component_states'].get('execution_registry'))
    settings = config['account']
    identity = account_identity(config, registry.state['ports'])
    previous = saved.payload['component_states']['session_identity']['identity']
    if {**previous,'trading_day':identity['trading_day']} != identity:
        raise ValueError('公共账户身份、额度或登记策略与账本不一致')
    remaining = settings['max_session_orders'] if session_order_budget is None else session_order_budget
    if type(remaining) is not int or not 0 < remaining <= settings['max_session_orders']:
        raise ValueError('账户剩余报单额度无效')
    resources, driver = ExitStack(), None
    try:
        resources.enter_context(strategy_state_lock(config['account_state_file']))
        transport = build_simnow_transport(CtpTdApiTransport, client_id=settings['client_id'],
            flow_path=f"/tmp/{settings['client_id']}-td")
        CtpSharedAccountLifecycle.acquire(resources, transport, environment='simnow')
        holder = {}
        def disconnected(reason):
            if 'client' in holder:
                holder['client'].mark_disconnected(reason)
        driver = CtpNativeTraderDriver(transport.client_id, transport.account_id, transport,
            enable_simnow_orders=True, max_session_orders=remaining, disconnect_handler=disconnected)
        driver.new_order_budget_window = session_order_budget is not None
        driver.start(lambda report:None)
        if driver.trading_day != config['trading_day']:
            raise ValueError(f"账户TD交易日不一致: actual={driver.trading_day} expected={config['trading_day']}")
        assert_account_ready(driver)
        if diagnostics is not None:
            diagnostics.update(actual_TD=driver.trading_day, submitted_orders=driver.submitted_orders)
        driver.stop()
        specifications = registry.state['instruments']
        scope = InstrumentPositionScope(specifications)
        budget = SharedRiskBudget(settings['max_gross_quantity'], settings['max_gross_notional'])
        limits = {InstrumentId.from_str(i):RiskLimits(max_order_quantity=budget.max_gross_quantity,
            max_abs_position=budget.max_gross_quantity, max_order_notional=budget.max_gross_notional,
            max_abs_position_notional=budget.max_gross_notional, max_market_age_ns=120_000_000_000,
            contract_multiplier=Decimal(row['multiplier'])) for i,row in specifications.items()}
        execution = build_ctp_portfolio_execution(driver, trading_day=config['trading_day'],
            price_increments={InstrumentId.from_str(i):Decimal(row['tick']) for i,row in specifications.items()},
            multipliers={i:r.contract_multiplier for i,r in limits.items()}, instrument_limits=limits,
            session_check=lambda:driver.is_simnow_session, orders=True,
            limit_offset_ticks=settings['limit_offset_ticks'], instrument_scope=scope,
            account_reconciliation=AccountReconciliationMode.OBSERVE,
            generic_close_venues=('DCE','CZCE','CFFEX'), generic_close_today_first_venues=('DCE','CZCE','CFFEX'))
        holder['client'] = execution.client
        service = SharedAccountExecutionService(execution.client, prices=execution.prices,
            instrument_limits=limits, budget=budget, instrument_scope=scope, execution_mode='independent')
        for sid,row in registry.state['ports'].items():
            service.strategy_client(sid, routes={key:InstrumentId.from_str(i) for key,i in row['routes'].items()},
                budget=SharedRiskBudget(**row['budget']),
                instrument_limits={InstrumentId.from_str(i):limits_from_wire(r) for i,r in row['limits'].items()})
        service.event_journal = ExecutionEventJournal(service._ports)
        account = CtpSharedAccountLifecycle(execution, service, transport=transport,
            state_file=config['account_state_file'], identity=identity, resources=resources,
            expected_day=config['trading_day'], position_scope=scope, preserve_external=True, rollover_transform=rollover)
        account.manager.state_components['execution_registry'] = registry
        strategies = {sid:SimpleNamespace(session=SimpleNamespace()) for sid in service._ports}
        return SimpleNamespace(account=account, service=service, resources=resources, strategies=strategies,
            configure=lambda sid,configuration,specs:registry.configure(account,sid,configuration,specs),
            snapshot=lambda:dict(execution_diagnostics=dict(account=service.snapshot_state())))
    except BaseException:
        if driver is not None:
            driver.stop()
        resources.close()
        raise
