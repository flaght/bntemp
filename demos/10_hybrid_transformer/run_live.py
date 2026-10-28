"""Hybrid Live：接收完整期货分钟，计算模型信号，并交给执行层调整目标仓位。

建议阅读顺序：main -> build_runtime -> prepare_inputs -> assemble -> build_strategy。
真正的启动顺序由 ManagedLiveRuntime.start 控制，而不是由本文件的定义顺序决定。
持续运行后再看 HybridLiveRunner 和两个 Lifecycle，理解行情、时钟和回报如何推进。

完整导读：pro/doc/functions/demos/run_live.md。
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pandas as pd
from dotenv import load_dotenv
from bomber.framework.calendars import SessionRulesCheckpoint
from bomber.framework.dataprep.history import FuturesMinuteArchive
from bomber.framework.dataprep.live_cached_role import CachedRoleReferences
from bomber.framework.dataprep.live_references import live_factor_policy
from bomber.framework.dataprep.live_role import SourceRoleReferences
from bomber.framework.dataprep.reference_freshness import ReferenceFreshnessPolicy
from bomber.framework.dataprep.session_inputs import SessionInputs, load_session_inputs, validate_session_inputs
from bomber.framework.dataprep.sources import DataSourcePurpose, DolphinDbReferenceConfig, ReferenceSourceFactory
from bomber.framework.market.basic.base import CustomBar, DataType
from bomber.framework.market.sources import MarketSourceConfig, assemble_minute_market, create_market_feed
from bomber.framework.trader import DataBinding, ExecutionRoute, RiskLimits
from bomber.framework.trader.assembly import RoleGuard, StrategyBindings, assemble_strategy
from bomber.framework.trader.execution.builders import ExecutionComponents, attach_ctp_persistence, build_ctp_execution
from bomber.framework.trader.execution.ctp import CtpTdApiTransport
from bomber.framework.trader.execution.policy import ExecutionPolicy, StrategyPositionSource
from bomber.framework.trader.execution.recording import RecordingExecutionClient
from bomber.framework.trader.execution.risk import MarketReferencePriceStore
from bomber.framework.trader.live_roles import SessionRoleLiveRunner
from bomber.framework.trader.portfolio import PositionManager
from bomber.framework.trader.runtime.live.channels.ctp import CtpExecutionChannel, CtpLiveProfile, CtpSessionLifecycle, build_simnow_transport, required
from bomber.framework.trader.runtime.live.channels.ctp_market import CtpMarketSessionLifecycle
from bomber.framework.trader.runtime.live.channels.ctp_recovery import CtpResumeLifecycle, validate_ctp_restored_positions
from bomber.framework.trader.runtime.live.contracts import SessionReadiness
from bomber.framework.trader.runtime.live.local import LocalExecutionChannel, RecordingExecutionLifecycle
from bomber.framework.trader.runtime.live.recovery import SessionIdentityCheckpoint, StrategyPositionBinding
from bomber.framework.trader.runtime.live.runtime import ManagedLiveRuntime
from bomber.framework.trader.runtime.reports import LiveRunReport

from .checkpoint import HybridCheckpoint
from .history import build_history, warm_history, warm_recording
from .signal_config import load_product_signal
from .strategy import HybridTransformerConfig, HybridTransformerStrategy

CLIENT_ID = 'hybrid-transformer-live'
MINUTE = 60_000_000_000  # 框架时间戳的单位为纳秒；这里是一分钟。

# 模型与策略声明的默认设置；不扩展成命令行开关。
MODEL_DEVICE = 'cpu'           # 模型推理设备；需要GPU时在代码里改这里。
MODEL_HISTORY_BARS = 1500      # 工作流最多保留的原始分钟窗口。
MODEL_MIN_RAW_BARS = 300       # 最少原始分钟数；其他因子/模型条件仍可能需要继续预热。
MODEL_BAR_TIMESTAMP = 'start'  # 研究数据用分钟开始标签；与事件实际可用时间是两个概念。
REBALANCE_MINUTES = 1          # 新批次的开仓检查间隔；持仓到期仍每轮检查。


def load_market_config(path: Path | None = None, *, source: str | None = None):
    """把行情JSON和显式来源转换为MarketSourceConfig，此处不建立行情连接。

    source明确传入时覆盖JSON；未配置来源时采用配置类默认的CTP。
    DolphinDB还必须说明流表、开始/结束标签、逐周期/累计量额口径。
    """
    values = json.loads(Path(path).read_text(encoding='utf-8')) if path is not None else {}
    if not isinstance(values, dict):
        raise ValueError('行情配置须为JSON对象')
    if source is not None:
        values['source'] = source
    if values.get('source') == 'dolphindb' and not values.get('table'):
        values['table'] = os.getenv('DDB_MARKET_BAR_TABLE')
    config = MarketSourceConfig(**values)
    if config.source not in {'ctp', 'dolphindb'}:
        raise ValueError('Hybrid实时行情仅支持 ctp 或 dolphindb')
    return config


def parse_args(argv=None):
    """读取命令行并进行本地检查，返回值只由main逐项读取。

    这里检查路径、日期和选项组合，不创建策略，也不加载模型权重。
    信号JSON的业务字段在build_strategy真正使用配置时解析。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    # 运行选择：行情来源和执行模式分别配置，DDB行情也可以配SimNow执行。
    parser.add_argument('--connect', action='store_true')
    parser.add_argument('--mode', choices=('recording', 'simnow'), default='recording')
    parser.add_argument('--product', required=True)
    # 三个路径各有用途：模型发布目录、特征方向、按品种配置的信号规则。
    parser.add_argument('--release-path', type=Path, required=True)
    parser.add_argument('--directions-file', type=Path, required=True)
    parser.add_argument('--signal-config', type=Path, required=True)
    # trading_day是本次会话的交易日；source_day是主力/条款参考资料的来源日。
    parser.add_argument('--expected-trading-day', required=True)
    parser.add_argument('--expected-source-day', required=True)
    parser.add_argument('--max-contracts', type=int, default=10)
    parser.add_argument('--seconds', type=float, default=600)
    parser.add_argument('--market-source', choices=('ctp', 'dolphindb'))
    parser.add_argument('--market-config', type=Path, help='行情源配置；DolphinDB在JSON中声明表名、时间标签和量额口径')
    # 历史预热只填充模型窗口；这些历史分钟不会通过策略交易回调重放。
    parser.add_argument('--history-minutes', type=int, default=0)
    parser.add_argument('--history-config', type=Path)
    parser.add_argument('--history-file', type=Path)
    # state-file用于联合恢复目标、订单、仓位和批次；不是普通运行报告。
    parser.add_argument('--state-file', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--enable-orders', action='store_true')
    parser.add_argument('--confirm-simnow', action='store_true')
    parser.add_argument('--report-dir', type=Path, default=Path(__file__).parent/'results'/'live')
    parser.add_argument('--no-session-overrides', action='store_true')
    parser.set_defaults(calendar_id=os.getenv('BOMBER_CALENDAR_ID') or 'china.sse')
    args = parser.parse_args(argv)
    try:
        if not args.connect:
            raise ValueError('须显式 --connect')
        args.market_source_config = load_market_config(args.market_config, source=args.market_source)
        for name in ('expected_trading_day', 'expected_source_day'):
            value = getattr(args, name)
            if datetime.strptime(value, '%Y%m%d').strftime('%Y%m%d') != value:
                raise ValueError('交易日/来源日须为YYYYMMDD')
        args.product = args.product.strip().upper()
        if not args.product.isalpha():
            raise ValueError('品种须为非空字母代码')
        if not args.release_path.is_dir() or not args.directions_file.is_file():
            raise ValueError('模型发布目录或方向文件不存在')
        if not args.signal_config.is_file():
            raise ValueError('信号配置文件不存在')
        if args.max_contracts < 1 or not math.isfinite(args.seconds) or args.seconds <= 0:
            raise ValueError('最大手数和运行时间须为正，运行时间须为有限数')
        if args.history_minutes and not MODEL_MIN_RAW_BARS <= args.history_minutes <= MODEL_HISTORY_BARS:
            raise ValueError('历史预热根数须介于模型最小根数与历史窗口之间')
        if bool(args.history_config or args.history_file) != bool(args.history_minutes):
            raise ValueError('历史预热须同时声明history-minutes及history-config/history-file')
        if args.history_config and not args.history_config.is_file() or args.history_file and not args.history_file.is_file():
            raise ValueError('历史配置或文件不存在')
        # 不读外部历史时，恢复持仓义务及本机CTP窗口，再追加实时完整分钟。
        # 显式启用恢复历史时仍只使用数据库，不以旧文件补造断线期间的分钟。
        if args.resume and args.history_minutes and not args.history_config:
            raise ValueError('启用恢复历史时须提供数据库history-config；实时预热使用history-minutes=0')
        if args.mode == 'simnow':
            # 新会话不能覆盖现有状态文件；恢复必须明确指定原文件与--resume。
            if not (args.enable_orders and args.confirm_simnow and args.state_file):
                raise ValueError('SimNow须 --enable-orders --confirm-simnow --state-file')
            if args.resume != args.state_file.is_file():
                raise ValueError('新会话须新状态文件；恢复须原文件并加--resume')
        elif args.enable_orders or args.confirm_simnow or args.state_file or args.resume:
            raise ValueError('Recording不接受交易授权、交易状态文件或恢复参数')
        # 此处只校验日历输入的声明；实际读取时段/例外资料发生在prepare_inputs。
        validate_session_inputs(SessionInputs(
            product=args.product, calendar_id=args.calendar_id,
            overrides_table=os.getenv('DDB_SESSION_OVERRIDES_TABLE') or None,
            no_overrides=args.no_session_overrides), purpose=DataSourcePurpose.LIVE_REFERENCE)
    except (ValueError, TypeError, OSError) as error:
        parser.error(str(error))
    return args


def model_identity(*, release_path: Path, directions_file: Path, signal_config: Path,
                   market_config: MarketSourceConfig, trading_day: str):
    """返回恢复会话时用于比对的模型/信号资料指纹。

    只读取匹配路径中的配置、权重、方向和信号文件，不运行模型推理。
    同时记录窗口、标签、行情配置与交易日；日历和批次由其他检查点负责。
    """
    root = Path(release_path).resolve()
    files = sorted(root.glob('*/*/config.json')) + sorted(root.glob('*/*/models/best_model.pt'))
    files += [Path(directions_file), Path(signal_config)]
    if not files:
        raise ValueError('模型发布目录没有配置/权重')
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.resolve().relative_to(root) if path.is_relative_to(root) else path.name).encode())
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024*1024), b''):
                digest.update(block)
    return {'sha256': digest.hexdigest(), 'history_bars': MODEL_HISTORY_BARS,
        'min_raw_bars': MODEL_MIN_RAW_BARS, 'model_bar_timestamp': MODEL_BAR_TIMESTAMP,
        'market': market_config.identity, 'trading_day': trading_day}


