"""验证实际08帧/决策分支的诊断；日志不能制造委托或改变交易结果。"""
import json
import unittest
from unittest.mock import patch
from tests import test_option_vega_assembly as fixture


class MinuteDiagnosticsTests(unittest.TestCase):
    setUp = fixture.AssemblyTests.setUp
    build = fixture.AssemblyTests.build
    bars = fixture.AssemblyTests.bars
    start = fixture.AssemblyTests.start

    def rows(self, logs, title):
        return [json.loads(message.split(title+": ", 1)[1]) for message in logs.output if title+": " in message]

    def test_complete_frame_no_target_change_and_wait_slot_are_visible(self):
        s, lifecycle = self.build(mode="recording")
        self.start(s, lifecycle)
        with self.assertLogs("OptionMarketInputs", level="INFO") as logs:
            self.bars(s, "2026-08-13 13:57")
        market = self.rows(logs, "分钟行情汇总")[0]
        decision = self.rows(logs, "分钟决策汇总")[0]
        self.assertEqual(market["status"], "COMPLETE")
        self.assertEqual(market["synchronous_option_groups"], 1)
        self.assertTrue(market["required"]["000852"]["same_minute"])
        self.assertTrue(market["minute"].endswith("+08:00"))
        self.assertEqual(decision["status"], "NO_TARGET_CHANGE")
        self.assertEqual(decision["entry"], "WAIT_SLOT")
        self.assertEqual(decision["target_submissions_total"], 0)
        self.assertFalse(s.client.requests)
        self.assertEqual(s.runner.frame_audit[-1]["decision"], decision)

    def test_entry_target_logged_without_claiming_fill(self):
        s, lifecycle = self.build(mode="recording")
        self.start(s, lifecycle)
        with self.assertLogs("OptionMarketInputs", level="INFO") as logs:
            self.bars(s, "2026-08-13 13:58")
        decision = self.rows(logs, "分钟决策汇总")[0]
        self.assertEqual(decision["status"], "TARGET_SUBMITTED")
        self.assertEqual(decision["entry"], "TARGET")
        self.assertEqual(decision["fills_received_total"], 0)
        self.assertEqual(len(s.client.requests), 1)

    def test_missing_index_skips_with_market_and_decision_evidence(self):
        s, lifecycle = self.build(mode="recording", dfs_index=True)
        self.start(s, lifecycle)
        with self.assertLogs("OptionMarketInputs", level="INFO") as logs:
            self.bars(s, "2026-08-13 13:58", omit=("000852",))
        market = self.rows(logs, "分钟行情汇总")[0]
        decision = self.rows(logs, "分钟决策汇总")[0]
        self.assertEqual(market["missing_required"], ["000852"])
        self.assertEqual(decision["status"], "SKIPPED_INDEX")
        self.assertEqual(s.strategy.frames, 0)
        self.assertFalse(s.client.requests)

    def test_restored_held_leg_wait_then_exit_check_is_visible(self):
        s, lifecycle = self.build()
        self.start(s, lifecycle)
        self.bars(s, "2026-08-13 13:58")
        s.strategy.restored = True
        s.strategy.latest.clear()
        with self.assertLogs("OptionMarketInputs", level="INFO") as logs:
            self.bars(s, "2026-08-13 13:59", omit=("MO2609-C-6600", "IM2609"))
        market = self.rows(logs, "分钟行情汇总")[0]
        decision = self.rows(logs, "分钟决策汇总")[0]
        self.assertEqual(market["status"], "INCOMPLETE")
        self.assertEqual(set(market["missing_required"]), {"MO2609-C-6600", "IM2609"})
        self.assertEqual(decision["status"], "RESUME_WAIT")
        with self.assertLogs("OptionMarketInputs", level="INFO") as logs:
            self.bars(s, "2026-08-13 14:00")
        decision = self.rows(logs, "分钟决策汇总")[0]
        self.assertNotEqual(decision["status"], "RESUME_WAIT")
        self.assertTrue(decision["exit_checks"])
        self.assertFalse(decision["exit_checks"][0]["should_close"])
        self.assertEqual(decision["price_cache"]["MO2609-C-6600"]["age_seconds"], 0)

    def test_decision_error_is_logged_and_original_failure_still_propagates(self):
        s, lifecycle = self.build(mode="recording")
        self.start(s, lifecycle)
        with patch.object(s.strategy, "_frame", side_effect=RuntimeError("fixture pricing failure")), \
                self.assertLogs("OptionMarketInputs", level="INFO") as logs:
            with self.assertRaisesRegex(RuntimeError, "fixture pricing failure"):
                self.bars(s, "2026-08-13 13:58")
        decision = self.rows(logs, "分钟决策汇总")[0]
        self.assertEqual(decision["status"], "ERROR")
        self.assertEqual(s.runner.frame_audit[-1]["status"], "FAILED")
        self.assertFalse(s.runner.accept_bars)
        self.assertFalse(s.client.requests)

    def test_logging_enablement_does_not_change_targets(self):
        requests = []
        for disabled in (True, False):
            s, lifecycle = self.build(mode="recording")
            self.start(s, lifecycle)
            with patch.object(fixture.runtime.logger, "disabled", disabled):
                self.bars(s, "2026-08-13 13:58")
            requests.append([(dict(r.targets), r.metadata["signal_ts"]) for r in s.client.requests])
        self.assertEqual(requests[0], requests[1])

    def test_heartbeat_is_once_per_wall_minute_without_creating_decision(self):
        s, lifecycle = self.build(mode="recording")
        self.start(s, lifecycle)
        with self.assertLogs("OptionMarketInputs", level="INFO") as logs:
            s.runner.poll_live_inputs()
            s.runner.poll_live_inputs()
            self.now += 60_000_000_000
            s.runner.poll_live_inputs()
        heartbeat = self.rows(logs, "分钟运行心跳")
        self.assertEqual(len(heartbeat), 2)
        self.assertIsNone(heartbeat[0]["last_closed_minute"])
        self.assertEqual(heartbeat[-1]["strategy_frames"], 0)
        self.assertFalse(s.client.requests)


if __name__ == "__main__":
    unittest.main()
