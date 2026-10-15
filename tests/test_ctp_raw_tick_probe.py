"""Raw MD probe callback and lifecycle checks without CTP SDK or network."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.integration.options import ctp_raw_tick_probe as module


class CtpRawTickProbeTests(unittest.TestCase):
    def test_raw_zero_trade_quote_changes_are_preserved(self):
        output, stop = StringIO(), threading.Event()
        probe = module.RawCtpProbe(("option",), output, ("b", "u", "p"), stop)
        tick = dict(InstrumentID="option", Volume=5, BidPrice1=1., BidVolume1=1,
            AskPrice1=2., AskVolume1=2, UpdateTime="10:01:00", UpdateMillisec=500)
        probe.on_depth_market_data(tick)
        probe.on_depth_market_data(dict(tick, AskVolume1=3))
        result = probe.collector.snapshot()["contracts"]["option"]
        self.assertEqual(result["tick_rows"], 2)
        self.assertEqual(result["volume_increases"], 0)
        self.assertEqual(result["quote_changes_without_volume_change"], 1)
        self.assertNotIn("Code", tick)
        self.assertEqual(json.loads(output.getvalue().splitlines()[0])["row"]["InstrumentID"], "option")

    def test_subscription_rejection_and_disconnection_fail_capture(self):
        for event in ("reject", "disconnect"):
            stop = threading.Event()
            probe = module.RawCtpProbe(("option",), StringIO(), ("b", "u", "p"), stop)
            if event == "reject":
                probe.on_subscribe_response({"InstrumentID":"option"}, {"ErrorID":7})
                self.assertFalse(probe.subscription_events["option"].is_set())
            else:
                probe.on_front_disconnected(4097)
            self.assertTrue(stop.is_set())
            self.assertTrue(probe.errors)

    def test_login_submission_exception_is_recorded(self):
        probe = module.RawCtpProbe(("option",), StringIO(), ("b", "u", "secret"), threading.Event())
        def fail(*args): raise RuntimeError("secret")
        probe.driver = SimpleNamespace(login=fail)
        probe.on_front_connected()
        self.assertTrue(probe.stop.is_set())
        self.assertNotIn("secret", str(probe.errors))

    def test_md_only_lifecycle_with_zero_tick_sample_is_saved(self):
        class Driver:
            def __init__(self, callbacks): self.cb, self.calls = callbacks, []
            def start(self, *args):
                self.calls.append("start")
                self.cb.on_front_connected()
            def login(self, *args):
                self.calls.append("login")
                self.cb.on_login_response({"ErrorID":0})
            def subscribe(self, key):
                self.calls.append("subscribe")
                self.cb.on_subscribe_response({"InstrumentID":key}, {"ErrorID":0})
            def stop(self): self.calls.append("stop")
        drivers = []
        def factory(cb):
            drivers.append(Driver(cb))
            return drivers[-1]
        with TemporaryDirectory() as directory, patch.dict("sys.modules", {
                "dotenv": SimpleNamespace(load_dotenv=lambda *_: None),
                "bomber.framework.market.native.ctp.driver": SimpleNamespace(create_native_driver=factory)}), \
                patch.dict("os.environ", {"CTP_MD_ADDRESS":"tcp://fixture:1", "CTP_BROKER_ID":"fixture",
                    "CTP_ACCOUNT_ID":"fixture", "CTP_PASSWORD":"secret", "CTP_PRODUCTION_MODE":"false"}), \
                patch.object(module.time, "monotonic", side_effect=[0,0,0,0,0,0,0,61]), redirect_stdout(StringIO()):
            code = module.main(["--keys", "option", "--seconds", "60", "--report-dir", directory])
            report_text = next(Path(directory).glob("*/summary.json")).read_text()
            report = json.loads(report_text)
        self.assertEqual(code, 0)
        self.assertEqual(drivers[0].calls, ["start", "login", "subscribe", "stop"])
        self.assertEqual(report["subscriptions"], {"option":"accepted"})
        self.assertEqual(report["contracts"]["option"]["tick_rows"], 0)
        self.assertNotIn("secret", report_text)


if __name__ == "__main__":
    unittest.main()