def prepare_inputs(context, lifecycle, *, product: str, expected_source_day: str,
                   market_config: MarketSourceConfig, calendar_id='china.sse',
                   no_session_overrides=False, reference_database=None,
                   reference_timeout=15, refresh_seconds=5, trading_hours_database=None):
    """准备装配所需的三个对象：references、calendar、upstream。

    context由生命周期创建，包含已确认的交易日与资源清理栈。
    本函数会连接参考数据库；upstream在这里仅创建，稍后由Runner启动。
    返回的集合只包含已准备对象，后续assemble据此构建完整会话。
    """
    config = DolphinDbReferenceConfig.from_env(database=reference_database,
        read_timeout_seconds=reference_timeout)
    source = ReferenceSourceFactory.create_for(DataSourcePurpose.LIVE_REFERENCE, 'dolphindb', config)
    # 先登记关闭动作再打开连接：后续读取或装配失败，也能由Runtime统一清理。
    context.resources.callback(source.close)
    source.open()
    # references回答：当前main角色是谁、合约乘数/最小变动价位是什么、资料是否有效。
    # 参考资料来源固定为DolphinDB；这与market_config选择的实时行情来源相互独立。
    references = CachedRoleReferences(SourceRoleReferences(source, product=product,
        trading_day=context.trading_day, started_ns=time.time_ns(),
        factor_date_basis='source', factor_availability=live_factor_policy('source', 'observed-on-read'),
        refresh_seconds=refresh_seconds, allowed_venues=('SHFE', 'INE'),
        required_currency='CNY', freshness=ReferenceFreshnessPolicy(
            expected_source_day=datetime.strptime(expected_source_day, '%Y%m%d').date())))
    context.references = references
    assignment = references.snapshot(time.time_ns())
    if assignment.source_day.strftime('%Y%m%d') != expected_source_day:
        raise ValueError('主力参考来源日与声明不一致')
    # 时段在静态资料库，不能沿用主力/条款所在的bomber_daily。
    # 显式参数优先，其次独立的时段环境配置；不改变参考资料库配置。
    hours_database = (trading_hours_database or os.getenv('DDB_TRADING_HOURS_DATABASE')
                      or 'dfs://bomber_static')
    hours_table = os.getenv('DDB_TRADING_HOURS_TABLE') or 'fut_trading_session'
    # 同一日历同时服务分钟聚合、历史预热和批次到期，跨休市时不另算一套时间。
    calendar = load_session_inputs(SessionInputs(
        product=product, calendar_id=calendar_id, exchange=references.spec.venue,
        database=hours_database, hours_table=hours_table,
        overrides_table=os.getenv('DDB_SESSION_OVERRIDES_TABLE') or None,
        no_overrides=no_session_overrides),
        purpose=DataSourcePurpose.LIVE_REFERENCE)
    # upstream是厂商原始入口：CTP提供深度，DolphinDB提供来源分钟。
    # assemble稍后会把它包装成模型需要的完整期货CustomBar。
    upstream = create_market_feed(market_config, 'hybrid-market', lifecycle=lifecycle,
        flow_path='/tmp/bomber-hybrid-md', environment='realtime')
    return SimpleNamespace(references=references, calendar=calendar, upstream=upstream)


