"""真实 Runtime/Manager/JSON 的启动和恢复事务；SDK 仅使用已有实体夹具。"""
from pathlib import Path
from importlib import import_module
from contextlib import ExitStack
from tempfile import TemporaryDirectory
from threading import Event, RLock
from types import SimpleNamespace as NS
from unittest.mock import Mock
import unittest

from tests import run_live_history_offline as fixtures
from bomber.framework.trader.contracts import RuntimeMode, TargetPortfolio
from bomber.framework.trader.execution.order_state import OrderReportStateMachine
from bomber.framework.trader.persistence import JsonStateStore, RuntimeStateManager, StatePersistenceError
from bomber.framework.trader.portfolio import AccountTargetKey, PositionManager, PortfolioCoordinator, TargetStore
from bomber.framework.trader.runtime.live.continuous import LiveWindowContext
from bomber.framework.trader.runtime.live.lifecycle import activate_live_session, shutdown_local_session
from bomber.framework.trader.runtime.live.local import LocalExecutionLifecycle, RecordingExecutionLifecycle, strategy_state_lock
from bomber.framework.trader.runtime.live.recovery import StrategyPositionBinding
from bomber.framework.trader.runtime.live.runtime import ManagedLiveRuntime
from tests.test_demo_hybrid_live import calendar, ZONE
from datetime import datetime


class Component:
    def __init__(self):
        self.value, self.invalid = 0, False
    def snapshot_state(self):
        return {"value": self.value}
    def restore_state(self, state):
        self.value = state["value"]
    def validate_targets(self, store):
        if self.invalid:
            raise ValueError("component target mismatch")


class Report:
    path = None
    def begin(self):
        self.path = "memory"
    def write(self, *, status, fields, **kwargs):
        return {"status": status, **fields}


class LiveLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.trace = []

    def build(self, *, resume=False):
        component = Component()
        positions, targets, portfolio = PositionManager(), TargetStore(), PortfolioCoordinator()
        machine = OrderReportStateMachine("fixture")
        manager = RuntimeStateManager(JsonStateStore(self.path), targets, portfolio, positions,
            order_machines={"fixture": machine}, state_components={"signal": component})
        runner = NS(mode=RuntimeMode.LIVE, _submit_lock=RLock(), accept_bars=False, failure=None,
            fixed_spec="v1", target_store=targets, portfolio_coordinator=portfolio, position_manager=positions,
            start=Mock(side_effect=lambda: self.trace.append("runner.start")), stop=Mock(),
            session_ready=lambda: True, begin_bars=Mock(), mark_history_live_start=Mock())
        def begin():
            self.trace.append("begin")
            runner.accept_bars = True
        runner.begin_bars.side_effect = begin
        client = NS(client_id="fixture", position_manager=positions, order_state_machine=machine,
            risk_manager=NS(instrument_limits={"RB.SHFE": NS(max_abs_position=1)}),
            backend=NS(reports=()), report_errors=(), disarm=Mock(), arm_demo=Mock())
        references = NS(spec="v1", refresh=Mock())
        strategy = NS(strategy_id="demo", restore_target_revision=Mock())
        session = NS(runner=runner, strategy=strategy, client=client, references=references,
            manager=manager, upstream=NS(latest_trading_day="20261028"),
            recovery_binding=StrategyPositionBinding("demo", "fixture", {"main": "RB.SHFE"}))
        controller = LocalExecutionLifecycle("20261028", resume=resume, state_file=self.path)
        controller.poll, controller.verify = Mock(return_value=False), Mock()
        runtime = ManagedLiveRuntime("fixture", controller=controller,
            prepare_inputs=lambda _: None, assemble_session=lambda *_: session,
            report=Report(), seconds=1, sleep=lambda _: None)
        return runtime, session, controller, component

    def seed(self):
        runtime, session, _, component = self.build()
        component.value = 7
        runtime.run()
        return self.path.read_bytes(), session.manager.generation

    def test_fresh_history_precedes_checkpoint_and_decision_gate(self):
        runtime, session, controller, component = self.build()
        def prepare():
            self.assertFalse(session.runner.accept_bars)
            self.assertFalse(self.path.exists())
            self.trace.append("history")
            component.value = 3
        session.runner.prepare_history = prepare
        result = runtime.run()
        self.assertEqual(self.trace, ["runner.start", "history", "begin"])
        self.assertEqual(result["status"], "passed")
        self.assertTrue(controller.resume_ready)
        self.assertEqual(JsonStateStore(self.path).load().payload["component_states"]["signal"], {"value": 3})
        self.assertEqual(result["checkpoint"]["generation"], session.manager.generation)
        self.assertEqual(result["checkpoint"]["components"], ["signal"])

    def test_restore_uses_existing_state_and_continues_generation(self):
        _, generation = self.seed()
        runtime, session, controller, component = self.build(resume=True)
        self.assertEqual(component.value, 0)
        runtime.run()
        self.assertEqual(component.value, 7)
        self.assertEqual(controller.restored_generation, generation)
        session.strategy.restore_target_revision.assert_called_once_with(0)
        self.assertGreater(session.manager.generation, generation)

    def test_missing_restore_does_not_create_an_empty_checkpoint(self):
        runtime, session, controller, _ = self.build(resume=True)
        with self.assertRaisesRegex(RuntimeError, "不存在"):
            runtime.run()
        self.assertFalse(self.path.exists())
        self.assertFalse(controller.resume_ready)
        session.runner.begin_bars.assert_not_called()
        session.runner.stop.assert_called_once()

    def test_history_failure_on_restore_preserves_original_bytes(self):
        original, generation = self.seed()
        runtime, session, controller, _ = self.build(resume=True)
        session.runner.prepare_history = Mock(side_effect=RuntimeError("history failed"))
        with self.assertRaisesRegex(RuntimeError, "history failed"):
            runtime.run()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(session.manager.generation, generation)
        self.assertFalse(controller.resume_ready)
        session.runner.begin_bars.assert_not_called()
        self.assertTrue(runtime.result["resume_state_preserved"])

    def test_fresh_history_failure_does_not_save_partial_initialization(self):
        runtime, session, controller, component = self.build()
        def fail():
            component.value = 4
            raise RuntimeError("history failed")
        session.runner.prepare_history = fail
        with self.assertRaisesRegex(RuntimeError, "history failed"):
            runtime.run()
        self.assertFalse(self.path.exists())
        self.assertFalse(controller.resume_ready)
        session.runner.begin_bars.assert_not_called()

    def test_hybrid_model_field_validation_happens_before_checkpoint_commit(self):
        from tests.test_live_history import service
        from tests.test_demo_hybrid_live import ns
        from bomber.framework.dataprep.history import HistoryBar, HistoryError
        hybrid = import_module("demos.10_hybrid_transformer.run_live")
        runtime, session, controller, _ = self.build()
        runner = session.runner
        runner.strategy = session.strategy
        runner.calendar, runner.bound_instrument = calendar(), "RB.SHFE"
        runner._clock_ns = lambda: ns("2026-10-28 09:03")
        runner.first_complete_bar_ns, runner.history_minutes = 0, 2
        stamps = runner.calendar.last_minutes(runner._clock_ns(), 2)
        runner.history_service = service(tuple(HistoryBar("RB.SHFE", stamp, close=100) for stamp in stamps))
        runner.bar_archive, runner.resuming = None, False
        runner.pause_requested = lambda: False
        runner.prepare_history = hybrid.HybridLiveRunner.prepare_history.__get__(runner)
        runner.mark_history_live_start = hybrid.HybridLiveRunner.mark_history_live_start.__get__(runner)
        session.strategy.config = NS(product="RB", contract_multiplier=10)
        session.strategy.predictor = NS(warm_history=Mock())
        with self.assertRaisesRegex(HistoryError, "原始OHLCV"):
            runtime.run()
        self.assertFalse(self.path.exists())
        self.assertFalse(controller.resume_ready)
        session.strategy.predictor.warm_history.assert_not_called()
        runner.begin_bars.assert_not_called()

    def test_changed_reference_after_history_never_opens_or_saves(self):
        runtime, session, controller, _ = self.build()
        session.runner.prepare_history = lambda: setattr(session.references, "spec", "v2")
        with self.assertRaisesRegex(RuntimeError, "参考"):
            runtime.run()
        self.assertFalse(self.path.exists())
        self.assertFalse(controller.resume_ready)
        self.assertFalse(session.runner.accept_bars)

    def test_stable_component_mismatch_blocks_commit_and_authorization(self):
        _, session, controller, component = self.build()
        component.invalid = True
        session.runner.accept_bars = True
        with self.assertRaisesRegex(ValueError, "target mismatch"):
            activate_live_session(controller, session, authorize=session.client.arm_demo)
        session.client.arm_demo.assert_not_called()
        session.runner.begin_bars.assert_not_called()
        self.assertFalse(controller.resume_ready)
        self.assertFalse(self.path.exists())
        self.assertFalse(session.runner.accept_bars)

    def test_portfolio_revision_mismatch_blocks_stable_checkpoint_commit(self):
        _, session, controller, _ = self.build()
        session.runner.target_store.apply(TargetPortfolio("demo", 1, 100, {"main": 1}))
        session.runner.portfolio_coordinator.update(strategy_id="demo", revision=2, ts_event=100,
            targets={AccountTargetKey("fixture", "RB.SHFE"): 1})
        with self.assertRaisesRegex(RuntimeError, "组合版本"):
            activate_live_session(controller, session, authorize=session.client.arm_demo)
        session.client.arm_demo.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_staged_restore_commits_after_preparation_and_keeps_original_backup(self):
        original, generation = self.seed()
        runtime, session, controller, _ = self.build(resume=True)
        controller.restore_session = lambda *_: session.manager.restore(payload_transform=lambda payload: payload)
        def check_staged():
            self.assertTrue(session.manager.describe_checkpoint()["restore_pending"])
            self.assertEqual(self.path.read_bytes(), original)
        session.runner.prepare_history = check_staged
        runtime.run()
        self.assertFalse(session.manager.describe_checkpoint()["restore_pending"])
        backup = self.path.with_name(self.path.name + f".before-rollover-g{generation}.json")
        self.assertEqual(backup.read_bytes(), original)

    def test_staged_restore_preparation_failure_keeps_write_gate_and_original(self):
        original, generation = self.seed()
        runtime, session, controller, _ = self.build(resume=True)
        controller.restore_session = lambda *_: session.manager.restore(payload_transform=lambda payload: payload)
        session.runner.prepare_history = Mock(side_effect=RuntimeError("history failed"))
        with self.assertRaisesRegex(RuntimeError, "history failed"):
            runtime.run()
        self.assertTrue(session.manager.describe_checkpoint()["restore_pending"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(session.manager.generation, generation)

    def test_wrong_manager_binding_is_rejected_before_start_or_cleanup_save(self):
        runtime, session, controller, _ = self.build()
        session.manager.target_store = TargetStore()
        with self.assertRaisesRegex(StatePersistenceError, "共享目标"):
            runtime.run()
        self.assertFalse(self.path.exists())
        session.runner.start.assert_not_called()
        session.runner.begin_bars.assert_not_called()
        session.runner.stop.assert_called_once()
        self.assertFalse(controller.resume_ready)

    def test_wrong_order_machine_binding_is_rejected_before_start(self):
        runtime, session, _, _ = self.build()
        session.manager.order_machines["fixture"] = OrderReportStateMachine("fixture")
        with self.assertRaisesRegex(StatePersistenceError, "订单状态机"):
            runtime.run()
        self.assertFalse(self.path.exists())
        session.runner.start.assert_not_called()

    def test_direct_controller_rejects_wrong_binding_before_restoring_components(self):
        original, _ = self.seed()
        _, session, controller, component = self.build(resume=True)
        session.manager.target_store = TargetStore()
        with self.assertRaises(StatePersistenceError):
            controller.start(session, NS(trading_day="20261028"))
        self.assertEqual(component.value, 0)
        self.assertEqual(self.path.read_bytes(), original)
        session.runner.start.assert_not_called()

    def test_stop_during_preparation_does_not_commit_or_authorize(self):
        _, session, controller, _ = self.build()
        stop = Event()
        now = datetime(2026, 10, 28, 10, tzinfo=ZONE)
        cal = calendar()
        session.runner.window_context = LiveWindowContext(cal, cal.window(now), stop, "20261027", clock=lambda: now)
        session.runner.prepare_history = stop.set
        controller.start(session, NS(trading_day="20261028"))
        self.assertFalse(self.path.exists())
        self.assertFalse(controller.resume_ready)
        self.assertFalse(session.runner.accept_bars)
        session.runner.begin_bars.assert_not_called()

    def test_recording_shutdown_does_not_require_backend_or_persistence(self):
        _, session, _, _ = self.build()
        controller = RecordingExecutionLifecycle("20261028")
        session.client = NS(requests=(), is_started=True)
        result = controller.shutdown(session, None)
        self.assertEqual(result["cleanup_errors"], [])
        self.assertFalse(self.path.exists())
        session.runner.stop.assert_called_once()
        self.assertEqual(controller.snapshot(session, None)["observed_md_trading_day"], "20261028")

    def test_save_failure_still_releases_runner_and_reports_cleanup_error(self):
        _, session, _, _ = self.build()
        session.manager.save = Mock(side_effect=OSError("disk failed"))
        result = shutdown_local_session(session, save_state=True)
        session.runner.stop.assert_called_once()
        self.assertEqual(result["cleanup_errors"], ["disk failed"])
        self.assertFalse(session.runner.accept_bars)

    def test_primary_failure_and_shutdown_failure_are_both_retained(self):
        runtime, session, controller, _ = self.build()
        controller.poll.side_effect = OSError("market failed")
        session.runner.stop.side_effect = RuntimeError("stop failed")
        with self.assertRaisesRegex(OSError, "market failed") as raised:
            runtime.run()
        self.assertTrue(any("stop failed" in note for note in raised.exception.__notes__))
        self.assertEqual(runtime.result["failure"], "market failed")
        self.assertIn("stop failed", runtime.result["cleanup_errors"])
        self.assertEqual(runtime.result["status"], "failed")

    def test_component_contract_is_checked_when_manager_is_built(self):
        for components, error in (({"bad": object()}, TypeError), ({"": Component()}, ValueError)):
            with self.assertRaises(error):
                RuntimeStateManager(JsonStateStore(self.path), TargetStore(), PortfolioCoordinator(),
                    PositionManager(), state_components=components)
        self.assertFalse(self.path.exists())

    def test_ctp_state_file_lock_is_checked_before_account_login(self):
        from tests.test_live_recovery import Lifecycle
        controller = Lifecycle(resume=True, state_file=self.path)
        with strategy_state_lock(self.path), ExitStack() as resources:
            with self.assertRaisesRegex(RuntimeError, "已有进程"):
                controller.prepare(resources)
        self.assertEqual(controller.calls, [])

    def test_fresh_ctp_uncommitted_cleanup_does_not_save_or_cancel(self):
        from tests.test_live_recovery import RecoveryTests
        fixture = RecoveryTests()
        self.addCleanup(fixture.doCleanups)
        _, controller, session, _, calls = fixture.scoped_controller()
        result = controller.shutdown(session, None)
        self.assertEqual(result["cleanup_errors"], [])
        self.assertNotIn("save", calls)
        self.assertNotIn("cancel.own", calls)
        self.assertIn("runner.stop", calls)
        self.assertIn("td.stop", calls)


if __name__ == "__main__":
    unittest.main()
