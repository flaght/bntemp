"""Hybrid原始分钟预热：先填模型窗口，再进入实时策略处理。

build_history只构建历史服务；warm_history才读取指定交易分钟的数据。
整个预热过程调用预测器的专用接口，不把旧行情送入策略开仓回调。
"""
import json
import time
from copy import deepcopy
from pathlib import Path

from bomber.framework.dataprep.history import HistoryService, DolphinDbHistoryConfig, HistoryError, HistoryResult, HistoryUnavailable
from bomber.framework.dolphindb import DolphinDbConnectionConfig
from bomber.framework.calendars.sessions import ns_datetime
from .checkpoint import validate_model_identity
from bomber.framework.trader.runtime.live.history import history_stamps

MINUTE = 60_000_000_000


def build_history(
    *,
    history_config: Path | None = None,
    history_file: Path | None = None,
    calendar=None,
):
    """创建历史服务，复用策略已经加载的会话日历。

    history_config是历史行情数据库配置JSON，与实时market_config用途不同。
    date_basis=trading时，calendar必须由run_live的assemble显式传入；
    此处不另建日历，避免历史时间与策略的持仓到期时间使用不同会话规则。
    本函数只构建服务；实际读取由warm_history完成。
    """
    config = None
    if history_config is not None:
        values = json.loads(Path(history_config).read_text())

        if values.get("date_basis") == "trading":
            if calendar is None:
                raise HistoryError(
                    "交易日历史预热缺少会话日历；请在run_live.py中调用 "
                    "build_history(..., calendar=calendar)"
                )
            # 用交易日对应的会话窗口还原夜盘、跨午夜分钟的自然日期。
            values["session_calendar"] = calendar

        # 历史不足时保留可用分钟；累计量额缺少前值的分钟不合并、不补造。
        values.setdefault("allow_missing_minutes", True)
        config = DolphinDbHistoryConfig(
            connection=DolphinDbConnectionConfig.from_env(purpose="market"),
            **values,
        )

        # Hybrid需要原始量额和持仓量，EMA的纯价格配置不能直接用于模型预热。
        required = {
            "open", "high", "low", "close",
            "volume", "value", "open_interest",
        }
        missing = required - config.columns.keys()
        if missing:
            raise HistoryError(
                f"Hybrid 历史配置缺少原始字段: {sorted(missing)}"
            )

    return HistoryService(
        database_config=config,
        file_path=history_file,
        missing_policy="allow",
    )