class HybridLiveRunner(SessionRoleLiveRunner):
    """协调行情、策略与执行的Runner；自身不实现Transformer预测算法。

    父类负责Feed订阅、事件分发、目标规划和执行接线；这里补上Hybrid所需的
    原始历史预热、重复分钟过滤和独立到期时钟。控制线程顺序处理这些动作。
    """
    def begin_bars(self):
        """在生命周期确认通道就绪后，预热模型并开放实时分钟处理。"""
        with self._submit_lock:
            if self.history_minutes or self.bar_archive is None:
                warm_history(self, product=self.strategy.config.product,
                    history_minutes=self.history_minutes, resume=self.resuming,
                    model_bar_timestamp=MODEL_BAR_TIMESTAMP)
            else:
                warm_recording(self, product=self.strategy.config.product, max_bars=MODEL_HISTORY_BARS,
                    model_bar_timestamp=MODEL_BAR_TIMESTAMP)
            super().begin_bars()
            # 历史窗口已预热至启动边界；排除历史重复并保留完整实时分钟。
            self.first_complete_bar_ns = max(self.first_complete_bar_ns, self.history_last_ns + MINUTE)
            self.expire_due()

    def publish(self, feed_id, event):
        """处理新旧窗口交接，再由父类把已完成CustomBar交给策略。"""
        if isinstance(event, CustomBar):
            # 历史窗口已经包含的分钟不能再次触发预测和开仓。
            if event.ts_event <= self.history_last_ns:
                return
            if self.accept_bars and event.ts_event < self.first_complete_bar_ns:
                # 启动边界内的交接分钟只更新模型窗口，不经策略回调生成批次。
                from bomber.framework.market.futures import futures_bar_values
                values = futures_bar_values(event, contract_multiplier=self.strategy.config.contract_multiplier)
                rows = list(self.strategy.predictor.workflow.history)
                rows.append({"trade_time": event.factors["trade_time"], "code": self.strategy.config.product, **values})
                self.strategy.predictor.warm_history(rows)
                self.history_last_ns = event.ts_event
                return
        return super().publish(feed_id, event)

    def _submit_locked(self, intent):
        """提交策略目标前检查通道与旧在途订单；intent表示目标而非已成交仓位。"""
        if not self.session_ready():
            raise RuntimeError('Hybrid提交目标时行情/执行未就绪')
        previous = self.target_store.get(intent.strategy_id)
        if (self.orders and previous is not None and dict(previous.targets) != dict(intent.targets)
                and self.position_manager.working_quantity(self.client.client_id, self.bound_instrument)):
            # 目标已变化时请求撤销旧策略单；撤单与成交结果仍由执行回报确认。
            self.client.cancel_strategy(intent.strategy_id)
        # 执行风控使用当前提交时间，原信号分钟留在metadata中供追踪。
        intent = replace(intent, ts_event=self._clock_ns(), metadata={
            **intent.metadata, 'signal_bar_ns': intent.ts_event})
        return super()._submit_locked(intent)

    def expire_due(self):
        """在正常运行的控制循环中，用当前时钟检查批次到期并继续执行目标。"""
        if not self.accept_bars:
            return
        with self._submit_lock:
            # 先处理已经到达的完整Bar，再处理同一边界的独立时钟。
            now = self._clock_ns()
            # 即使当前没有新的模型输出，已有批次的期限也继续由日历推进。
            self.strategy.on_time(now)
            if self.orders and self.strategy.last_targets:
                self.continue_execution_target(self.strategy.strategy_id, self.strategy.config.target_key,
                    now, trigger_instrument_id=self.bound_instrument)


