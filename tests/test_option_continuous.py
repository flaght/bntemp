"""08常驻：真实调度与恢复组件，注入时钟和行情；不连接数据库或CTP。"""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from copy import deepcopy
from datetime import date, datetime, timedelta
from importlib import import_module
from io import StringIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
import unittest

from bomber.framework.calendars.hours import SessionSegment, TradingHoursSnapshot
from bomber.framework.calendars.sessions import SessionBinding, SessionCalendar
from bomber.framework.calendars.trading import CalendarSnapshot
from bomber.framework.trader.runtime.live.continuous import LiveWindowContext, run_continuous
from bomber.framework.trader.runtime.live.contracts import LiveRunContext
from bomber.framework.trader.runtime.live.recovery import SessionIdentityCheckpoint
from tests.test_demo_option_vega import ns

entry = import_module("demos.08_option_vega.run_live")
assembly = import_module("tests.test_option_vega_assembly")
recovery = import_module("demos.08_option_vega.recovery")
ZONE = ZoneInfo("Asia/Shanghai")


def dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZONE)


def calendar():
    dates = CalendarSnapshot("china.cffex", "fixture", date(2026, 8, 12), date(2026, 8, 17),
        (date(2026, 8, 12), date(2026, 8, 13), date(2026, 8, 14), date(2026, 8, 17)), "fixture")
    hours = TradingHoursSnapshot(tuple(("MO", SessionSegment(sid, "TRADING_DAY", 0, start, 0, end))
        for sid, start, end in (("am", "09:30", "11:30"), ("pm", "13:00", "14:57"))), "fixture")
    return SessionCalendar(hours, dates, SessionBinding("MO", "MO", "china.cffex", "Asia/Shanghai",
        "CFFEX", "OPTION"), overrides=())


