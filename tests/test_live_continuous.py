"""公共 Live 常驻入口及真实 ManagedLiveRuntime 的窗口边界；不连接外部服务。"""
from contextlib import nullcontext, redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from threading import Event, RLock
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import unittest

# 复用已有 SDK 实体夹具；被测调度、资源栈、状态续接和停机都调用真实实现。
from tests.run_hybrid_live_offline import live
from tests.test_demo_hybrid_live import calendar, ZONE
from bomber.framework.trader.contracts import RuntimeMode
from bomber.framework.trader.runtime.live import LiveWindowContext, run_continuous
from bomber.framework.trader.runtime.live.contracts import LiveRunContext
from bomber.framework.trader.runtime.live.runtime import ManagedLiveRuntime


def dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZONE)


class Report:
    path = None
    def begin(self):
        self.path = "fixture"
    def write(self, *, status, fields, **kwargs):
        return {"status": status, **fields}


class CounterState:
    def __init__(self):
        self.value = 0
    def snapshot_state(self):
        return {"value": self.value}
    def restore_state(self, value):
        self.value = value["value"]


class Controller:
    event_label = "fixture"
    def __init__(self, now, *, seen=None, stop=None, windows=0, failure=None):
        self.now, self.seen, self.stop = now, seen, stop
        self.windows, self.failure = windows, failure
        self.polls = self.shutdowns = 0
    def prepare(self, resources):
        return LiveRunContext(self.window.trading_day, resources)
    def finish_preparation(self, context):
        pass
    def start(self, session, context):
        state = session.window_components["counter"]
        if self.seen is not None:
            self.seen.append((self.window.trading_day, state.value))
        state.value += 1
        session.runner.accept_bars = True
    def poll(self, session):
        self.polls += 1
        if self.failure is not None:
            raise RuntimeError(self.failure)
        self.now[0] = self.window.end
        return True
    def verify(self, session):
        if session.runner.failure:
            raise RuntimeError(session.runner.failure)
    def shutdown(self, session, context):
        self.shutdowns += 1
        if self.stop is not None and len(self.seen) >= self.windows:
            self.stop.set()
        return {"cleanup_errors": []}
    def snapshot(self, session, context):
        return {}
    def events(self, session):
        return ()


def worker(context, controller, *, flush=False):
    session = NS(runner=NS(mode=RuntimeMode.LIVE, _submit_lock=RLock(),
                          accept_bars=False, failure=None), client=Mock(),
                 bar_feed=Mock(close_delay_ns=0),
                 window_components={"counter": CounterState()}, flush_completed_bars=flush)
    runtime = ManagedLiveRuntime("fixture", controller=controller,
        prepare_inputs=lambda _: None, assemble_session=lambda *_: session,
        report=Report(), seconds=0.000001, sleep=lambda _: None, window_context=context)
    return runtime, session