def build_strategy(references, calendar, *, product: str, release_path: Path,
                   directions_file: Path, signal_config: Path, max_contracts=10):
    """只构建预测器与策略，返回HybridTransformerStrategy。

    selected是信号配置数据；predictor执行特征和模型计算；策略再把信号转换为
    批次与目标仓位。这里不启动行情，也不主动向柜台下单。
    """
    # 到装配阶段才导入模型适配器，其构造过程会加载研究工作流和模型资料。
    from .model_adapter import MizarWorkflowAdapter
    selected = load_product_signal(signal_config, product)
    # JSON按品种选出signal_method、signal_params及hold_bars，返回值在下面真正使用。
    predictor = MizarWorkflowAdapter(
        release_path=release_path, directions_file=directions_file,
        code=product, signal_id=selected.signal_id,
        signal_method=selected.signal_method, signal_params=selected.signal_params,
        device=MODEL_DEVICE, history_bars=MODEL_HISTORY_BARS,
        min_raw_bars=MODEL_MIN_RAW_BARS)
    # max_contracts约束全部未到期批次的绝对手数总和；hold_bars来自所选品种配置。
    # 策略如何分槽、量化信号和生成净目标，继续由strategy.py/holding.py实现。
    config = HybridTransformerConfig(
        product=product, venue=references.spec.venue, signal_id=selected.signal_id,
        max_contracts=max_contracts, hold_bars=selected.hold_bars,
        rebalance_minutes=REBALANCE_MINUTES,
        # 策略目标和ExecutionRoute使用同一个显式键，不依赖配置类自动补全。
        target_key=f"{product.strip().lower()}_main",
        contract_multiplier=references.spec.multiplier, record_signals=True)
    return HybridTransformerStrategy(CLIENT_ID, references, predictor, config, calendar=calendar)