class OptionContinuousTests(unittest.TestCase):
    setUp = assembly.AssemblyTests.setUp
    build = assembly.AssemblyTests.build
    bars = assembly.AssemblyTests.bars
    start = assembly.AssemblyTests.start

    def args(self, *extras):
        config = Path(__file__).resolve().parents[1] / "demos/08_option_vega/config/strategy.yaml"
        return entry.parse_args(["--config-file", str(config), "--config-id", "live_recording", *extras])

    def test_cli_timed_requires_day_and_forever_loads_option_hours(self):
        args = self.args()
        self.assertTrue(args.run_forever)
        self.assertIsNone(args.expected_trading_day)
        inputs = entry.session_inputs(args)
        self.assertEqual((inputs.product, inputs.hours_code, inputs.asset_type), ("MO", "MO", "OPTION"))
        self.assertEqual(inputs.database, "dfs://bomber_static")
        self.assertFalse(self.args("--no-run-forever", "--expected-trading-day", "20261106").run_forever)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            self.args("--no-run-forever")
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            self.args("--simnow-environment", "replay", "--replay-md-trading-day", "20260813")

    def test_calendar_rejects_im_hours_and_keeps_mo_1457_boundary(self):
        cal = calendar()
        entry.validate_calendar(cal, "MO")
        self.assertIsNone(cal.window(dt("2026-08-13 14:57")))
        cal.binding = SessionBinding("MO", "IM", "china.cffex", "Asia/Shanghai", "CFFEX", "OPTION")
        with self.assertRaisesRegex(ValueError, "不能使用期货时段"):
            entry.validate_calendar(cal, "MO")

    def test_runtime_uses_each_window_day_without_mutating_startup_args(self):
        args = self.args()
        window = calendar().window(dt("2026-08-14 13:05"))
        context = LiveWindowContext(calendar(), window, Event(), "20260813", resume=True)
        worker = entry.build_runtime(args, window_context=context)
        self.assertEqual(worker.controller.trading_day, "20260814")
        self.assertEqual(worker.controller.valuation_day, "20260814")
        self.assertTrue(worker.controller.resuming)
        self.assertEqual(worker.report.trading_day, "20260814")
        self.assertIsNone(args.expected_trading_day)
        self.assertFalse(args.resume)

    def test_preopen_lunch_close_and_next_day_use_public_scheduler(self):
        now, stop, seen = [dt("2026-08-13 09:10")], Event(), []
        def factory(*, window_context):
            self.assertGreaterEqual(now[0], window_context.window.start)
            worker = entry.build_runtime(self.args(), window_context=window_context)
            seen.append((now[0], worker.report.trading_day, window_context.state))
            def run():
                now[0] = window_context.window.end
                if len(seen) == 3:
                    stop.set()
                return {"status": "passed"}
            worker.run = run
            worker.session = NS(window_components={"recording": NS(snapshot_state=lambda: {"sent_days": ["2026-08-13"]})})
            return worker
        with redirect_stdout(StringIO()):
            result = run_continuous(factory, calendar=calendar(), stop_event=stop, clock=lambda: now[0],
                wait=lambda _: now.__setitem__(0, now[0]+timedelta(minutes=1)))
        self.assertEqual([row[1] for row in seen], ["20260813", "20260813", "20260814"])
        self.assertEqual(seen[0][0].strftime("%H:%M"), "09:30")
        self.assertEqual(seen[1][0].strftime("%H:%M"), "13:00")
        self.assertEqual(seen[1][2], {"recording": {"sent_days": ["2026-08-13"]}})
        self.assertTrue(result["stopped_by_operator"])

    def test_recording_resumes_selection_without_replaying_or_fabricating_fills(self):
        first, control = self.build(mode="recording")
        self.start(first, control)
        self.bars(first, "2026-08-13 13:58")
        saved = first.window_components["recording"].snapshot_state()
        control.shutdown(first, LiveRunContext("20261009", ExitStack()))
        second, control = self.build(mode="recording")
        second.window_components["recording"].restore_state(saved)
        self.start(second, control)
        self.bars(second, "2026-08-13 13:58")
        self.assertEqual(second.strategy.entry_decisions, first.strategy.entry_decisions)
        self.assertIn(date(2026, 8, 13), second.strategy.sent_days)
        self.assertEqual(second.strategy.option_goals, first.strategy.option_goals)
        self.assertEqual(len(second.client.requests), 0)
        self.assertEqual(second.strategy.fills_received, 0)
        self.assertFalse(self.path.exists())

    def test_boundary_and_operator_stop_precede_health_and_callback_dispatch(self):
        for when, stopped in (("2026-08-13 11:30", False), ("2026-08-13 14:57", False),
                              ("2026-08-13 10:00", True)):
            with self.subTest(when=when, stopped=stopped):
                session, control = self.build(mode="recording")
                cal, stop = calendar(), Event()
                if stopped:
                    stop.set()
                now = dt(when)
                window = cal.window(now-timedelta(minutes=1))
                session.runner.window_context = LiveWindowContext(cal, window, stop, "20260812", clock=lambda: now)
                session.runner.accept_bars = True
                session.runner.readiness = Mock(side_effect=AssertionError("休市后不能查健康"))
                session.runner.poll_live_inputs()
                session.runner.flush_frames()
                self.bars(session, "2026-08-12 13:58", flush=False)  # 旧日回调亦不触发日期错误。
                self.assertFalse(session.runner.accept_bars)
                self.assertIsNone(session.runner.failure)
                self.assertEqual(session.strategy.frames, 0)
                control.verify(session)
                session.runner.failure = "actual fault"
                with self.assertRaisesRegex(RuntimeError, "actual fault"):
                    control.verify(session)

    def test_recording_can_remove_delisted_observation_targets_without_adopting_positions(self):
        session, _ = self.build(mode="recording")
        saved = session.window_components["recording"].snapshot_state()
        saved["option_goals"]["MO2608-C-6600"] = "-1"
        saved["future_goals"]["IM2608"] = "1"
        original = deepcopy(saved)
        session.window_components["recording"].restore_state(saved)
        self.assertNotIn("MO2608-C-6600", session.strategy.option_goals)
        self.assertNotIn("IM2608", session.strategy.future_goals)
        self.assertEqual(saved, original)
        self.assertFalse(session.runner.position_manager.snapshot().strategy_positions)

    def set_day(self, session, day):
        session.session_identity.update(td_day=day, valuation_day=datetime.strptime(day, "%Y%m%d").date().isoformat(),
            environment="realtime", md_day=None, sources={"market": {"channel": "dolphindb"}, "index": {}},
            calendar_manifest={"calendar_id": "china.cffex"},
            labels={"market-bars": "end", "index-bars": "end"})
        session.manager.state_components["session_identity"] = SessionIdentityCheckpoint(session.session_identity)

    def test_model_cross_day_preserves_owned_fills_and_commits_backup_after_validation(self):
        first, control = self.build()
        self.set_day(first, "20260813")
        self.start(first, control)
        self.bars(first, "2026-08-13 13:58")
        quantity = first.strategy.position("MO2609-C-6600.CFFEX")
        self.assertLess(quantity, 0)
        control.shutdown(first, LiveRunContext("20260813", ExitStack()))
        original = self.path.read_bytes()
        second, _ = self.build(resume=True)
        self.set_day(second, "20260814")
        control = recovery.OptionLocalLifecycle("20260814", mode="model", valuation_day="20260814",
            resume=True, state_file=self.path)
        context = LiveRunContext("20260814", ExitStack())
        control.restore_session(second, context)
        control.validate_restored_session(second, "20260814")
        self.assertEqual(second.runner.position_manager.position(second.strategy.strategy_id,
            "MO2609-C-6600.CFFEX"), quantity)
        self.assertEqual(self.path.read_bytes(), original)  # 尚未开闸提交。
        second.manager.commit_restore()
        backups = list(self.path.parent.glob("state.json.before-rollover-g*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        identity = second.manager.repository.load().payload["component_states"]["session_identity"]["identity"]
        self.assertEqual(identity["td_day"], "20260814")
        saved = second.manager.repository.load().payload
        bad = deepcopy(saved)
        bad["component_states"]["session_identity"]["identity"]["config"]["nav"] = "1"
        second.session_identity["td_day"] = "20260817"
        second.session_identity["valuation_day"] = "2026-08-17"
        with self.assertRaisesRegex(ValueError, "不能改变策略"):
            recovery.model_payload_for_day(second, bad)

    def test_main_delegates_calendar_and_daily_order_policy_without_connecting(self):
        args = self.args()
        with patch.object(entry, "parse_args", return_value=args), \
                patch("dotenv.load_dotenv"), \
                patch("bomber.framework.dataprep.session_inputs.load_session_inputs", return_value=calendar()), \
                patch("bomber.framework.trader.runtime.live.continuous.run_continuous", return_value={"status": "completed"}) as run:
            self.assertEqual(entry.main([]), {"status": "completed"})
        self.assertEqual(run.call_args.kwargs["order_limit_policy"], "wait")
        self.assertIsNone(run.call_args.kwargs["max_session_orders"])


if __name__ == "__main__":
    unittest.main()
