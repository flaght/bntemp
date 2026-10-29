"""Hybrid历史适配：研究字段/时间标签与模型预热；加载和窗口编排由框架负责。"""
from copy import deepcopy
from itertools import chain

from bomber.framework.calendars.sessions import ns_datetime
from bomber.framework.dataprep.history import build_history_service
from bomber.framework.dataprep.history.futures import futures_history_values
from bomber.framework.trader.runtime.live.history import warm_history_window, merge_history_rows
from .checkpoint import validate_model_identity

MINUTE = 60_000_000_000
RAW_FIELDS = ('open', 'high', 'low', 'close', 'volume', 'value', 'open_interest')


def build_history(*, history_config=None, history_file=None, calendar=None):
    return build_history_service(history_config=history_config, history_file=history_file,
        calendar=calendar, required_fields=RAW_FIELDS, missing_policy='allow')


def _warm(runner, *, product, minutes, phase, model_bar_timestamp, archive=None):
    result = warm_history_window(runner.history_service, calendar=runner.calendar,
        instrument=runner.bound_instrument, before_ns=runner._clock_ns(), minutes=minutes,
        phase=phase, archive=archive, archive_source='ctp-recording', label='Hybrid',
        consume=lambda bars: runner.strategy.predictor.warm_history(
            _model_rows(bars, runner, product, model_bar_timestamp)))
    if result is not None:
        runner.history_last_ns = max((bar.ts_event for bar in result.bars), default=-1)
        _progress(getattr(getattr(runner.strategy.predictor, 'workflow', None),
                                  'min_raw_bars', 300), len(result.bars))


def warm_history(runner, *, product, history_minutes=0, resume=False, model_bar_timestamp='start'):
    _warm(runner, product=product, minutes=history_minutes, phase='recovery' if resume else 'preopen',
          model_bar_timestamp=model_bar_timestamp)


def warm_recording(runner, *, product, max_bars, model_bar_timestamp='start'):
    _warm(runner, product=product, minutes=max_bars, phase='preopen',
          model_bar_timestamp=model_bar_timestamp, archive=runner.bar_archive)


def _model_rows(bars, runner, product, model_bar_timestamp):
    if model_bar_timestamp not in {'start', 'end'}:
        raise ValueError('模型时间标签须为start/end')
    rows = []
    for bar in bars:
        values = futures_history_values(bar, calendar=runner.calendar,
            contract_multiplier=runner.strategy.config.contract_multiplier)
        label = bar.ts_event + 1 - (MINUTE if model_bar_timestamp == 'start' else 0)
        rows.append(dict(trade_time=ns_datetime(label, runner.calendar.zone), code=product, **values))
    return rows


def _event_ns(row):
    import pandas as pd
    stamp = pd.Timestamp(row['trade_time'])
    if stamp.tzinfo is None:
        raise ValueError('持续模型窗口时间必须携带时区')
    return stamp.value + MINUTE - 1  # Live固定研究标签为分钟开始。


def warm_model_seed(runner, seed, *, identity, max_bars, minimum, minute_ns):
    if minute_ns != MINUTE:
        raise ValueError('Hybrid模型窗口使用一分钟数据')
    if seed is None:
        return
    validate_model_identity(seed['identity'], identity)
    if seed['instrument'] != str(runner.bound_instrument):
        return  # 新合约只用自己的原始行情。
    rows = merge_history_rows(chain(seed['rows'], runner.strategy.predictor.workflow.history),
        calendar=runner.calendar, before_ns=runner._clock_ns(), minutes=max_bars, event_ns=_event_ns)
    runner.strategy.predictor.warm_history(rows)
    runner.history_last_ns = max(runner.history_last_ns, _event_ns(rows[-1]) if rows else -1)
    _progress(minimum, len(rows))


def _progress(minimum, count):
    print(f'Hybrid模型窗口: model_window={count} min_raw_bars={minimum} '
          f'warmup_remaining={max(0, minimum-count)}', flush=True)
    if count < minimum:
        print(f'Hybrid模型预热等待: 已装入{count}根分钟，仍需至少{minimum-count}根完整实时分钟；'
              '模型未就绪前不建立新批次', flush=True)


class ModelWindowState:
    """公共常驻通过组件契约保存模型原始窗口；不重放历史信号。"""
    def __init__(self, runner):
        self.runner = runner

    def snapshot_state(self):
        return {'instrument': str(self.runner.bound_instrument),
                'identity': deepcopy(self.runner.model_identity),
                'rows': deepcopy(list(self.runner.strategy.predictor.workflow.history))}

    def restore_state(self, saved):
        self.runner.model_seed = deepcopy(saved)