def assemble(inputs, lifecycle, *, product: str, release_path: Path, directions_file: Path,
             signal_config: Path, market_config: MarketSourceConfig, mode='recording',
             max_contracts=10, state_file: Path | None = None,
             history_minutes=0, history_config: Path | None = None, history_file: Path | None = None,
             resume=False, execution_policy=None,
             max_notional=Decimal('1000000'), limit_offset_ticks=1, bar_directory: Path | None = None):
    """把资料、行情、执行、策略和持久化连接成session，交给生命周期启动。

    inputs是prepare_inputs的返回结果；session则还包含Runner、执行客户端、
    状态管理器等运行对象。本函数负责创建和注册，Runner.start由后续启动流程调用。
    """
    execution_policy = execution_policy or ExecutionPolicy('off', 'fills')
    references, calendar, upstream = inputs.references, inputs.calendar, inputs.upstream
    # 1. 根据主力参考确定本次真实合约，例如rb2701.SHFE；当前Live会话固定此合约。
    instrument = references.instrument_id
    profile = CtpLiveProfile('realtime')
    # 2. 把厂商数据包装成完整分钟；同时取得行情就绪检查和逐合约健康检查对象。
    feed, market_channel, health = assemble_minute_market(market_config, upstream, 'HYBRID_1M', profile,
        (instrument,), references.trading_day.strftime('%Y%m%d'), futures_calendar=calendar,
        model_timestamp=MODEL_BAR_TIMESTAMP)
    feed.register_instrument(references.instrument_meta())
    archive = None
    if market_config.source == 'ctp':
        directory = Path(bar_directory) if bar_directory is not None else Path(__file__).parent/'results'/'market'/'ctp'
        archive = FuturesMinuteArchive(directory/f'{instrument}.jsonl', instrument_id=str(instrument),
            contract_multiplier=references.spec.multiplier, max_bars=MODEL_HISTORY_BARS)
        feed.archive = archive  # Feed连接/断开负责打开与关闭存档。
    orders = mode == 'simnow'
    # 3. 装配执行端。Recording记录目标；SimNow使用TD通道并处理真实柜台回报。
    if orders:
        check = SessionReadiness(market_channel, CtpExecutionChannel(lifecycle.driver,
            references.trading_day.strftime('%Y%m%d'), require_orders=True))
        # 从-10手调至+10手可能需要20手增量，因此单笔上限与最终净仓上限不同。
        limits = RiskLimits(max_order_quantity=2*max_contracts, max_abs_position=max_contracts,
            max_order_notional=max_notional, max_abs_position_notional=max_notional,
            max_market_age_ns=120_000_000_000, contract_multiplier=references.spec.multiplier)
        execution = build_ctp_execution(lifecycle.driver, instrument_id=instrument,
            trading_day=references.trading_day.strftime('%Y%m%d'), price_increment=references.spec.tick,
            multiplier=references.spec.multiplier, risk_limits=limits, session_check=check,
            orders=True, limit_offset_ticks=limit_offset_ticks,
            account_reconciliation=execution_policy.account_reconciliation)
    else:
        execution = ExecutionComponents(RecordingExecutionClient(CLIENT_ID), PositionManager(), MarketReferencePriceStore())
        check = SessionReadiness(market_channel, LocalExecutionChannel(CLIENT_ID,
            lambda: execution.client.is_started, channel='recording'))
    # 4. 模型/策略在此加载；这里只创建历史服务，实际历史读取发生在begin_bars。
    strategy = build_strategy(references, calendar, product=product, release_path=release_path,
        directions_file=directions_file, signal_config=signal_config, max_contracts=max_contracts)
    # Runner与执行端共享同一个PositionManager，执行回报才能更新策略所依据的仓位。
    runner = HybridLiveRunner(references=references, session_check=check, orders=orders,
        position_manager=execution.positions, instrument_health=health)
    runner.calendar, runner.strategy, runner.client = calendar, strategy, execution.client
    runner.history_minutes, runner.resuming = history_minutes, resume
    runner.history_last_ns = -1
    runner.bar_archive = archive
    runner.history_service = build_history(
        history_config=history_config, history_file=history_file, calendar=calendar)
    if not getattr(references, 'refresh_on_event', True):
        # 参考发布与目标提交共用锁，避免读取主力/条款时正好发生一次发布切换。
        references.publication_lock = runner._submit_lock
    # 5. 连接两条方向相反的路线：数据流向策略，策略目标流向执行客户端。
    # model-bars是本次Runner的Feed注册键；HYBRID_1M是Feed自己的source_id。
    # 只绑定CUSTOM_BAR到模型，配套原生BAR仍供框架观察行情、更新参考价格。
    bindings = StrategyBindings(
        data=(DataBinding(str(instrument), 'model-bars', instrument, DataType.CUSTOM_BAR, '1-MINUTE'),),
        execution=(ExecutionRoute(strategy.config.target_key, execution.client.client_id, instrument),),
        role_guard=RoleGuard(references, product, 'main', lambda: strategy.last_processed_ns))
    # assemble_strategy执行注册和一致性检查；此处没有调用runner.start。
    assemble_strategy(runner, strategy, feeds={'model-bars': feed}, execution=execution, bindings=bindings)
    # 6. 为同一次联合检查点增加批次、交易日历及模型资料身份，供恢复时一起核对。
    components = {'hybrid': HybridCheckpoint(strategy), 'session_rules': SessionRulesCheckpoint(calendar),
        'live_session': SessionIdentityCheckpoint(model_identity(
            release_path=release_path, directions_file=directions_file, signal_config=signal_config,
            market_config=market_config, trading_day=references.trading_day.strftime('%Y%m%d')))}
    manager = attach_ctp_persistence(execution, runner, state_file=state_file,
        state_components=components) if orders else None
    runner.manager = manager
    if manager is not None:
        # 传入函数本身，尚未执行save()；后续批次/目标/时钟变化会触发保存。
        strategy.bind_checkpoint(manager.save)
    # 字段是后续生命周期使用的已构造对象；这里的SimpleNamespace是会话返回结果。
    return SimpleNamespace(runner=runner, strategy=strategy, execution=execution, client=execution.client,
        driver=lifecycle.driver, upstream=upstream, ledger=execution.ledger, manager=manager,
        references=references, bar_feed=feed, market_profile=profile, readiness=check,
        recovery_binding=StrategyPositionBinding(strategy.strategy_id, execution.client.client_id,
            {strategy.config.target_key: instrument}))