class PublicContinuousTests(unittest.TestCase):
    def setUp(self):
        self.calendar = calendar()

    def test_real_runtime_carries_state_across_lunch_and_next_trading_day(self):
        now, stop, seen, workers = [dt("2026-10-28 11:29")], Event(), [], []
        def factory(*, window_context):
            control = Controller(now, seen=seen, stop=stop, windows=3)
            runtime, session = worker(window_context, control)
            workers.append(runtime)
            return runtime
        with redirect_stdout(StringIO()):
            result = run_continuous(factory, calendar=self.calendar, stop_event=stop,
                clock=lambda: now[0], wait=lambda _: now.__setitem__(0, now[0]+timedelta(minutes=1)))
        self.assertEqual(seen, [("20261028", 0), ("20261028", 1), ("20261029", 2)])
        self.assertEqual(result["windows"], 3)
        self.assertTrue(result["stopped_by_operator"])
        self.assertTrue(all(r.controller.polls == 1 and r.controller.shutdowns == 1 for r in workers))
        self.assertTrue(all(r.result["scheduled_pause"] for r in workers))

    def test_closed_operator_stop_never_builds_a_session(self):
        stop, factory = Event(), Mock()
        result = run_continuous(factory, calendar=self.calendar, stop_event=stop,
            clock=lambda: dt("2026-10-28 12:00"), wait=lambda _: stop.set(), status=lambda _: None)
        factory.assert_not_called()
        self.assertEqual(result["windows"], 0)
        self.assertTrue(result["stopped_by_operator"])

    def test_close_is_checked_before_health_and_does_not_flatten(self):
        now = [dt("2026-10-28 11:30")]
        window = self.calendar.window(dt("2026-10-28 11:29"))
        context = LiveWindowContext(self.calendar, window, Event(), "20261027", clock=lambda: now[0])
        control = Controller(now)
        control.poll = Mock(side_effect=AssertionError("休市时不能检查接收心跳"))
        runtime, session = worker(context, control, flush=True)
        with redirect_stdout(StringIO()):
            result = runtime.run()
        control.poll.assert_not_called()
        self.assertFalse(session.runner.accept_bars)
        session.client.disarm.assert_called_once_with("scheduled_pause_or_stop")
        session.client.submit_targets.assert_not_called()
        session.bar_feed.poll.assert_called_once()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["trading_window"]["trading_day"], "20261028")

    def test_operator_stop_inside_active_window_uses_normal_cleanup(self):
        now, stop = [dt("2026-10-28 10:31")], Event()
        window = self.calendar.window(now[0])
        context = LiveWindowContext(self.calendar, window, stop, "20261027", clock=lambda: now[0])
        control = Controller(now)
        def poll(session):
            stop.set()
            return True
        control.poll = poll
        runtime, session = worker(context, control)
        with redirect_stdout(StringIO()):
            self.assertEqual(runtime.run()["status"], "passed")
        self.assertEqual(control.shutdowns, 1)
        self.assertTrue(context.scheduled_pause)
        self.assertEqual(now[0], dt("2026-10-28 10:31"))

    def test_real_health_fault_stops_without_next_window(self):
        now, built = [dt("2026-10-28 14:00")], []
        def factory(*, window_context):
            runtime, _ = worker(window_context, Controller(now, failure="actual stale"))
            built.append(runtime)
            return runtime
        with redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "actual stale"):
            run_continuous(factory, calendar=self.calendar, stop_event=Event(), clock=lambda: now[0])
        self.assertEqual(len(built), 1)
        self.assertEqual(built[0].controller.shutdowns, 1)
        self.assertEqual(built[0].result["status"], "failed")

    def test_cleanup_failure_never_starts_next_window(self):
        now, built = [dt("2026-10-28 14:00")], []
        def factory(*, window_context):
            control = Controller(now)
            control.shutdown = lambda *_: {"cleanup_errors": ["cancel failed"]}
            runtime, _ = worker(window_context, control)
            built.append(runtime)
            return runtime
        with redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "cancel failed"):
            run_continuous(factory, calendar=self.calendar, stop_event=Event(), clock=lambda: now[0])
        self.assertEqual(len(built), 1)

    def test_budget_restore_uses_only_current_trading_day(self):
        saved = NS(payload={"orders": {"client": [
            {"client_order_id": "CTP-fixture-20261027-1"},
            {"client_order_id": "CTP-fixture-20261028-2"}]}})
        factory = Mock(return_value=NS(run=lambda: {"status": "failed"}))
        with patch("bomber.framework.trader.persistence.JsonStateStore.load", return_value=saved), \
                redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "停机核对失败"):
            run_continuous(factory, calendar=self.calendar, state_file="fixture.json", max_session_orders=10,
                owner_lock=nullcontext(), stop_event=Event(), clock=lambda: dt("2026-10-28 14:00"))
        context = factory.call_args.kwargs["window_context"]
        self.assertTrue(context.resume)
        self.assertEqual(context.max_session_orders, 9)
        self.assertEqual(context.source_day, "20261027")

    def test_budget_wait_skips_today_and_runs_next_trading_day(self):
        now, stop, seen = [dt("2026-10-28 14:59")], Event(), []
        saved = NS(payload={"orders": {"client": [{"client_order_id": "CTP-fixture-20261028-1"}]}})
        def factory(*, window_context):
            seen.append(window_context)
            def run():
                stop.set()
                return {"status": "passed"}
            return NS(run=run)
        with patch("bomber.framework.trader.persistence.JsonStateStore.load", return_value=saved), redirect_stdout(StringIO()):
            result = run_continuous(factory, calendar=self.calendar, state_file="fixture.json", max_session_orders=1,
                order_limit_policy="wait", owner_lock=nullcontext(), stop_event=stop, clock=lambda: now[0],
                wait=lambda _: now.__setitem__(0, now[0]+timedelta(minutes=1)))
        self.assertEqual(result["skipped_windows"], 1)
        self.assertEqual(result["windows"], 1)
        self.assertEqual(seen[0].trading_day, "20261029")
        self.assertEqual(seen[0].max_session_orders, 1)

    def test_budget_stop_does_not_build_a_session(self):
        saved = NS(payload={"orders": {"client": [{"client_order_id": "CTP-fixture-20261028-1"}]}})
        factory = Mock()
        with patch("bomber.framework.trader.persistence.JsonStateStore.load", return_value=saved), \
                redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "额度已用完"):
            run_continuous(factory, calendar=self.calendar, state_file="fixture.json", max_session_orders=1,
                owner_lock=nullcontext(), stop_event=Event(), clock=lambda: dt("2026-10-28 14:00"))
        factory.assert_not_called()

    def test_first_window_expected_dates_are_checked_before_factory(self):
        for expected in ({"expected_trading_day": "20261027"}, {"expected_source_day": "20261026"}):
            factory = Mock()
            with redirect_stdout(StringIO()), self.assertRaises(ValueError):
                run_continuous(factory, calendar=self.calendar, stop_event=Event(),
                    clock=lambda: dt("2026-10-28 14:00"), **expected)
            factory.assert_not_called()

    def test_changed_component_set_fails_before_start(self):
        now = [dt("2026-10-28 14:00")]
        context = LiveWindowContext(self.calendar, self.calendar.window(now[0]), Event(), "20261027",
            clock=lambda: now[0], state={"removed": {"value": 9}})
        control = Controller(now)
        control.start = Mock()
        runtime, session = worker(context, control)
        with redirect_stdout(StringIO()), self.assertRaisesRegex(ValueError, "组件集合变化"):
            runtime.run()
        control.start.assert_not_called()
        self.assertEqual(context.state, {"removed": {"value": 9}})
        self.assertEqual(control.shutdowns, 1)

    def test_invalid_order_policy_and_limit_fail_before_connecting(self):
        for options in ({"order_limit_policy": "retry"}, {"max_session_orders": 0}):
            factory = Mock()
            with self.assertRaises(ValueError):
                run_continuous(factory, calendar=self.calendar, **options)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
