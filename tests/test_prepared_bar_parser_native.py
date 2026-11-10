"""Native object equivalence; run separately on XY after syncing Python files."""
from decimal import Decimal
from pathlib import Path
import unittest


class NativePreparedBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import bomber.backtest.engine as engine
            if not str(getattr(engine, "__file__", "")).endswith((".so", ".pyd")):
                raise ImportError("compiled engine unavailable")
            from bomber.framework.market.basic.base import Bar, InstrumentId, InstrumentMeta
        except ImportError as exc:
            raise unittest.SkipTest(f"requires the installed native Bomber SDK: {exc}")
        # An installed SDK with an old parser is a deployment failure, not a skip.
        from bomber.framework.market.replay.parsers.bar.fixed import FixedInstrumentBarParser, PreparedBarParser
        from bomber.framework.market.replay.parsers.base import ParserContext
        cls.Bar, cls.InstrumentId, cls.InstrumentMeta = Bar, InstrumentId, InstrumentMeta
        cls.Fixed, cls.Prepared, cls.Context = FixedInstrumentBarParser, PreparedBarParser, ParserContext

    def signature(self, event):
        bar = event.payload
        self.assertIsInstance(bar, self.Bar)
        return (str(bar.bar_type), bar.ts_event, bar.ts_init,
            *((str(getattr(bar, name)), getattr(bar, name).precision)
                for name in ("open", "high", "low", "close", "volume")))

    def test_native_price_quantity_precision_timestamps_and_periods_equal_generic(self):
        instrument = self.InstrumentId.from_str("MO2609-C-6600.CFFEX")
        stamp = 1_786_600_800_000_000_000
        for period in ("1-MINUTE", "5-MINUTE"):
            for precision in (0, 1, 2, 4, 8):
                for size_precision in (0, 1, 2):
                    for close in ("101.005", Decimal("101.125"), 101.55555555):
                        with self.subTest(period=period, price=precision, size=size_precision, close=close):
                            meta = self.InstrumentMeta(instrument, price_precision=precision, size_precision=size_precision)
                            context = self.Context(Path("/native/fixture.feather"), 1, lambda key: meta)
                            row = dict(datetime="2026-08-13T14:00:00+08:00", event_ns=stamp,
                                available_ns=stamp+123, open="100.125", high="102.987", low="99.876",
                                close=close, volume="20.5")
                            # Keep the ISO and integer clock equal using a trusted parser calculation.
                            from bomber.framework.market.replay.parsers.base import parse_iso_timestamp
                            row["event_ns"] = parse_iso_timestamp(row["datetime"], context)
                            row["available_ns"] = row["event_ns"]+123
                            old = self.Fixed(instrument, bar_spec=period, available_nanoseconds="available_ns")
                            new = self.Prepared(instrument, bar_spec=period)
                            left = [self.signature(event) for event in old.parse(row, context)]
                            right = [self.signature(event) for event in new.parse(row, context)]
                            self.assertEqual(left, right)

    def test_native_parser_reset_observes_new_metadata_and_keeps_price_rounding(self):
        instrument = self.InstrumentId.from_str("IM2609.CFFEX")
        registry = {instrument: self.InstrumentMeta(instrument, price_precision=1, size_precision=0)}
        context = self.Context(Path("/native/fixture.feather"), 2, registry.get)
        row = dict(datetime="2026-08-13T14:00:00+08:00", event_ns=1_786_600_800_000_000_000,
            available_ns=1_786_600_800_000_000_123, open=100.125, high=102.987, low=99.876,
            close=101.005, volume=20.5)
        from bomber.framework.market.replay.parsers.base import parse_iso_timestamp
        row["event_ns"] = parse_iso_timestamp(row["datetime"], context)
        row["available_ns"] = row["event_ns"]+123
        parser = self.Prepared(instrument)
        first = next(iter(parser.parse(row, context)))
        registry[instrument] = self.InstrumentMeta(instrument, price_precision=4, size_precision=1)
        parser.reset()
        second = next(iter(parser.parse(row, context)))
        reference = next(iter(self.Fixed(instrument, available_nanoseconds="available_ns").parse(row, context)))
        self.assertNotEqual(self.signature(first), self.signature(second))
        self.assertEqual(self.signature(second), self.signature(reference))


if __name__ == "__main__":
    unittest.main()