class HybridSessionLifecycle(CtpResumeLifecycle, CtpSessionLifecycle):
    """SimNow会话控制器：沿用框架连接/恢复/停机流程，补充Hybrid检查和轮询。

    CtpResumeLifecycle先处理恢复分支；新会话start会继续交给CtpSessionLifecycle。
    """
    def validate_restored_session(self, session, day):
        """联合检查保存的持仓、批次与目标版本，返回恢复流程需要的revision。"""
        validate_ctp_restored_positions(session, day, session.recovery_binding)
        HybridCheckpoint(session.strategy).validate_targets(session.runner.target_store)
        revision = session.strategy._revision
        session.runner.hold_restored_target(session.strategy.strategy_id, revision)
        return revision

    def poll(self, session):
        # 顺序有意义：已到达完整分钟 -> 父类回报/健康检查 -> 当前时钟的到期处理。
        session.bar_feed.poll()
        result = super().poll(session)
        session.runner.expire_due()
        return result

    def verify(self, session):
        """运行结束时验收处理和执行状态；不强迫产生非零信号或成交。"""
        if session.runner.failure or session.client.report_errors:
            raise RuntimeError(session.runner.failure or 'Hybrid执行回报异常')
        if not session.strategy.bars_used and not self.resuming:
            raise RuntimeError('没有处理完整实时分钟')