def warm_history(runner, *, product: str, history_minutes=0, resume=False, model_bar_timestamp='start'):
    """读取启动边界之前的N个交易分钟，调用predictor.warm_history填充窗口。

    history_minutes=0时不读历史，后续依靠实时分钟积累窗口。
    resume改变本次读取的审计阶段名称；恢复的批次/订单由统一检查点负责。
    """
    if not history_minutes:
        print('Hybrid历史预热未启用: history_minutes=0，等待实时分钟积累模型窗口', flush=True)
        return
    # 启动时正在形成的分钟尚未结束，先向下对齐到已完成的分钟边界。
    end = runner._clock_ns() // MINUTE * MINUTE
    # 使用交易日历取N个分钟，而不是从墙钟时间简单减去N分钟；午休等间隔会跳过。
    stamps = history_stamps(runner.calendar, end, history_minutes)
    service = runner.history_service
    phase = 'recovery' if resume else 'preopen'
    started = time.monotonic()
    print(f"Hybrid预热开始: instrument={runner.bound_instrument} phase={phase} "
          f"backend={service.backend} required={len(stamps)} "
          f"first_ns={stamps[0] if stamps else None} last_ns={stamps[-1] if stamps else None}", flush=True)
    if isinstance(service.config, DolphinDbHistoryConfig):
        source = service.config
        print(f"Hybrid历史数据源: endpoint={source.connection.host}:{source.connection.port} "
              f"database={source.database} table={source.table}", flush=True)
    try:
        result = service.load(str(runner.bound_instrument), stamps, phase=phase)
        # 只允许真实覆盖不足；读取失败不能被解释成“没有历史”。
        if service.backend in result.unavailable_sources:
            raise HistoryUnavailable(f"历史行情源读取失败: {service.backend}；请检查连接和查询")
        rows = _model_rows(result.bars, runner, product, model_bar_timestamp)
        # 专用预热接口不经strategy.on_custom_bar，因此不会重建历史交易批次。
        count = runner.strategy.predictor.warm_history(rows)
    except Exception as error:
        print(f"Hybrid预热失败: instrument={runner.bound_instrument} phase={phase} "
              f"required={len(stamps)} elapsed_seconds={time.monotonic() - started:.3f} "
              f"error={type(error).__name__}: {error}", flush=True)
        # 区分数据库读取失败和查询成功但分钟未匹配；保留原异常停止启动。
        print("Hybrid历史读取审计: " + json.dumps(
            service.last_audit, ensure_ascii=False, default=str), flush=True)
        raise
    # 记录窗口最后一个事件时间，实时publish用它排除重叠的分钟。
    runner.history_last_ns = max((bar.ts_event for bar in result.bars), default=-1)
    workflow = getattr(runner.strategy.predictor, 'workflow', None)
    minimum = getattr(workflow, 'min_raw_bars', 300)
    remaining = max(0, minimum - count)
    print(f"Hybrid历史加载完成: requested={len(stamps)} loaded={count} "
          f"missing={len(result.missing)} model_window={count} min_raw_bars={minimum} "
          f"warmup_remaining={remaining} phase={phase} sources={result.sources} "
          f"elapsed_seconds={time.monotonic() - started:.3f}", flush=True)
    if remaining:
        print(f"Hybrid模型预热等待: 已装入{count}根历史分钟，仍需至少{remaining}根完整实时分钟；"
              "保持订阅，模型未就绪前不建立新批次", flush=True)


def warm_recording(runner, *, product: str, max_bars: int, model_bar_timestamp='start'):
    """只恢复本机CTP录制的近期原始分钟；部分窗口也可继续积累，不重放交易。"""
    archive = runner.bar_archive
    end = runner._clock_ns() // MINUTE * MINUTE
    bars = archive.recent(before_ns=end, limit=max_bars)
    missing = ()
    if bars:
        # 按交易分钟限制最大回看范围，不能把很久之前的缓存当作近期窗口。
        expected = history_stamps(runner.calendar, end, max_bars)
        allowed = set(expected)
        bars = tuple(bar for bar in bars if bar.ts_event in allowed)
        if bars:
            observed = {bar.ts_event for bar in bars}
            missing = tuple(stamp for stamp in expected if stamp >= bars[0].ts_event and stamp not in observed)
    result = HistoryResult(bars, missing, ('ctp-recording',), True)
    audit = {'phase': 'local-recording', 'file': str(archive.path), **result.audit()}
    runner.history_service.last_audit = audit
    try:
        rows = _model_rows(bars, runner, product, model_bar_timestamp)
        count = runner.strategy.predictor.warm_history(rows)
    except Exception as error:
        print(f"Hybrid本地分钟恢复失败: file={archive.path} error={type(error).__name__}: {error}", flush=True)
        raise
    runner.history_last_ns = max((bar.ts_event for bar in bars), default=-1)
    print(f"Hybrid本地分钟恢复: file={archive.path} loaded={count} "
          f"missing={len(missing)} recorded={archive.recorded_bars}，后续完整分钟持续追加", flush=True)
    if missing:
        print(f"Hybrid本地分钟存在真实缺口: first_ns={missing[0]} last_ns={missing[-1]}；"
              "不补造分钟、不平移时间，模型沿用现有时间分段规则", flush=True)


