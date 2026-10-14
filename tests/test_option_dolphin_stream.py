"""Minute-push regression: real feed/converter/health with fake SDK transport."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib import import_module
import os
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from bomber.framework.market.basic.base import DataType, InstrumentId, InstrumentMeta, make_bar
from bomber.framework.market.native.dolphin.driver import OfficialDolphinDbDriver, NativeDolphinSubscription, rows_from_message
from bomber.framework.market.stream.dolphin import DolphinDbMarketConverter, DolphinDbLiveDataFeed, DolphinDbConfig, DolphinDbStreamSpec
from bomber.framework.market.stream.dolphin.config import BAR_REQUIRED_COLUMNS
from bomber.framework.market.stream.health import MarketHealthState, MarketHealthReason
from bomber.framework.trader.runtime.live.channels.dolphin import DolphinMarketChannel
from tests.test_demo_option_vega import ns


class Session:
    def __init__(self, columns):
        self.columns, self.subscriptions, self.queries = columns, [], []
    def connect(self, *args): return True
    def enableStreaming(self, *args): pass
    def run(self, script):
        self.queries.append(script)
        return self.columns
    def subscribe(self, **kwargs): self.subscriptions.append(kwargs)
    def unsubscribe(self, *args): pass
    def close(self): pass


class DolphinMinuteTests(unittest.TestCase):
    def setUp(self):
        self.instrument = InstrumentId.from_str("MO2611-C-7400.CFFEX")
        self.meta = InstrumentMeta(self.instrument, price_precision=1, size_precision=0,
                                   price_increment=Decimal(".2"), multiplier=Decimal(100))
        self.converter = DolphinDbMarketConverter()

    def row(self, clock="09:35:00", volume=2):
        return dict(minTime=clock, date="2026-10-14", Code="MO2611-C-7400",
                    open=82.8, high=82.8, low=82.8, close=82.8, volume=volume,
                    updateTime="2026-10-14T09:35:03")

    def test_auto_schema_maps_reordered_optional_columns_without_guessing_positions(self):
        columns = ("Code", "extra") + tuple(c for c in BAR_REQUIRED_COLUMNS if c != "Code")
        session = Session(columns)
        driver = OfficialDolphinDbDriver(session_factory=lambda **kw: session)
        driver.start("ddb.test", 8848, "test", "password")
        self.addCleanup(driver.stop)
        rows = []
        driver.subscribe(NativeDolphinSubscription("cffexBarStream", "test", (),
            required_columns=BAR_REQUIRED_COLUMNS), rows.append)
        row = self.row(); row["extra"] = 123
        session.subscriptions[0]["handler"]([row[c] for c in columns])
        self.assertEqual(rows, [{c: row[c] for c in columns}])
        self.assertEqual(driver.resolved_columns["cffexBarStream"], columns)
        self.assertIn('objByName("cffexBarStream", true)', session.queries[0])
        self.assertEqual(session.subscriptions[0]["offset"], -1)

    def test_schema_missing_close_or_dfs_path_fails_before_subscription(self):
        for name, columns in (("cffexBarStream", tuple(c for c in BAR_REQUIRED_COLUMNS if c != "close")),
                              ("dfs://min_bar/cffex_1min", BAR_REQUIRED_COLUMNS)):
            session = Session(columns)
            driver = OfficialDolphinDbDriver(session_factory=lambda **kw: session)
            driver.start("ddb.test", 8848, "test", "password")
            try:
                with self.assertRaises(ValueError):
                    driver.subscribe(NativeDolphinSubscription(name, "test", (),
                        required_columns=BAR_REQUIRED_COLUMNS), lambda row: None)
                self.assertEqual(session.subscriptions, [])
            finally: driver.stop()

    def test_wrong_row_width_is_rejected(self):
        with self.assertRaises(ValueError): rows_from_message(["symbol", 1], ("Code", "close", "volume"))

    def test_action_name_rejects_hyphens_before_sdk_calls(self):
        for action in ("vega-market", "name with space", "name/path", ""):
            with self.subTest(action=action):
                with self.assertRaisesRegex(ValueError, "action_name"):
                    NativeDolphinSubscription("stream_cffex_1min", action, BAR_REQUIRED_COLUMNS)
        with self.assertRaises(ValueError):
            DolphinDbStreamSpec.bar("stream_cffex_1min", action_name="vega-market")

    def test_option_market_and_index_generate_valid_unique_action_names(self):
        entry = import_module("demos.08_option_vega.run_live")
        with patch.dict(os.environ, {"DDB_HOST":"ddb.test","DDB_USERNAME":"test","DDB_PASSWORD":"fixture"}, clear=True):
            for source in ("vega-market", "vega-index"):
                feed = entry._dolphin_feed("stream_cffex_1min", source, max_age_seconds=120)
                action = feed.config.streams[0].action_name
                self.assertRegex(action, r"^[A-Za-z0-9_]+$")
                self.assertNotIn("-", action)
                self.assertEqual(NativeDolphinSubscription("stream_cffex_1min", action, ()).action_name, action)

    def run_raw_probe(self, streams):
        from contextlib import redirect_stdout
        from io import StringIO
        from pathlib import Path
        from tempfile import TemporaryDirectory
        probe = import_module("scripts.integration.options.dolphin_stream_probe")
        # Publisher shape observed for market, different time-column shape for index.
        index_columns = ("timestamp", "Code", "open", "high", "low", "close", "volume")
        class Publisher(Session):
            def run(self, script):
                self.queries.append(script)
                return index_columns if '"stream_index_1min"' in script else BAR_REQUIRED_COLUMNS
            def subscribe(self, **kwargs):
                import re
                if not re.fullmatch(r"[A-Za-z0-9_]+", kwargs["actionName"]):
                    raise ValueError("server actionName rejected")
                self.subscriptions.append(kwargs)
                table = kwargs["tableName"]
                columns = index_columns if table == "stream_index_1min" else BAR_REQUIRED_COLUMNS
                for key in probe.TABLE_KEYS[table]:
                    row = dict(timestamp="2026-10-14T11:10:00", Code=key, date="2026-10-14",
                        minTime="11:10:00", open=82.8, high=82.8, low=82.8, close=82.8, volume=2)
                    kwargs["handler"]([row[c] for c in columns])
        publisher = Publisher(())
        driver = OfficialDolphinDbDriver(session_factory=lambda **kw:publisher)
        with TemporaryDirectory() as directory, patch.object(probe, "ROOT", Path(directory)), \
                patch.dict(os.environ, {"DDB_HOST":"ddb.test","DDB_USERNAME":"fake","DDB_PASSWORD":"fixture"}, clear=True), \
                patch("bomber.framework.market.native.dolphin.driver.create_native_driver", return_value=driver), \
                patch.object(probe.time, "monotonic", side_effect=[0,61]), redirect_stdout(StringIO()):
            code = probe.main(["--seconds","60","--streams",streams])
            import json
            report = json.loads(next(Path(directory).glob("tests/results/option-vega-dolphin-stream-probe/*/summary.json")).read_text())
        self.assertEqual(code,0)
        self.assertEqual(report["probe_version"],3)
        self.assertEqual(report["missing_monitored_keys"],[])
        return report, publisher

    def test_raw_probe_reads_index_schema_without_guessing_time_fields(self):
        report, publisher = self.run_raw_probe("both")
        self.assertEqual(len(publisher.subscriptions),2)
        self.assertNotIn("date",report["schema"]["stream_index_1min"])
        self.assertNotIn("minTime",report["schema"]["stream_index_1min"])
        row = report["tables"]["stream_index_1min"]["matched"]["000852"]["last_observation"]["row"]
        self.assertEqual(row["timestamp"],"2026-10-14T11:10:00")
        self.assertNotIn("minTime",row)

    def test_market_only_probe_does_not_query_or_subscribe_unknown_index(self):
        report, publisher = self.run_raw_probe("market")
        self.assertEqual(set(report["tables"]),{"stream_cffex_1min"})
        self.assertEqual(len(publisher.subscriptions),1)
        self.assertTrue(all("stream_index_1min" not in query for query in publisher.queries))

    def test_cumulative_volume_baseline_flat_minute_increment_and_day_reset(self):
        convert = lambda row: self.converter.convert_bar(row, self.instrument, self.meta, "1-MINUTE",
            volume_mode="cumulative", received_ns=ns("2026-10-14 09:35") + 14_000_000_000)
        first = convert(self.row(volume=100))[0].event
        self.assertEqual(first.volume.as_decimal(), 0)
        self.assertEqual(first.ts_init, ns("2026-10-14 09:35") + 14_000_000_000)
        self.assertEqual(convert(self.row("09:36:00", 100))[0].event.volume.as_decimal(), 0)
        self.assertEqual(convert(self.row("09:37:00", 102))[0].event.volume.as_decimal(), 2)
        self.assertEqual(convert(self.row("09:37:00", 102)), [])
        with self.assertRaisesRegex(ValueError, "累计量下降"):
            convert(self.row("09:38:00", 101))
        row = self.row("09:30:00", 1); row["date"] = "2026-10-15"
        self.assertEqual(convert(row)[0].event.volume.as_decimal(), 0)

    def test_per_bar_volume_and_zero_volume_ohlc_are_preserved(self):
        for clock, volume in (("09:35:00", 2), ("09:36:00", 0)):
            bar = self.converter.convert_bar(self.row(clock, volume), self.instrument, self.meta, "1-MINUTE")[0].event
            self.assertEqual(bar.volume.as_decimal(), volume)
            self.assertEqual(bar.close.as_decimal(), Decimal("82.8"))

    def test_reported_numpy_time_rows_use_business_date_and_cumulative_increment(self):
        import numpy as np
        samples = (
            ("IM2611", 200, (6941.4,6942.6,6938.6,6940.2,19420),
                (6940.2,6943.8,6936.0,6936.0,19467),
                1791948481272364375,1791948540862200453,47),
            ("MO2611-C-7400", 100, (72.4,72.4,71.6,71.6,1607),
                (71.6,72.0,71.6,72.0,1608),
                1791948481284191872,1791948542991255356,1),
        )
        for symbol, multiplier, first, second, first_receipt, second_receipt, expected_volume in samples:
            with self.subTest(symbol=symbol):
                instrument = InstrumentId.from_str(symbol + ".CFFEX")
                meta = InstrumentMeta(instrument, price_precision=1, size_precision=0,
                    price_increment=Decimal(".2"), multiplier=Decimal(multiplier))
                bars = []
                for minute, values, received in (("11:28:00",first,first_receipt),("11:29:00",second,second_receipt)):
                    row = dict(date=np.datetime64("2026-10-14","D"),
                        minTime=np.datetime64("1970-01-01T" + minute,"ms"), Code=symbol,
                        open=values[0],high=values[1],low=values[2],close=values[3],volume=values[4],
                        updateTime=np.datetime64("2026-10-14T" + minute,"ms"))
                    bar = self.converter.convert_bar(row,instrument,meta,"1-MINUTE",
                        volume_mode="cumulative",received_ns=received)[0].event
                    self.assertEqual(bar.ts_event,ns("2026-10-14 " + minute[:5]))
                    self.assertEqual(bar.ts_init,received)
                    self.assertEqual(bar.close.as_decimal(),Decimal(str(values[3])))
                    bars.append(bar)
                self.assertEqual(bars[0].volume.as_decimal(),0)
                self.assertEqual(bars[1].volume.as_decimal(),expected_volume)

    def feed(self):
        spec = DolphinDbStreamSpec.bar("cffexBarStream", auto_schema=True, local_receipt_time=True)
        feed = DolphinDbLiveDataFeed(DolphinDbConfig("ddb.test", 8848, "test", "password", (spec,)))
        feed.register_instrument(self.meta)
        feed.subscribe(self.instrument, DataType.BAR, "1-MINUTE")
        feed._health_monitor.on_connected()
        return feed, spec

    def test_live_receipts_override_frozen_ready_snapshot_and_stale_still_fails(self):
        feed, spec = self.feed()
        first = ns("2026-10-14 09:35") + 3_000_000_000
        with patch("time.time_ns", return_value=first): feed.on_stream_row(spec, self.row())
        old = feed.health_snapshot.last_event_received_ns
        last = ns("2026-10-14 09:39") + 14_000_000_000
        with patch("time.time_ns", return_value=last): feed.on_stream_row(spec, self.row("09:39:00"))
        self.assertEqual(feed.health_snapshot.last_event_received_ns, old)
        channel = DolphinMarketChannel(feed, max_receive_age_seconds=120, clock_ns=lambda: last)
        self.assertTrue(channel.check().ready)
        channel.clock_ns = lambda: last + 121_000_000_000
        self.assertFalse(channel.check().ready)
        self.assertEqual(feed.manifest["received_bars"], 2)

    def test_start_label_uses_completed_minute_for_event_age(self):
        feed, spec = self.feed()
        received = ns("2026-10-14 09:37") + 14_000_000_000
        with patch("time.time_ns", return_value=received): feed.on_stream_row(spec, self.row("09:35:00"))
        now = lambda: received
        self.assertFalse(DolphinMarketChannel(feed, max_receive_age_seconds=120, clock_ns=now).check().ready)
        self.assertTrue(DolphinMarketChannel(feed, max_receive_age_seconds=120, clock_ns=now,
            event_offset_seconds=60).check().ready)

    def test_bad_ohlc_closes_health_and_does_not_refresh_receipt(self):
        feed, spec = self.feed()
        row = self.row(); row["high"] = 1
        with self.assertLogs("DolphinDbLiveDataFeed", level="ERROR"): feed.on_stream_row(spec, row)
        self.assertEqual(feed.health_snapshot.reason, MarketHealthReason.STREAM_INTERRUPTED)
        self.assertIsNone(feed.latest_received_ns)
        self.assertEqual(feed.manifest["conversion_errors"], 1)
        # A subsequent valid row cannot hide an invalid input in this session.
        received = ns("2026-10-14 09:35") + 3_000_000_000
        with patch("time.time_ns", return_value=received): feed.on_stream_row(spec, self.row())
        self.assertEqual(feed.health_snapshot.state, MarketHealthState.READY)
        result = DolphinMarketChannel(feed, clock_ns=lambda:received).check()
        self.assertFalse(result.ready)
        self.assertIn("CONVERSION_ERROR", {issue.code for issue in result.issues})

    def test_cli_requires_stream_and_explicit_minute_semantics(self):
        from contextlib import redirect_stderr
        from io import StringIO
        entry = import_module("demos.08_option_vega.run_live")
        common = ["--connect", "--expected-trading-day", "20261014", "--market-source", "dolphindb"]
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(StringIO()):
            for extras in ([], ["--market-bar-table", "cffexBarStream"],
                           ["--market-bar-table", "cffexBarStream", "--market-bar-timestamp", "start"]):
                with self.assertRaises(SystemExit): entry.parse_args(common + extras)
            args = entry.parse_args(common + ["--market-bar-table", "cffexBarStream",
                "--market-bar-timestamp", "start", "--market-volume-mode", "cumulative"])
        self.assertEqual(args.market_bar_max_age_seconds, 120)
        self.assertEqual(args.mode, "recording")

    def test_required_bar_gate_expires_source_even_if_late_receipt_is_recent(self):
        from tests.test_option_vega_assembly import ReferenceService
        runtime = import_module("demos.08_option_vega.live_runtime")
        from bomber.framework.trader.portfolio import PositionManager
        service = ReferenceService()
        refs = runtime.OptionReferences(service, "20260813", tuple(date(2026, 8, 13) + timedelta(days=n) for n in range(50)))
        now = ns("2026-08-13 09:37") + 1_000_000_000
        runner = runtime.OptionSessionRunner(positions=PositionManager(), references=refs,
            sources={"MO2609-C-6600":"market-bars"}, labels={"market-bars":"end"}, replay=False,
            grace_seconds=20, market_age_seconds=300, instrument_age_seconds=30,
            expected_day="20260813", clock_ns=lambda:now, source_age_seconds={"market-bars":120})
        runner.strategy = NS(required_market_keys=lambda:{"MO2609-C-6600"})
        runner.observations["MO2609-C-6600"] = (ns("2026-08-13 09:35"), now - 1_000_000_000)
        self.assertIn("MO2609-C-6600", runner.stale_instruments())

    def test_stream_callback_reaches_recording_target_without_ctp_market_or_td(self):
        from tests.test_option_vega_assembly import ReferenceService, Feed
        from zoneinfo import ZoneInfo
        runtime = import_module("demos.08_option_vega.live_runtime")
        entry = import_module("demos.08_option_vega.run_live")
        signal = import_module("demos.08_option_vega.vega_signal")
        args = entry.parse_args(["--connect", "--expected-trading-day", "20260813",
            "--market-source", "dolphindb", "--market-bar-table", "cffexBarStream",
            "--market-bar-timestamp", "end", "--market-volume-mode", "cumulative",
            "--frame-wait-seconds", "20", "--min-time-value", "0"])
        service = ReferenceService()
        refs = runtime.OptionReferences(service, "20260813", tuple(
            date(2026,8,13) + timedelta(days=n) for n in range(50)
            if (date(2026,8,13) + timedelta(days=n)).weekday()<5))
        spec = DolphinDbStreamSpec.bar("cffexBarStream", auto_schema=True,
            local_receipt_time=True, volume_mode="cumulative")
        sdk = Session(BAR_REQUIRED_COLUMNS)
        market = DolphinDbLiveDataFeed(DolphinDbConfig("ddb.test", 8848, "test", "password", (spec,)),
            source_id="vega-market", driver_factory=lambda:OfficialDolphinDbDriver(session_factory=lambda **kw:sdk))
        for instrument in refs.instrument_ids.values():
            market.register_instrument(InstrumentMeta(instrument, price_precision=1))
        index = Feed("fixture-index")
        index_id = InstrumentId.from_str("000852.XSHG")
        index.register_instrument(InstrumentMeta(index_id, price_precision=4))
        inputs = NS(references=refs, feeds={"market-bars":market,"index-bars":index},
            labels={"market-bars":"end","index-bars":"end"}, index_id=index_id,
            sources={**{key:"market-bars" for key in refs.instrument_ids},"000852":"index-bars"},
            ctp_upstream=None, ctp_bar_feed=None, dolphin_feeds=(market,), feature_query_feeds=(),
            source_identity={"market":{"channel":"dolphindb"}})
        session = runtime.assemble(args, inputs)
        self.addCleanup(session.runner.stop)
        stamp = ns("2026-08-13 13:58")
        now = [stamp + 14_000_000_000]
        session.runner.clock_ns = lambda:now[0]
        for channel in session.readiness.channels:
            if channel.identity.channel == "dolphindb":
                channel.clock_ns = lambda:now[0]
        done, observed = threading.Event(), []
        premium = signal.call_price(6500,6600,signal.tau(datetime.fromtimestamp(stamp/1e9,
            ZoneInfo("Asia/Shanghai")), date(2026,9,18)), .02,.25)
        with patch("time.time_ns", side_effect=lambda:now[0]):
            session.runner.start()
            session.runner.begin_bars()
            def received(bar):
                observed.append(bar)
                if len(observed)==2:done.set()
            market.register_bar_handler(received)
            callback = sdk.subscriptions[0]["handler"]
            for code, price in (("MO2609-C-6600",premium),("IM2609",6500)):
                row=dict(date="2026-08-13", minTime="13:58:00", Code=code,
                    open=price, high=price, low=price, close=price, volume=100)
                callback([row[c] for c in BAR_REQUIRED_COLUMNS])
            self.assertTrue(done.wait(2), "actual feed dispatcher did not deliver both Bars")
            index._emit_bar(make_bar(instrument_id=index_id, open=6400,high=6400,low=6400,
                close=6400,volume=0,ts_event=stamp,ts_init=now[0],meta=index.get_instrument_meta(index_id)))
            now[0] += 20_000_000_000
            session.runner.flush_frames()
            self.assertTrue(session.runner.session_ready())
        self.assertEqual(len(session.client.requests),1)
        self.assertEqual(session.strategy.entry_audit[0]["outcome"],"TARGET")
        self.assertEqual(session.runner.frame_audit[0]["status"],"CONSUMED")
        self.assertEqual(session.strategy.fills_received,0)
        self.assertIsNone(session.upstream)
        self.assertIsNone(session.driver)
        self.assertTrue(all(bar.volume.as_decimal()==0 for bar in observed))
        self.assertEqual(entry.describe(session)["market_streams"][0]["received_bars"],2)


if __name__ == "__main__": unittest.main()
