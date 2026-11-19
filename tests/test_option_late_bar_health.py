"""实际08 Runner验证迟到Bar只更新健康、不追溯决策；仅SDK/行情/执行为夹具。"""
from copy import deepcopy
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from bomber.framework.market.basic.base import make_bar
from tests import test_option_vega_assembly as fixture
from tests.test_demo_option_vega import ns

runtime = import_module("demos.08_option_vega.live")
signal = import_module("demos.08_option_vega.vega_signal")
OPTION = "MO2609-C-6600"


class LateBarHealthTests(unittest.TestCase):
    setUp = fixture.AssemblyTests.setUp
    build = fixture.AssemblyTests.build
    start = fixture.AssemblyTests.start
    bars = fixture.AssemblyTests.bars

    def setup_session(self, *, held=False):
        self.path = self.path.with_name(f"state-{len(self.sessions)}.json")
        s, lifecycle = self.build(mode="model" if held else "recording", config=signal.OptionVegaConfig(
            option_slot="10:30" if held else "13:58", min_time_value=0,
            max_option_lots=1, max_total_option_lots=2, max_future_lots=1))
        self.start(s, lifecycle)
        s.runner.replay = False
        s.runner.grace_seconds = 20
        s.runner.frames.grace_ns = 20_000_000_000
        s.runner.source_age_ns["market"] = 120_000_000_000
        if held:
            self.now = ns("2026-08-13 10:30")+5_000_000_000
            self.bars(s, "2026-08-13 10:30", flush=False)
            self.now += 20_000_000_001
            s.runner.flush_frames()
            self.assertEqual(s.strategy.position(str(s.references.instrument_ids[OPTION])), -1)
            self.now = ns("2026-08-13 10:31")+5_422_518_217
            self.bars(s, "2026-08-13 10:31", flush=False)
            self.now += 20_000_000_001
            s.runner.flush_frames()
        return s

    def publish(self, s, key, clock, price=40):
        instrument = next(i for i, name in s.runner.key_by_instrument.items() if name == key)
        source = s.runner.sources[key]
        meta = s.feeds[source].get_instrument_meta(instrument)
        s.runner.publish(source, make_bar(instrument_id=instrument, meta=meta,
            open=price, high=price, low=price, close=price, volume=1,
            ts_event=ns(clock), ts_init=self.now, bar_type="1-MINUTE"))

    def missing_1032(self, s, *, flush=True):
        self.now = ns("2026-08-13 10:32")+2_396_661_000
        self.bars(s, "2026-08-13 10:32", omit=(OPTION,), flush=False)
        self.now = ns("2026-08-13 10:32")+22_546_617_000
        if flush:
            s.runner.flush_frames()

    def preparing_with_restored_position(self):
        old = self.setup_session(held=True)
        old.runner.stop()
        self.now = ns("2026-08-13 13:00") + 1_270_000_000
        s, _ = self.build(resume=True, config=old.strategy.config)
        s.manager.restore()
        s.runner.replay = False
        s.runner.source_age_ns["market"] = 300_000_000_000
        s.runner.start()
        self.assertFalse(s.runner.accept_bars)
        self.assertIsNone(s.runner.warm_started_ns)
        self.assertEqual(s.strategy.position(str(s.references.instrument_ids[OPTION])), -1)
        return s

    def test_lunch_tail_is_ignored_until_all_required_inputs_are_fresh_without_rewriting_checkpoint(self):
        s = self.preparing_with_restored_position()
        checkpoint = self.path.read_bytes()
        owned = deepcopy(s.runner.position_manager.attribution.state())
        observations = dict(s.runner.observations)
        prices = {key:s.runner.prices.get(instrument) for key,instrument in s.references.instrument_ids.items()}
        counts = (s.strategy.frames, s.strategy.submissions, s.strategy.fills_received)
        with self.assertLogs("OptionMarketInputs", level="INFO") as logs:
            for key in (OPTION, "IM2609", "000852"):
                self.publish(s, key, "2026-08-13 11:30")
        self.assertIn("闭闸等待新鲜行情", "\n".join(logs.output))
        self.assertEqual(s.runner.ignored_preparation_bars, 3)
        self.assertEqual(s.runner.bar_time_audit[-1]["action"], "IGNORED_PREPARATION")
        self.assertIsNone(s.runner.failure)
        self.assertFalse(s.runner.session_ready())
        self.assertFalse(s.runner.accept_bars)
        self.assertEqual(s.runner.observations, observations)
        self.assertFalse(s.runner.frames.pending or s.runner.frame_audit)
        self.assertEqual(s.runner.position_manager.attribution.state(), owned)
        for key,instrument in s.references.instrument_ids.items():
            self.assertEqual(s.runner.prices.get(instrument),prices[key])
        self.now = ns("2026-08-13 13:01") + 5_000_000_000
        for key in (OPTION, "IM2609", "000852"):
            self.publish(s, key, "2026-08-13 13:01")
            if key != "000852":self.assertFalse(s.runner.session_ready())
        self.assertTrue(s.runner.session_ready())
        fresh = dict(s.runner.observations)
        self.publish(s, OPTION, "2026-08-13 11:30")
        self.assertEqual(s.runner.observations, fresh)
        self.assertTrue(s.runner.session_ready())
        self.assertFalse(s.runner.accept_bars)  # 健康就绪也不自行授权，仍由宿主开放决策。
        self.assertFalse(s.runner.frames.pending or s.runner.frame_audit)
        self.assertEqual((s.strategy.frames,s.strategy.submissions,s.strategy.fills_received),counts)
        self.assertEqual(s.runner.position_manager.attribution.state(),owned)
        self.assertEqual(self.path.read_bytes(),checkpoint)
        s.runner.begin_bars()
        self.assertTrue(s.runner.accept_bars)

    def test_preparation_wait_still_times_out_when_fresh_held_bar_never_arrives(self):
        s = self.preparing_with_restored_position()
        before = self.path.read_bytes()
        self.publish(s,OPTION,"2026-08-13 11:30")
        readiness = import_module("bomber.framework.trader.runtime.live.readiness")
        clock = iter((0,0,0,91))
        with self.assertRaisesRegex(RuntimeError,"90秒内行情/执行未就绪"):
            readiness.wait_for_session_ready(s,90,monotonic=lambda:next(clock),sleep=lambda _:None)
        self.assertFalse(s.runner.accept_bars)
        self.assertIsNone(s.runner.failure)
        self.assertEqual(self.path.read_bytes(),before)

    def test_preparation_future_wrong_day_and_invalid_price_still_fail(self):
        for minute,price,reason in (("2026-08-13 13:01",40,"实时Bar过期或尚未收盘"),
                                    ("2026-08-12 11:30",40,"估值日期不一致"),
                                    ("2026-08-13 11:30",0,"有限正数")):
            with self.subTest(minute=minute,price=price):
                s = self.preparing_with_restored_position()
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError,reason):self.publish(s,OPTION,minute,price)
                self.assertIsNotNone(s.runner.failure)
                self.assertFalse(s.runner.observations or s.runner.accept_bars)
                self.assertEqual(s.runner.ignored_preparation_bars,0)
                self.assertEqual(self.path.read_bytes(),before)

    def test_running_keeps_300_second_held_bar_limit_after_preparation(self):
        s = self.preparing_with_restored_position()
        self.now = ns("2026-08-13 13:01") + 5_000_000_000
        for key in (OPTION,"IM2609","000852"):self.publish(s,key,"2026-08-13 13:01")
        self.assertTrue(s.runner.session_ready())
        s.runner.begin_bars()
        self.now = ns("2026-08-13 13:06")
        for key in ("IM2609","000852"):self.publish(s,key,"2026-08-13 13:06")
        s.runner.poll_live_inputs()  # 刚好300秒仍健康。
        self.now += 1
        with self.assertRaisesRegex(RuntimeError,"必需行情失效.*MO2609-C-6600"):
            s.runner.poll_live_inputs()
        self.assertFalse(s.runner.accept_bars)
        self.assertEqual(s.strategy.position(str(s.references.instrument_ids[OPTION])), -1)
        with self.assertRaisesRegex(ValueError,"实时Bar过期"):
            self.publish(s,OPTION,"2026-08-13 13:01")
        self.assertEqual(s.runner.ignored_preparation_bars,0)

    def test_reported_100ms_late_bar_keeps_held_health_without_changing_closed_decision(self):
        s = self.setup_session(held=True)
        self.missing_1032(s)
        old_frames, old_latest = deepcopy(s.runner.frame_audit), dict(s.strategy.latest)
        counts = (s.strategy.frames, s.strategy.submissions, s.strategy.fills_received)
        old_prices = s.runner.prices.get(s.references.instrument_ids[OPTION])
        self.now = ns("2026-08-13 10:32")+22_645_000_000
        with self.assertLogs("OptionMarketInputs", level="INFO") as logs:
            self.publish(s, OPTION, "2026-08-13 10:32")
        self.assertIn("迟到Bar仅更新行情健康", "\n".join(logs.output))
        self.assertEqual(s.runner.observations[OPTION], (ns("2026-08-13 10:32"), self.now))
        self.assertEqual(s.runner.frame_audit, old_frames)
        self.assertEqual(s.strategy.latest, old_latest)
        self.assertEqual((s.strategy.frames, s.strategy.submissions, s.strategy.fills_received), counts)
        self.assertEqual(s.runner.prices.get(s.references.instrument_ids[OPTION]), old_prices)
        self.assertEqual(s.runner.frame_audit[-1]["market_summary"]["status"], "INCOMPLETE")
        self.assertEqual(s.runner.late_bar_health_updates, 1)
        self.assertEqual(s.runner.bar_time_audit[-1]["action"], "HEALTH_ONLY_LATE")
        self.now = ns("2026-08-13 10:33")+54_982_740
        s.runner.poll_live_inputs()
        self.assertIsNone(s.runner.failure)
        self.assertEqual(s.strategy.position(str(s.references.instrument_ids[OPTION])), -1)

    def test_late_before_flush_still_cannot_enter_frame_or_emit_target(self):
        s = self.setup_session()
        stamp = ns("2026-08-13 13:58")
        self.now = stamp
        self.bars(s, "2026-08-13 13:58", omit=(OPTION,), flush=False)
        self.now = stamp+20_100_000_000
        self.publish(s, OPTION, "2026-08-13 13:58")
        self.assertNotIn(OPTION, s.runner.frames.pending[stamp][1])
        s.runner.flush_frames()
        self.assertEqual(s.strategy.entry_audit[-1]["outcome"], "NO_VALID_ENTRY")
        self.assertFalse(s.client.requests)
        self.assertNotIn(OPTION, s.strategy.latest)
        self.assertEqual(s.runner.observations[OPTION][0], stamp)

    def test_true_silence_still_closes_gate_after_120_seconds(self):
        s = self.setup_session(held=True)
        self.missing_1032(s)
        self.now = ns("2026-08-13 10:32")+22_645_000_000
        self.publish(s, OPTION, "2026-08-13 10:32")
        self.now = ns("2026-08-13 10:34")+1
        self.bars(s, "2026-08-13 10:34", omit=(OPTION,), flush=False)
        with self.assertRaisesRegex(RuntimeError, "必需行情失效.*MO2609-C-6600"):
            s.runner.poll_live_inputs()
        self.assertFalse(s.runner.accept_bars)
        self.assertEqual(s.strategy.position(str(s.references.instrument_ids[OPTION])), -1)

    def test_next_timely_minute_resumes_normal_frames_after_health_only_update(self):
        s = self.setup_session(held=True)
        self.missing_1032(s)
        old_frame = deepcopy(s.runner.frame_audit[-1])
        self.now = ns("2026-08-13 10:32")+22_645_000_000
        self.publish(s, OPTION, "2026-08-13 10:32")
        count = s.strategy.frames
        self.now = ns("2026-08-13 10:33")+6_916_000_000
        self.bars(s, "2026-08-13 10:33", flush=False)
        self.now += 20_000_000_001
        s.runner.poll_live_inputs()
        self.assertEqual(s.strategy.frames, count+1)
        self.assertEqual(s.runner.frame_audit[-2], old_frame)
        self.assertEqual(s.runner.frame_audit[-1]["market_summary"]["status"], "COMPLETE")
        self.assertEqual(s.strategy.latest[OPTION][0], ns("2026-08-13 10:33"))
        self.assertEqual(s.runner.observations[OPTION][0], ns("2026-08-13 10:33"))
        self.assertEqual(s.strategy.position(str(s.references.instrument_ids[OPTION])), -1)

    def test_repeated_same_minute_cannot_refresh_health_receive_time(self):
        s = self.setup_session(held=True)
        self.missing_1032(s)
        self.now = ns("2026-08-13 10:32")+22_645_000_000
        self.publish(s, OPTION, "2026-08-13 10:32")
        observation = s.runner.observations[OPTION]
        self.now = ns("2026-08-13 10:33")+30_000_000_000
        self.publish(s, OPTION, "2026-08-13 10:32")
        self.assertEqual(s.runner.observations[OPTION], observation)
        self.assertEqual(s.runner.late_bar_health_updates, 1)

    def test_invalid_late_price_cannot_refresh_health(self):
        for price in (Decimal(0), Decimal(-1), Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(price=price):
                s = self.setup_session(held=True)
                self.missing_1032(s)
                before = dict(s.runner.observations)
                self.now = ns("2026-08-13 10:32")+22_645_000_000
                event = NS(bar_type=NS(instrument_id=s.references.instrument_ids[OPTION]),
                    ts_event=ns("2026-08-13 10:32"), close=NS(as_decimal=lambda: price))
                with patch.object(runtime, "Bar", NS):
                    with self.assertRaisesRegex(ValueError, "有限正数"):
                        s.runner.publish("market", event)
                self.assertEqual(s.runner.observations, before)
                self.assertEqual(s.runner.late_bar_health_updates, 0)

    def test_regressed_or_expired_late_bar_remains_fatal(self):
        for minute, now, reason in (("10:31", "10:32:30", "时间倒退"),
                                    ("10:32", "10:34:01", "实时Bar过期")):
            with self.subTest(reason=reason):
                s = self.setup_session(held=True)
                self.missing_1032(s)
                self.now = ns("2026-08-13 10:32")+22_645_000_000
                self.publish(s, OPTION, "2026-08-13 10:32")
                before = dict(s.runner.observations)
                self.now = ns("2026-08-13 "+now)
                with self.assertRaisesRegex(ValueError, reason):
                    self.publish(s, OPTION, "2026-08-13 "+minute)
                self.assertEqual(s.runner.observations, before)


if __name__ == "__main__":
    unittest.main()