def _model_rows(bars, runner, product, model_bar_timestamp):
    """全部历史检查通过后才交给模型；错误中保留合约、时间和原始量额。"""
    rows = []
    for bar in bars:
        identity = f"instrument={bar.instrument_id} ts_event={bar.ts_event}"
        # 数据供应器已标准化字段；这里继续拒绝缺少量额/持仓量或混入复权价格的数据。
        fields = ('open', 'high', 'low', 'close', 'volume', 'value', 'open_interest')
        if any(getattr(bar, field) is None for field in fields):
            raise HistoryError('Hybrid历史分钟必须包含原始OHLCV、value、open_interest')
        if bar.cumulative_factor is not None or bar.adjusted_close is not None:
            raise HistoryError('Hybrid模型使用原始行情，历史源不能提供复权分钟')
        if bar.volume <= 0:
            raise HistoryError(f'Hybrid历史分钟成交量须为正: {identity}')
        point = ns_datetime(bar.ts_event, runner.calendar.zone)
        if runner.calendar.window(point) is None:
            raise HistoryError('Hybrid历史包含盘外分钟')
        # 历史Bar事件采用结束边界减1ns；先还原边界，再转成模型约定的开始/结束标签。
        label = bar.ts_event + 1 - (MINUTE if model_bar_timestamp == 'start' else 0)
        row = {name: float(getattr(bar, name)) for name in fields if name != 'open_interest'}
        # open_interest是框架字段名，openint是当前研究工作流使用的名称。
        # VWAP = 本周期成交额 / 本周期成交量 / 每手合约乘数。
        row.update(trade_time=ns_datetime(label, runner.calendar.zone), code=product,
                   openint=float(bar.open_interest),
                   vwap=float(bar.value / bar.volume / runner.strategy.config.contract_multiplier))
        tolerance = max(1e-8, row['high'] * 1e-6)
        if not row['low'] - tolerance <= row['vwap'] <= row['high'] + tolerance:
            raise HistoryError(f"Hybrid历史VWAP与OHLC不一致: {identity} "
                f"volume={bar.volume} value={bar.value} vwap={row['vwap']} "
                f"low={bar.low} high={bar.high}")
        rows.append(row)
    return rows


def warm_model_seed(runner, seed, *, identity, max_bars, minimum, minute_ns):
    """合并同一模型、同一真实合约的已完成窗口；不补造断线分钟。"""
    if seed is None:
        return
    validate_model_identity(seed['identity'], identity)
    if seed['instrument'] != str(runner.bound_instrument):
        return  # 新合约只读自己的历史，不能拿旧主力分钟预热。
    import pandas as pd
    end = runner._clock_ns() // minute_ns * minute_ns
    expected = set(history_stamps(runner.calendar, end, max_bars))
    rows = {}
    for row in [*seed['rows'], *runner.strategy.predictor.workflow.history]:
        label = pd.Timestamp(row['trade_time'])
        if label.tzinfo is None:
            raise ValueError('持续模型窗口时间必须携带时区')
        stamp = label.value + minute_ns - 1  # Hybrid固定使用分钟开始标签。
        if stamp not in expected:
            continue
        if stamp in rows and rows[stamp] != row:
            raise ValueError(f'持续窗口与历史源同一分钟内容冲突: ts_event={stamp}')
        rows[stamp] = row
    count = runner.strategy.predictor.warm_history([rows[s] for s in sorted(rows)][-max_bars:])
    runner.history_last_ns = max(runner.history_last_ns, max(rows, default=-1))
    print(f'Hybrid休市窗口续接: model_window={count} min_raw_bars={minimum} '
          f'warmup_remaining={max(0, minimum-count)}；不重放历史交易', flush=True)


class ModelWindowState:
    """保留已完成的原始模型分钟；下一会话由历史接口合并，不重放信号。"""
    def __init__(self, runner):
        self.runner = runner

    def snapshot_state(self):
        runner = self.runner
        return {
            "instrument": str(runner.bound_instrument),
            "identity": deepcopy(runner.model_identity),
            "rows": deepcopy(list(runner.strategy.predictor.workflow.history)),
        }

    def restore_state(self, saved):
        self.runner.model_seed = deepcopy(saved)
