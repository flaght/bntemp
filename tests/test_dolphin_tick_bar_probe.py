"""Offline checks for sparse tick/Bar evidence collection, with no network."""
from contextlib import redirect_stdout
from importlib import import_module
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.integration.options.dolphin_tick_bar_probe import TickBarProbe


class TickBarProbeTests(unittest.TestCase):
    def test_quote_only_changes_trade_changes_and_bar_tick_context(self):
        output = StringIO()
        probe = TickBarProbe(("option",), output)
        tick = dict(Code="option", Volume=10, BidPrice1=1., BidVolume1=2, AskPrice1=2., AskVolume1=3)
        with patch("scripts.integration.options.dolphin_tick_bar_probe.time.time_ns", side_effect=[1_000_000_000, 2_000_000_000, 62_000_000_000, 63_000_000_000]), redirect_stdout(StringIO()):
            probe.on_row("tick", "ticks", tick)
            probe.on_row("tick", "ticks", dict(tick, BidVolume1=4))
            probe.on_row("tick", "ticks", dict(tick, Volume=11))
            probe.on_row("bar", "bars", dict(Code="option", minTime="09:42:00"))
        state = probe.snapshot()["contracts"]["option"]
        self.assertEqual(state["tick_rows"], 3)
        self.assertEqual(state["bar_rows"], 1)
        self.assertEqual(state["quote_changes_without_volume_change"], 1)
        self.assertEqual(state["volume_increases"], 1)
        self.assertEqual(state["max_tick_receipt_gap_seconds"], 60)
        lines = [json.loads(row) for row in output.getvalue().splitlines()]
        self.assertEqual(lines[-1]["latest_tick_at_bar_receipt"]["row"]["Volume"], 11)
        self.assertEqual(len(lines), 4)

    def test_unknown_symbols_not_persisted_and_missing_quotes_not_inferred(self):
        output = StringIO()
        probe = TickBarProbe(("option",), output)
        for row in (dict(Code="other"), dict(Code="option", Volume=10), dict(Code="option", Volume=10)):
            probe.on_row("tick", "ticks", row)
        result = probe.snapshot()
        self.assertEqual(result["total_received"]["tick"], 3)
        self.assertEqual(result["contracts"]["option"]["quote_changes_without_volume_change"], 0)
        self.assertEqual(len(output.getvalue().splitlines()), 2)

    def test_empty_live_sample_reports_unknown_and_cleans_up(self):
        module = import_module("scripts.integration.options.dolphin_tick_bar_probe")
        class Driver:
            resolved_columns = {}
            subscriptions = []
            stopped = False
            def start(self, *args, **kwargs): pass
            def subscribe(self, spec, handler):
                self.subscriptions.append(spec)
                self.resolved_columns[spec.table_name] = ("Code",)
            def stop(self): self.stopped = True
        driver = Driver()
        native = SimpleNamespace(create_native_driver=lambda: driver,
            NativeDolphinSubscription=lambda **kw: SimpleNamespace(**kw))
        with TemporaryDirectory() as directory, patch.dict("sys.modules", {
                "dotenv": SimpleNamespace(load_dotenv=lambda *_: None),
                "bomber.framework.market.native.dolphin.driver": native}), \
                patch.dict("os.environ", {"DDB_HOST":"fixture", "DDB_USERNAME":"fixture", "DDB_PASSWORD":"fixture"}), \
                patch.object(module.time, "monotonic", side_effect=[0, 61]), redirect_stdout(StringIO()):
            code = module.main(["--seconds", "60", "--keys", "option", "--report-dir", directory])
            report = json.loads(next(Path(directory).glob("*/summary.json")).read_text())
        self.assertEqual(code, 0)
        self.assertTrue(driver.stopped)
        self.assertEqual(len(driver.subscriptions), 2)
        self.assertTrue(all(s.offset == -1 for s in driver.subscriptions))
        self.assertEqual(report["contracts"]["option"]["tick_rows"], 0)
        self.assertTrue(report["limitations"])


if __name__ == "__main__":
    unittest.main()