class HybridRecordingLifecycle(RecordingExecutionLifecycle, CtpMarketSessionLifecycle):
    """只记录策略目标，仍使用真实行情、参考、模型和批次逻辑。

    父类Recording的验收要求取得目标请求；这里不创建TD交易通道。
    """
    def poll(self, session):
        session.bar_feed.poll()
        result = super().poll(session)
        session.runner.expire_due()
        return result


class HybridLiveReport(LiveRunReport):
    """停机时补充Hybrid分析文件，再由父类写summary.json和events.txt。"""
    def write(self, *, status, fields, session, events):
        if session is not None:
            pd.DataFrame(session.strategy.signal_records).to_csv(
                self.path/'signals.csv', index=False, encoding='utf-8')
            pd.DataFrame(session.strategy.batch_records).to_csv(
                self.path/'batches.csv', index=False, encoding='utf-8')
            # 此文件只是策略组件快照；完整恢复仍应使用统一state-file。
            (self.path/'strategy_state.json').write_text(json.dumps(
                HybridCheckpoint(session.strategy).snapshot_state(), ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8')
        return super().write(status=status, fields=fields, session=session, events=events)


def build_runtime(
    *, product: str, release_path: Path, directions_file: Path, signal_config: Path,
    expected_trading_day: str, expected_source_day: str,
    mode='recording', max_contracts=10, seconds=600,
    market_config: MarketSourceConfig | None = None,
    calendar_id='china.sse', no_session_overrides=False,
    history_minutes=0, history_config: Path | None = None, history_file: Path | None = None,
    state_file: Path | None = None, resume=False, report_dir: Path | None = None,
    query_timeout=15, max_session_orders=100, max_notional=Decimal('1000000'),
    limit_offset_ticks=1, execution_policy=None, bar_directory: Path | None = None,
):
    """创建并返回ManagedLiveRuntime，此时尚未执行prepare_inputs和assemble。

    CLI有对应字段时由main显式传入；未传入的运行细项采用本签名中的默认值。
    Runtime保存下面两个延迟回调，run/start时才依次准备资料、组装并启动会话。
    """
    market_config = market_config if market_config is not None else MarketSourceConfig()
    execution_policy = execution_policy or ExecutionPolicy('off', 'fills')
    if mode not in {'recording', 'simnow'}:
        raise ValueError('mode 仅支持 recording 或 simnow')
    if market_config.source not in {'ctp', 'dolphindb'}:
        raise ValueError('Hybrid实时行情仅支持 ctp 或 dolphindb')
    if mode == 'recording' and resume:
        raise ValueError('recording 模式不支持恢复交易会话')
    if execution_policy.position_source is not StrategyPositionSource.FILLS:
        raise ValueError('recording/simnow 模式须使用 fills 仓位来源')
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('seconds 必须是有限正数')
    if not math.isfinite(query_timeout) or not 0 < query_timeout <= 120:
        raise ValueError('CTP查询超时须在0至120秒')
    if not 1 <= max_session_orders <= 10000 or limit_offset_ticks < 0:
        raise ValueError('报单上限或限价偏移无效')
    if not max_notional.is_finite() or max_notional <= 0:
        raise ValueError('名义上限须为有限正数')

    product = product.strip().upper()
    report_dir = Path(report_dir) if report_dir is not None else Path(__file__).parent/'results'/'live'
    release_path, directions_file, signal_config = Path(release_path), Path(directions_file), Path(signal_config)

    # 控制器决定怎样连接、等待就绪、恢复和停机；与行情来源的选择相互独立。
    if mode == 'recording':
        lifecycle = HybridRecordingLifecycle(expected_trading_day=expected_trading_day,
            market_source=market_config.source)
    else:
        transport = build_simnow_transport(CtpTdApiTransport, client_id=CLIENT_ID,
            flow_path='/tmp/bomber-hybrid-td', timeout_seconds=query_timeout)
        lifecycle = HybridSessionLifecycle(transport,
            md_front=required('CTP_MD_ADDRESS') if market_config.source == 'ctp' else None,
            orders=True, max_session_orders=max_session_orders, expected_trading_day=expected_trading_day,
            resume=resume, account_reconciliation=execution_policy.account_reconciliation)

    def describe(session):
        # session在准备/装配失败时可能还不存在，报告也要能说明这种失败。
        if session is None:
            return {}
        strategy = session.strategy
        return {
            'bars_used': strategy.bars_used, 'last_prediction': strategy.last_prediction,
            'model_window_bars': len(strategy.predictor.workflow.history),
            'model_min_raw_bars': MODEL_MIN_RAW_BARS,
            'last_targets': strategy.last_targets,
            'active_batches': strategy.holdings.snapshot_state()['active'],
            'order_updates_received': strategy.order_updates_received,
            'fills_received': strategy.fills_received,
            'history': session.runner.history_service.last_audit,
            'bar_file': str(session.runner.bar_archive.path) if session.runner.bar_archive is not None else None,
            'recorded_bars': session.runner.bar_archive.recorded_bars if session.runner.bar_archive is not None else 0,
        }

    report = HybridLiveReport(report_dir, prefix='hybrid-'+mode,
        metadata={'mode': mode, 'product': product, 'market_config': market_config.identity,
            'execution_policy': execution_policy.snapshot_state()}, describe=describe)

    def progress(session):
        # 控制循环比一分钟更频繁；只在有效bar计数变化时打印一次模型进度。
        strategy = session.strategy
        count = strategy.bars_used
        if count != getattr(session, '_reported_bars', -1):
            session._reported_bars = count
            # bars_used来自交易检查点；model_bars包含本机缓存及新分钟，二者不同。
            model_bars = len(strategy.predictor.workflow.history)
            print(f"Hybrid分钟: bars={count} model_window={model_bars} "
                  f"min_raw_bars={MODEL_MIN_RAW_BARS} prediction={strategy.last_prediction} "
                  f"targets={strategy.last_targets} active_batches={len(strategy.holdings.active)}", flush=True)

    # lambda在这里被创建但没有执行。Runtime.start先取得context，再调用准备回调；
    # 准备回调返回inputs后，Runtime才调用会话组装回调。
    return ManagedLiveRuntime(CLIENT_ID, controller=lifecycle,
        prepare_inputs=lambda context: prepare_inputs(context, lifecycle,
            product=product, expected_source_day=expected_source_day, market_config=market_config,
            calendar_id=calendar_id, no_session_overrides=no_session_overrides),
        assemble_session=lambda inputs, context: assemble(inputs, lifecycle,
            product=product, release_path=release_path, directions_file=directions_file,
            signal_config=signal_config, market_config=market_config, mode=mode,
            max_contracts=max_contracts, state_file=state_file, history_minutes=history_minutes,
            history_config=history_config, history_file=history_file, resume=resume,
            execution_policy=execution_policy, max_notional=max_notional,
            limit_offset_ticks=limit_offset_ticks, bar_directory=bar_directory),
        report=report, seconds=seconds, progress=progress)


def main(argv=None):
    """应用入口：加载环境 -> 解析参数 -> 显式接线 -> 运行 -> 最后清理。"""
    load_dotenv()
    args = parse_args(argv)
    # 只转交命令行已声明的字段；query_timeout等未出现的项目由build_runtime取默认值。
    runtime = build_runtime(
        product=args.product,
        release_path=args.release_path,
        directions_file=args.directions_file,
        signal_config=args.signal_config,
        expected_trading_day=args.expected_trading_day,
        expected_source_day=args.expected_source_day,
        mode=args.mode,
        max_contracts=args.max_contracts,
        seconds=args.seconds,
        market_config=args.market_source_config,
        calendar_id=args.calendar_id,
        no_session_overrides=args.no_session_overrides,
        history_minutes=args.history_minutes,
        history_config=args.history_config,
        history_file=args.history_file,
        state_file=args.state_file,
        resume=args.resume,
        report_dir=args.report_dir,
    )
    try:
        # 这里才进入真实生命周期：prepare/start、持续poll、verify和stop。
        return runtime.run()
    finally:
        # run内部也会stop；框架stop具有幂等保护，重复调用不会重复释放会话。
        runtime.stop()


if __name__ == '__main__':
    main()
