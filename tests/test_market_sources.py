"""M01配置及标准行情装配回归；行情/资料均为离线夹具，不连接数据库或柜台。"""
import argparse
from contextlib import redirect_stderr
from datetime import date
from decimal import Decimal
from importlib import import_module
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from bomber.framework.market.sources import (MarketSourceConfig, add_market_source_arguments,
    market_config_from_args, create_market_feed)

MINUTE = 60_000_000_000
BASE = 1_791_511_200_000_000_000


class ConfigTests(unittest.TestCase):
    def parse(self, values=(), **kwargs):
        parser = argparse.ArgumentParser()
        add_market_source_arguments(parser, **kwargs)
        return market_config_from_args(parser.parse_args(values))

    def test_ctp_and_file_defaults(self):
        self.assertEqual(self.parse().source, "ctp")
        self.assertEqual(self.parse(backtest=True).source, "file")

    def test_json_source_and_explicit_cli_override(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "market.json"
            path.write_text(json.dumps(dict(source="dolphindb", table="shared_minute",
                timestamp_label="start", volume_mode="cumulative")))
            cfg = self.parse(["--market-config", str(path), "--market-bar-table", "other_stream"])
            self.assertEqual((cfg.source, cfg.table, cfg.timestamp_label, cfg.volume_mode),
                ("dolphindb", "other_stream", "start", "cumulative"))

    def test_dolphin_requires_declared_semantics(self):
        for values in (dict(source="dolphindb"), dict(source="dolphindb", table="stream"),
                       dict(source="dolphindb", table="stream", timestamp_label="end", volume_mode="guess")):
            with self.subTest(values=values), self.assertRaises(ValueError):
                MarketSourceConfig(**values)

    def test_invalid_age_and_source(self):
        for values in (dict(source="unknown"), dict(max_age_seconds=60),
                       dict(max_age_seconds=float("nan")), dict(max_age_seconds=121)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                MarketSourceConfig(**values)

    def test_live_file_and_non_ctp_replay_fail(self):
        for args in (NS(market_source="file"), NS(market_source="bn-ws", simnow_environment="replay")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                market_config_from_args(args)

    def test_binance_contract_boundary(self):
        args = NS(market_source="bn-ws")
        self.assertEqual(market_config_from_args(args).source, "bn-ws")
        with self.assertRaisesRegex(ValueError, "国内期货/期权"):
            market_config_from_args(args, domestic_contracts=True)

    def test_file_factory_is_lazy(self):
        from bomber.framework.market.replay.base import FileReplayFeed
        feed = create_market_feed(MarketSourceConfig(source="file"), "file-input")
        self.assertIsInstance(feed, FileReplayFeed)
        self.assertFalse(feed.is_connected)

    def test_08_direct_script_help_bootstraps_project_path(self):
        entry = Path(__file__).resolve().parents[1] / "demos/08_option_vega/run_live.py"
        environment = os.environ.copy()
        if environment.get("PYTHONPATH"):
            environment["PYTHONPATH"] = os.pathsep.join(str(Path(item).resolve())
                for item in environment["PYTHONPATH"].split(os.pathsep) if item)
        with TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable,str(entry),"--help"], cwd=directory,
                env=environment,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("--market-config",result.stdout)


class MinuteSourceTests(unittest.TestCase):
    def setUp(self):
        from bomber.framework.market.basic.base import InstrumentId, InstrumentMeta, MarketDataFeed
        self.id = InstrumentId.from_str("rb2701.SHFE")
        self.meta = InstrumentMeta(self.id, price_precision=0, multiplier=Decimal(10))
        class Feed(MarketDataFeed):
            def connect(self): self._is_connected = True
            def disconnect(self): self._is_connected = False
            def _on_subscription_added(self, request): pass
            def _on_subscription_removed(self, request): pass
            def register_health_handler(self, handler): pass
        self.Feed = Feed
        self.now = BASE + MINUTE

    def feed(self, label="end", source="dolphindb", instrument=None):
        from bomber.framework.market.stream.completed import CompletedMinuteBarFeed
        from bomber.framework.market.basic.base import DataType
        config = MarketSourceConfig(source=source, table="minute_stream", timestamp_label=label, volume_mode="per-bar")
        upstream = self.Feed("upstream")
        feed = CompletedMinuteBarFeed("market-bars", upstream, config=config, clock_ns=lambda: self.now)
        feed.register_instrument(self.meta)
        feed.subscribe(instrument or self.id, DataType.BAR, "1-MINUTE")
        feed.required_instruments = {self.id}
        feed.connect()
        self.addCleanup(feed.disconnect)
        return upstream, feed

    def bar(self, stamp, price=100, instrument=None):
        from bomber.framework.market.basic.base import make_bar
        return make_bar(instrument_id=instrument or self.id, open=price, high=price, low=price,
            close=price, volume=1, ts_event=stamp, ts_init=self.now, meta=self.meta)

    def test_start_end_labels_produce_identical_closed_minute(self):
        for label, stamp in (("start", BASE), ("end", BASE + MINUTE)):
            upstream, feed = self.feed(label)
            results = []
            feed.register_bar_handler(results.append)
            upstream._emit_bar(self.bar(stamp))
            self.assertEqual(results[0].ts_event, BASE + MINUTE - 1)
            self.assertEqual(results[0].close.as_decimal(), 100)
            self.assertEqual(feed.stale_instruments(), {})

    def test_missing_leg_and_expired_event_fail_even_with_fresh_receipt(self):
        from bomber.framework.market.basic.base import InstrumentId
        upstream, feed = self.feed()
        peer = InstrumentId.from_str("rb2705.SHFE")
        feed.required_instruments = {self.id, peer}
        upstream._emit_bar(self.bar(BASE + MINUTE))
        self.assertIn(str(peer), feed.stale_instruments())
        self.now += 121_000_000_000
        self.assertIn(str(self.id), feed.stale_instruments())

    def test_duplicate_ignored_regression_and_future_close_gate(self):
        upstream, feed = self.feed()
        results = []
        feed.register_bar_handler(results.append)
        upstream._emit_bar(self.bar(BASE + MINUTE))
        upstream._emit_bar(self.bar(BASE + MINUTE))
        self.assertEqual(len(results), 1)
        upstream._emit_bar(self.bar(BASE))
        self.assertIn(str(self.id), feed.stale_instruments())
        upstream, feed = self.feed()
        upstream._emit_bar(self.bar(BASE + 2 * MINUTE))
        self.assertIn("未来", feed.failure)

    def test_bn_rejects_domestic_instrument(self):
        with self.assertRaisesRegex(ValueError, "BINANCE"):
            self.feed(source="bn-ws")

    def test_binance_closed_kline_is_normalized(self):
        from bomber.framework.market.basic.base import InstrumentId, InstrumentMeta
        self.id = InstrumentId.from_str("BTCUSDT.BINANCE")
        self.meta = InstrumentMeta(self.id, price_precision=0, exchange="BINANCE")
        upstream, feed = self.feed(source="bn-ws")
        results = []
        feed.register_bar_handler(results.append)
        upstream._emit_bar(self.bar(BASE + MINUTE - 1_000_000))
        self.assertEqual(results[0].ts_event, BASE + MINUTE - 1)

    def test_factories_do_not_connect_and_dolphin_has_no_ctp_credentials(self):
        from bomber.framework.trader.runtime.live.channels.ctp_market import CtpMarketCredentials
        with patch.dict(os.environ, {"DDB_HOST":"fake-db", "DDB_USERNAME":"fixture", "DDB_PASSWORD":"fixture"}, clear=True):
            feed = create_market_feed(MarketSourceConfig(source="dolphindb", table="minute_stream",
                timestamp_label="end", volume_mode="per-bar"), "test-dolphin")
            self.assertFalse(feed.is_connected)
            self.assertEqual(feed.config.streams[0].table_name,"minute_stream")
        lifecycle = NS(md_front="tcp://fake:1234", md_credentials=CtpMarketCredentials("9999","fixture","fixture",True))
        feed = create_market_feed(MarketSourceConfig(source="ctp"),"test-ctp",lifecycle=lifecycle,flow_path="/tmp/m01-unused")
        self.assertFalse(feed.is_connected)
        self.assertEqual(feed.config.front,"tcp://fake:1234")

    def test_bn_factory_uses_correct_market_and_ignores_open_kline(self):
        from bomber.framework.market.basic.base import InstrumentId, InstrumentMeta
        for market_type, symbol in (("spot","BTCUSDT"),("futures","BTCUSDT-PERP")):
            feed = create_market_feed(MarketSourceConfig(source="bn-ws",market_type=market_type),"bn-test")
            self.assertFalse(feed.is_connected)
            self.assertIn("fstream" if market_type == "futures" else "stream.binance",feed.config.ws_base_url)
            identity = InstrumentId.from_str(symbol+".BINANCE")
            feed.register_instrument(InstrumentMeta(identity,price_precision=0,exchange="BINANCE"))
            row = dict(e="kline",s="BTCUSDT",E=self.now//1_000_000,
                k=dict(s="BTCUSDT",i="1m",T=self.now//1_000_000-1,x=False,o="100",h="100",l="100",c="100",v="1"))
            with patch.object(feed,"enqueue_event") as emit:
                feed.on_ws_message(row)
                emit.assert_not_called()
                row["k"]["x"] = True
                feed.on_ws_message(row)
                self.assertEqual(emit.call_args.args[0].bar_type.instrument_id,identity)

    def test_ctp_assembly_preserves_tick_aggregation_contract(self):
        from bomber.framework.market.sources import assemble_minute_market
        from bomber.framework.market.basic.base import DataType, make_trade_tick
        from bomber.framework.trader.runtime.live.channels.ctp import CtpLiveProfile
        upstream = self.Feed("ctp-fixture")
        feed, channel, gate = assemble_minute_market(NS(market_source_config=MarketSourceConfig()),
            upstream,"ctp-minute",CtpLiveProfile(),(self.id,),"20261009")
        feed.register_instrument(self.meta)
        feed.subscribe(self.id,DataType.BAR,"1-MINUTE")
        feed.connect()
        self.addCleanup(feed.disconnect)
        results = []
        feed.register_bar_handler(results.append)
        for n,stamp in enumerate((BASE,BASE+MINUTE)):
            upstream._emit_trade_tick(make_trade_tick(instrument_id=self.id,price=100+n,size=1,
                trade_id=str(n),ts_event=stamp,ts_init=stamp,meta=self.meta))
        self.assertEqual(len(results),1)
        self.assertEqual(results[0].ts_event,BASE+MINUTE-1)
        self.assertEqual(results[0].close.as_decimal(),100)
        self.assertEqual(channel.identity.channel,"ctp")


class StrategySourceTests(unittest.TestCase):
    def setUp(self):
        from bomber.framework.market.basic.base import InstrumentId, InstrumentMeta, MarketDataFeed
        from bomber.framework.dataprep.contracts import InstrumentSpec
        from bomber.framework.market.stream.health import MarketHealthSnapshot, MarketHealthState, MarketHealthReason
        self.now = BASE + MINUTE
        owner = self
        class Feed(MarketDataFeed):
            latest_received_ns = BASE + MINUTE
            latest_event_ns = BASE + MINUTE
            conversion_failure = None
            @property
            def health_snapshot(self):
                return MarketHealthSnapshot(self.source_id, MarketHealthState.READY, MarketHealthReason.HEALTHY,
                    "fixture", 1, owner.now, owner.now, owner.now, owner.now, 0, 0, 0, 0)
            def connect(self): self._is_connected = True
            def disconnect(self): self._is_connected = False
            def _on_subscription_added(self, request): pass
            def _on_subscription_removed(self, request): pass
            def register_health_handler(self, handler): pass
        self.upstream = Feed("dolphin-fixture")
        self.ids = {key: InstrumentId.from_str(symbol + ".SHFE")
            for key, symbol in (("secondary", "rb2701"), ("far", "rb2705"), ("RB", "rb2701"), ("HC", "hc2701"))}
        self.specs = {key: InstrumentSpec("future", str(value.symbol), "HC" if key == "HC" else "RB",
            "SHFE", "CNY", date(2026, 1, 1), date(2027, 10, 1), Decimal(1), Decimal(10))
            for key, value in self.ids.items()}
        self.meta = lambda key: InstrumentMeta(self.ids[key], price_precision=0,
            price_increment=Decimal(1), multiplier=Decimal(10), exchange="SHFE")
        self.args = NS(mode="recording", product="RB", products=("RB", "HC"),
            leg1_role="secondary", leg2_role="far", expected_instruments={},
            simnow_environment="realtime", replay_md_trading_day=None, instrument_max_age_seconds=10,
            quantity=Decimal(1), lookback=2, entry_z=.5, exit_z=.1, rebalance_interval=1,
            max_notional=Decimal(1_000_000), max_quantity=Decimal(10), limit_offset_ticks=1,
            target_notional=Decimal(1000), group_fraction=Decimal('.5'), state_file=None,
            market_source_config=MarketSourceConfig(source="dolphindb", table="minute_stream",
                timestamp_label="end", volume_mode="per-bar"))

    def bar(self, key, price, stamp=None):
        from bomber.framework.market.basic.base import make_bar
        return make_bar(instrument_id=self.ids[key], open=price, high=price, low=price, close=price,
            volume=1, ts_event=stamp or self.now, ts_init=self.now, meta=self.meta(key))

    def test_calendar_same_strategy_receives_complete_dolphin_frames(self):
        runtime = import_module("demos.03_calendar_spread.live_runtime")
        refs = NS(roles=("secondary", "far"), product="RB", trading_day=date(2026, 10, 9),
            instrument_ids={k:self.ids[k] for k in ("secondary", "far")},
            specs={k:self.specs[k] for k in ("secondary", "far")}, spec="fixed", manifest={"fingerprint":"fixed"},
            validate=lambda:None, assert_expected=lambda _:None, instrument_meta=self.meta)
        session = runtime.assemble(self.args, refs, None, self.upstream, clock_ns=lambda:self.now)
        self.assertEqual(type(session.strategy), import_module("demos.03_calendar_spread.strategy").CalendarSpreadStrategy)
        session.runner.start()
        self.addCleanup(session.runner.stop)
        # Startup health requires genuine bars for both legs before enabling decisions.
        for key in refs.roles: self.upstream._emit_bar(self.bar(key, 100))
        self.assertTrue(session.runner.session_ready())
        session.runner.begin_bars()
        for n, price in enumerate((100, 101, 115, 100)):
            self.now = BASE + (n + 2) * MINUTE
            self.upstream.latest_event_ns = self.upstream.latest_received_ns = self.now
            self.upstream._emit_bar(self.bar("secondary", price))
            before = session.strategy.complete_frames
            self.upstream._emit_bar(self.bar("far", 100))
            self.assertEqual(session.strategy.complete_frames, before + 1)
        self.assertTrue(session.client.requests)
        self.assertEqual(session.runner.market_feed_id, "market-bars")

    def test_cross_section_same_strategy_receives_dolphin_frames(self):
        runtime = import_module("demos.04_cross_section.live_runtime")
        from bomber.framework.datahub.sector_roles import SectorRoleAssignment
        assignment = SectorRoleAssignment(date(2026,10,9),date(2026,10,8),BASE,BASE,
            {p:{"main":str(self.ids[p].symbol)} for p in self.args.products},
            {p:{"main":Decimal(1)} for p in self.args.products})
        refs = NS(trading_day=date(2026,10,9), instrument_ids={p:self.ids[p] for p in self.args.products},
            specs={p:self.specs[p] for p in self.args.products}, spec="fixed", validate=lambda:None,
            assert_expected=lambda *args:None, snapshot=lambda stamp:assignment, instrument_meta=self.meta)
        with patch.object(runtime.time,"time_ns",lambda:self.now):
            session = runtime.assemble(self.args, refs, None, self.upstream)
            self.assertEqual(type(session.strategy), import_module("demos.04_cross_section.strategy").MainCrossSectionMomentumStrategy)
            session.runner.start()
            self.addCleanup(session.runner.stop)
            for key in self.args.products: self.upstream._emit_bar(self.bar(key,100))
            self.assertTrue(session.runner.session_ready())
            session.runner.begin_bars()
            for n in range(4):
                self.now = BASE + (n + 2) * MINUTE
                self.upstream.latest_event_ns = self.upstream.latest_received_ns = self.now
                self.upstream._emit_bar(self.bar("RB",100+n*5))
                self.upstream._emit_bar(self.bar("HC",100-n*5))
            self.assertEqual(session.strategy.synchronized_frames,4)
            self.assertTrue(session.client.requests)

    def test_ema_original_strategy_consumes_dolphin_minutes(self):
        runtime = import_module("demos.01_main_ema.run_live")
        from bomber.framework.datahub.sector_roles import SectorRoleAssignment
        assignment = SectorRoleAssignment(date(2026,10,9),date(2026,10,8),BASE,BASE,
            {"RB":{"main":"rb2701"}}, {"RB":{"main":Decimal(1)}})
        refs = NS(trading_day=date(2026,10,9), started_ns=BASE, instrument_id=self.ids["RB"],
            spec=self.specs["RB"], factor_date=date(2026,10,8), factor_date_basis="source",
            snapshot=lambda stamp:assignment, instrument_meta=lambda:self.meta("RB"),
            refresh=lambda:None)
        self.args.fast, self.args.slow = 1, 2
        with patch.object(runtime.time,"time_ns",lambda:self.now):
            session = runtime.assemble(self.args, refs, None, self.upstream)
            self.assertEqual(type(session.strategy), import_module("demos.01_main_ema.strategy").MainEmaStrategy)
            session.runner.start()
            self.addCleanup(session.runner.stop)
            self.upstream._emit_bar(self.bar("RB",100))
            self.assertTrue(session.runner.session_ready())
            session.runner.begin_bars()
            for n in range(3):
                self.now = BASE + (n+2)*MINUTE
                self.upstream.latest_event_ns = self.upstream.latest_received_ns = self.now
                self.upstream._emit_bar(self.bar("RB",100+n*5))
            self.assertEqual(session.strategy.bars_used,3)
            self.assertEqual(session.strategy.last_target,Decimal(1))
            self.assertTrue(session.client.requests)

    def test_four_live_clis_share_source_config_and_reject_bn_before_connect(self):
        commands = {"01_main_ema":["--product","RB"], "03_calendar_spread":["--product","RB"],
            "04_cross_section":["--products","RB,HC"], "08_option_vega":[]}
        for demo, argv in commands.items():
            entry = import_module(f"demos.{demo}.run_live")
            base = ["--connect","--expected-trading-day","20261014", *argv]
            if demo != "08_option_vega": base += ["--expected-source-day","20261013"]
            args = entry.parse_args([*base,"--market-source","dolphindb","--market-bar-table","minute_stream",
                "--market-bar-timestamp","end","--market-volume-mode","per-bar"])
            self.assertEqual(args.market_source_config.source,"dolphindb")
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                entry.parse_args([*base,"--market-source","bn-ws"])
            with patch.dict(os.environ, {}, clear=True):
                if demo != "08_option_vega":
                    # Only construct lifecycle: no CTP MD/TD credentials required for DDB Recording.
                    runtime = entry.build_runtime(args)
                    self.assertIsNone(runtime.controller.driver)

    def test_local_source_runs_existing_history_hook_before_decisions(self):
        from bomber.framework.trader.runtime.live.local import RecordingExecutionLifecycle
        for history_fails in (False, True):
            trace = []
            runner = NS(failure=None, accept_bars=True,
                start=lambda:trace.append("start"),
                mark_history_live_start=lambda:trace.append("mark"),
                begin_bars=lambda:trace.append("begin"))
            runner.session_ready = lambda:runner.failure is None
            def prepare():
                self.assertFalse(runner.accept_bars)
                trace.append("history")
                if history_fails: runner.failure = "history lost market readiness"
            runner.prepare_history = prepare
            session = NS(runner=runner,manager=None,references=NS(refresh=lambda:trace.append("refresh")))
            lifecycle = RecordingExecutionLifecycle("20261014")
            if history_fails:
                with self.assertRaisesRegex(RuntimeError,"历史加载后"):
                    lifecycle.start(session,None)
                self.assertNotIn("begin",trace)
            else:
                lifecycle.start(session,None)
                self.assertEqual(trace,["start","refresh","mark","history","begin"])


if __name__ == "__main__":
    unittest.main()
