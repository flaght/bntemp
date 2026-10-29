"""公共常驻和 EMA 接线离线检查；不联网，不替代或运行原生 EMA 数值算法。"""
from contextlib import redirect_stdout
from datetime import datetime
from decimal import Decimal
from importlib import import_module
from io import StringIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, RLock
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import Mock, patch
import unittest

from tests.run_hybrid_live_offline import base
from tests.test_demo_hybrid_live import calendar, ZONE, ns
from bomber.framework.trader.runtime.live.continuous import LiveWindowContext

# 补齐已有 SDK 夹具的公共导出；全部指向真实 Python 实现。
trader = sys.modules['bomber.framework.trader']
for suffix, names in (
    ('portfolio', ('TargetStore', 'PortfolioCoordinator')),
    ('execution.risk', ('MarketReferencePriceStore',)),
    ('execution.recording', ('RecordingExecutionClient',)),
):
    module = import_module(trader.__name__+'.'+suffix)
    for name in names:
        setattr(trader, name, getattr(module, name))

# 仅让入口模块可导入；以下检查都不构造指标。数值回归须使用真实 Bomber 环境。
try:
    import bomber.indicators
except ImportError:
    indicators = ModuleType('bomber.indicators')
    class NativeIndicatorRequired:
        def __init__(self, *args, **kwargs):
            raise AssertionError('本离线检查不构造原生 EMA；数值回归须在 Bomber 环境执行')
    indicators.ExponentialMovingAverage = NativeIndicatorRequired
    sys.modules[indicators.__name__] = indicators

ema_live = import_module('demos.01_main_ema.run_live')
ema_history = import_module('demos.01_main_ema.history')
ema_recovery = import_module('demos.01_main_ema.recovery')


class EmaForeverEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.args = ema_live.parse_args(['--connect', '--mode', 'simnow', '--product', 'CU',
            '--run-forever', '--enable-orders', '--confirm-simnow', '--no-session-overrides',
            '--calendar-id', 'china.sse',
            '--state-file', str(Path(self.temp.name)/'state.json')])

    def test_builder_projects_window_without_mutating_original_cli(self):
        cal = calendar()
        window = cal.window(datetime(2026,10,28,14,tzinfo=ZONE))
        context = LiveWindowContext(cal, window, Event(), '20261027', max_session_orders=3)
        controller = NS(driver=Mock())
        with patch.object(ema_recovery, 'EmaCheckpointLifecycle', return_value=controller) as lifecycle, \
                patch.object(ema_live, 'build_simnow_transport', return_value=Mock()), \
                patch.object(ema_live, 'required', return_value='fixture'):
            runtime = ema_live.build_runtime(self.args, window_context=context)
        self.assertIs(runtime.controller, controller)
        self.assertIs(runtime.window_context, context)
        self.assertEqual(lifecycle.call_args.kwargs['expected_trading_day'], '20261028')
        self.assertEqual(lifecycle.call_args.kwargs['max_session_orders'], 3)
        self.assertIs(lifecycle.call_args.kwargs['window_context'], context)
        self.assertIsNone(self.args.expected_trading_day)
        self.assertEqual(self.args.max_session_orders, 4)
        self.assertFalse(hasattr(self.args, '_window_context'))
        self.assertFalse(self.args.state_file.exists())

    def test_cli_uses_public_entry_with_existing_daily_limit_policy(self):
        public = import_module('bomber.framework.trader.runtime.live.continuous')
        with patch.object(ema_live, 'parse_args', return_value=self.args), \
                patch.object(ema_live, 'load_dotenv'), patch.object(ema_live, 'runtime_sessions', return_value=calendar()), \
                patch.object(ema_live, 'required', return_value='fixture'), \
                patch.object(public, 'run_continuous', return_value={'status':'completed'}) as run, \
                redirect_stdout(StringIO()):
            self.assertEqual(ema_live.main(), {'status':'completed'})
        self.assertIs(run.call_args.args[0].func, ema_live.build_runtime)
        self.assertIs(run.call_args.args[0].args[0], self.args)
        self.assertEqual(run.call_args.kwargs['order_limit_policy'], 'wait')
        self.assertEqual(run.call_args.kwargs['max_session_orders'], 4)

    def test_closed_minute_warms_only_and_never_dispatches_a_target(self):
        runner = object.__new__(ema_history.CheckpointedRunner)
        runner._submit_lock = RLock()
        client = Mock(_submit_lock=RLock())
        runner._clients = {'fixture':client}
        runner.accept_bars = True
        runner.first_complete_bar_ns = 0
        runner.history_service = None
        runner.recovery_bars = None
        runner.calendar = calendar()
        now = datetime(2026,10,28,11,30,tzinfo=ZONE)
        window = runner.calendar.window(datetime(2026,10,28,11,29,tzinfo=ZONE))
        runner.window_context = LiveWindowContext(runner.calendar,window,Event(),'20261027',clock=lambda:now)
        state = NS(bars_used=1,last_processed_ns=ns('2026-10-28 11:29')-1,config=NS(product='CU'))
        runner.ema_checkpoint = NS(strategy=state,warm_closed=Mock())
        runner.references = NS(snapshot=lambda _:NS(factor=lambda *_:Decimal(1)))
        runner.manager = Mock()
        stamp=ns('2026-10-28 11:30')-1
        bar=base.Bar(ts_event=stamp,close=NS(as_decimal=lambda:Decimal(100)))
        runner.publish('fixture',bar)
        self.assertFalse(runner.accept_bars)
        runner.ema_checkpoint.warm_closed.assert_called_once_with(stamp,Decimal(100))
        client.submit_targets.assert_not_called()
        runner.manager.save.assert_not_called()

    def test_begin_after_close_does_not_warm_or_open_decisions(self):
        runner = object.__new__(ema_history.CheckpointedRunner)
        runner._submit_lock = RLock()
        runner._clients = {'fixture':Mock()}
        runner.accept_bars = False
        cal=calendar();now=datetime(2026,10,28,11,30,tzinfo=ZONE)
        runner.window_context=LiveWindowContext(cal,cal.window(datetime(2026,10,28,11,29,tzinfo=ZONE)),
            Event(),'20261027',clock=lambda:now)
        with patch.object(ema_history,'flush_buffer') as flush:
            runner.begin_bars()
        flush.assert_not_called()
        self.assertFalse(runner.accept_bars)


if __name__ == '__main__':
    names = [
        'tests.test_live_continuous', 'tests.test_hybrid_continuous',
        'tests.test_continuous_position_runtime', 'tests.test_shared_continuous',
        'tests.test_demo_hybrid_live', 'tests.test_hybrid_history_source',
        'tests.run_hybrid_live_offline',
    ]
    suite=unittest.defaultTestLoader.loadTestsFromNames(names)
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(EmaForeverEntryTests))
    # 选用不需要原生指标的既有 EMA 日历、额度、恢复门控及收尾回归。
    prefix='tests.test_main_ema_continuous.ContinuousTests.'
    suite.addTests(unittest.defaultTestLoader.loadTestsFromNames([prefix+name for name in (
        'test_intraday_pause_and_open_boundaries',
        'test_friday_night_belongs_to_monday_and_saturday_tail',
        'test_holidays_and_previous_source_day',
        'test_unknown_calendar_and_naive_clock_fail',
        'test_missing_minutes_excludes_break',
        'test_daily_order_budget_includes_terminal_orders',
        'test_cross_day_empty_orders_requires_current_authoritative_query',
        'test_shutdown_bar_failure_still_runs_cancel_query_and_release',
        'test_supervisor_waits_through_rest_and_stops_after_failed_worker',
    )]))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
