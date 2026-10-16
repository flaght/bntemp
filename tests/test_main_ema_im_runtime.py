"""正式IM Recording装配/生命周期与历史实时衔接；假柜台，不连接网络。"""
from contextlib import ExitStack, redirect_stderr
from datetime import datetime
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch
import os

import pandas as pd

from bomber.framework.calendars import SessionCalendar
from bomber.framework.trader.runtime.trading_sessions import SHANGHAI
from tests import run_main_ema_live as live_fixture
from tests.session_fixtures import index_sessions

ManualMd, BASE, MINUTE = live_fixture.ManualMd, live_fixture.BASE, live_fixture.MINUTE
live, refs = live_fixture.live, live_fixture.refs

OPEN = BASE + 30 * MINUTE


class ImRuntimeTests(unittest.TestCase):
    def fixture(self, product="IM"):
        fixture = live_fixture.LiveTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        symbol = f"{product}2610"
        pd.DataFrame([{"trade_date": "2026-09-21", "code": product, "main": symbol}]).to_feather(fixture.role_path)
        pd.DataFrame([{"trade_date": "2026-09-22", "code": product, "symbol": symbol,
            "pcr_cumfactor": "1.456591"}]).to_feather(fixture.factor_path)
        pd.DataFrame([{"symbol": symbol, "code": product, "exchangeCD": "CCFX",
            "contMultNum": 300 if product == "IF" else 200, "minChgPriceNum": 0.2,
            "listDate": "2026-01-01", "lastTradeDate": "2026-10-16"}]).to_feather(fixture.basic_path)
        fixture.now = OPEN + 2 * MINUTE
        references = refs.FileRoleReferences(product=product, trading_day="20260922",
            contract_struct=fixture.role_path, factors=fixture.factor_path, fut_basic=fixture.basic_path,
            started_ns=OPEN, allowed_venues=("CFFEX",))
        self.assertEqual(str(references.instrument_id), f"{symbol}.CFFEX",
            f"CFFEX标识应为大写；实际加载参考模块={refs.__file__}，"
            f"spec.symbol={references.spec.symbol!r} venue={references.spec.venue!r}；"
            "请核对公共live_role.py与PF0916部署摘要")
        return fixture, references

    def recording_controller(self):
        env = {"CTP_MD_ADDRESS": "tcp://fake:1234", "CTP_ACCOUNT_ID": "demo", "CTP_PASSWORD": "fake"}
        with patch.dict(os.environ, env, clear=True):
            return live.MainEmaRecordingLifecycle(expected_trading_day="20260922")

    def args(self, fixture, **changes):
        path = fixture.root / "im-history.jsonl"
        values = dict(mode="recording", product="IM", expected_instrument="IM2610.CFFEX",
            fast=1, slow=2, quantity=Decimal(1), limit_offset_ticks=1,
            max_notional=Decimal(2000000), state_file=None, history_minutes=2,
            history_config=None, history_db="dolphindb", history_file=path, history_missing_policy="fail",
            history_stage="preopen", reference_timeout=15, trading_calendar=None)
        values.update(changes)
        values["_session_calendar"] = index_sessions(values["product"])
        if values["product"] == "IF" and "max_notional" not in changes:
            values["max_notional"] = Decimal(3000000)
        factor = Decimal("1.456591")
        path.write_text("\n".join(json.dumps({"instrument_id": f"{values['product']}2610.CFFEX",
            "ts_event": OPEN + (index + 1) * MINUTE - 1,
            "adjusted_close": str(Decimal("7760") * factor)}) for index in range(2)))
        return SimpleNamespace(**values)

    def test_recording_runtime_needs_only_md_credentials_and_no_td_transport(self):
        fixture, _ = self.fixture()
        args = live.parse_args(["--connect", "--product", "IM", "--expected-instrument", "IM2610.CFFEX",
            "--expected-trading-day", "20260922", "--expected-source-day", "20260921"])
        env = {"CTP_MD_ADDRESS": "tcp://fake:1234", "CTP_ACCOUNT_ID": "demo", "CTP_PASSWORD": "fake"}
        with patch.dict(os.environ, env, clear=True), patch.object(live, "build_simnow_transport",
                side_effect=AssertionError("Recording不得创建TD")):
            runtime = live.build_runtime(args)
        self.assertIsNone(runtime.controller.driver)
        self.assertEqual(args.execution_policy.account_reconciliation.value, "off")
        self.assertIsInstance(runtime.controller, live.MainEmaRecordingLifecycle)
        with ExitStack() as resources:
            for namespace in ("bomber-ctp", "bomber-main-ema"):
                resources.enter_context(live_fixture.account_lock("demo", namespace=namespace))
            context = runtime.controller.prepare(resources)
            self.assertEqual(context.trading_day, "20260922")

    def test_if_calendar_and_cli_use_main_without_fixed_contract(self):
        calendar = index_sessions("IF")
        self.assertEqual(calendar.product, "IF")
        self.assertEqual(calendar.previous_trading_day("20260922"), "20260921")
        self.assertEqual(len(calendar.last_minutes(OPEN, 240)), 240)
        self.assertIsNone(calendar.window(datetime(2026, 9, 22, 12, tzinfo=SHANGHAI)))
        with self.assertRaises(RuntimeError): calendar.last_minutes(OPEN, 241)
        base = ["--connect", "--product", "IF", "--expected-source-day", "20260921",
            "--history-minutes", "240", "--calendar-id", "china.cffex", "--no-session-overrides"]
        args = live.parse_args(base)
        self.assertIsNone(args.trading_calendar)
        self.assertIsNone(args.expected_instrument)
        self.assertEqual(args.mode, "recording")
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            live.parse_args([*base, "--expected-instrument", "IF2610.SHFE"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            live.parse_args([*base, "--mode", "simnow"])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            live.parse_args([*base, "--run-forever"])

    def test_if_main_preloads_history_and_only_new_bar_records_target(self):
        fixture, references = self.fixture("IF")
        args = self.args(fixture, product="IF", expected_instrument=None)
        session = live.assemble(args, references, None, ManualMd())
        fixture.addCleanup(session.runner.stop)
        controller = self.recording_controller()
        controller.start(session, SimpleNamespace(trading_day="20260922"))
        self.assertEqual(str(session.references.instrument_id), "IF2610.CFFEX")
        self.assertEqual(session.strategy.bars_used, 2)
        self.assertEqual(session.strategy._revision, 0)
        self.assertEqual(session.client.requests, ())
        fixture.tick(session, 32, price=7760)
        fixture.tick(session, 33, price=7760)
        self.assertEqual(session.strategy.bars_used, 3)
        self.assertEqual(len(session.client.requests), 1)
        self.assertIsNone(session.driver)
        self.assertIsNone(session.ledger)
        self.assertEqual(session.strategy.fills_received, 0)
        self.assertTrue(controller.poll(session))
        controller.verify(session)
        result = controller.shutdown(session, None)
        self.assertEqual(result["cleanup_errors"], [])
        snapshot = controller.snapshot(session, SimpleNamespace(references=references))
        self.assertEqual(snapshot["observed_md_trading_day"], "20260922")
        self.assertIsNone(snapshot["observed_td_trading_day"])
        self.assertEqual(snapshot["execution_origin"], "recording")
        with self.assertRaises(ValueError):
            live.assemble(self.args(fixture, product="IF", mode="simnow", expected_instrument=None,
                trading_calendar=args.trading_calendar), references, None, ManualMd())

    def test_if_strict_history_gap_keeps_bar_gate_closed(self):
        from bomber.framework.dataprep.history import HistoryUnavailable
        fixture, references = self.fixture("IF")
        args = self.args(fixture, product="IF", expected_instrument=None, history_minutes=3)
        session = live.assemble(args, references, None, ManualMd())
        fixture.addCleanup(session.runner.stop)
        controller = self.recording_controller()
        with self.assertRaises(HistoryUnavailable):
            controller.start(session, SimpleNamespace(trading_day="20260922"))
        self.assertFalse(session.runner.accept_bars)
        self.assertEqual(session.strategy.bars_used, 0)
        self.assertEqual(session.client.requests, ())
        self.assertIsNone(session.driver)
        self.assertIsNone(session.ledger)

    def test_calendar_factory_im_sessions_lunch_and_previous_day(self):
        calendar = index_sessions("IM")
        self.assertIsInstance(calendar, SessionCalendar)
        def moment(value): return datetime.fromisoformat("2026-09-22T" + value).replace(tzinfo=SHANGHAI)
        self.assertIsNone(calendar.window(moment("09:29:59")))
        self.assertIsNotNone(calendar.window(moment("09:30:00")))
        self.assertIsNone(calendar.window(moment("11:30:00")))
        self.assertIsNotNone(calendar.window(moment("13:00:00")))
        self.assertIsNone(calendar.window(moment("15:00:00")))
        self.assertEqual(calendar.previous_trading_day("20260922"), "20260921")
        cutoff = int(moment("15:00:00").timestamp()) * 10**9
        self.assertEqual(len(calendar.last_minutes(cutoff, 240)), 240)
        previous_close = int(datetime(2026, 9, 21, 15, tzinfo=SHANGHAI).timestamp()) * 10**9 - 1
        self.assertEqual(calendar.last_minutes(OPEN, 1)[0], previous_close)

    def test_calendar_unknown_day_and_insufficient_coverage_fail(self):
        calendar = index_sessions("IM")
        with self.assertRaises(RuntimeError):
            calendar.window(datetime(2026, 9, 23, 9, 30, tzinfo=SHANGHAI))
        with self.assertRaises(RuntimeError): calendar.last_minutes(OPEN, 241)
        with self.assertRaises(ValueError): calendar.window(datetime(2026, 9, 22, 9, 30))

    def test_cli_requires_expected_im_contract_and_correct_history_calendar(self):
        base = ["--connect", "--product", "IM", "--expected-source-day", "20260921"]
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit): live.parse_args(base)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            live.parse_args([*base, "--expected-instrument", "IM2610.CFFEX", "--history-minutes", "240"])
        args = live.parse_args([*base, "--expected-instrument", "IM2610.CFFEX", "--history-minutes", "240",
            "--calendar-id", "china.cffex", "--no-session-overrides"])
        self.assertEqual(args.mode, "recording")
        self.assertIsNone(args.trading_calendar)

    def test_formal_recording_lifecycle_preloads_history_then_records_only_new_minute(self):
        fixture, references = self.fixture()
        session = live.assemble(self.args(fixture), references, None, ManualMd())
        fixture.addCleanup(session.runner.stop)
        controller = self.recording_controller()
        controller.start(session, SimpleNamespace(trading_day="20260922"))
        self.assertEqual(str(session.references.instrument_id), "IM2610.CFFEX")
        self.assertEqual(session.strategy.bars_used, 2)
        self.assertEqual(session.strategy._revision, 0)
        self.assertEqual(session.client.requests, ())
        fixture.tick(session, 32, price=7760)
        fixture.tick(session, 33, price=7760)
        self.assertEqual(session.strategy.bars_used, 3)
        self.assertEqual(len(session.client.requests), 1)
        self.assertIsNone(session.driver)
        self.assertIsNone(session.ledger)
        self.assertEqual(session.strategy.fills_received, 0)
        self.assertTrue(controller.poll(session))
        controller.verify(session)
        result = controller.shutdown(session, None)
        self.assertEqual(result["cleanup_errors"], [])
        snapshot = controller.snapshot(session, SimpleNamespace(references=references))
        self.assertEqual(snapshot["observed_md_trading_day"], "20260922")
        self.assertIsNone(snapshot["observed_td_trading_day"])
        self.assertEqual(snapshot["execution_origin"], "recording")

    def test_assembly_rejects_im_orders_and_wrong_expected_contract(self):
        fixture, references = self.fixture()
        transport, driver, _ = fixture.transport_driver()
        with self.assertRaises(ValueError):
            live.assemble(self.args(fixture, mode="simnow"), references, driver, ManualMd())
        with self.assertRaises(ValueError):
            live.assemble(self.args(fixture, expected_instrument="IM2611.CFFEX"), references, driver, ManualMd())

    def test_formal_history_gap_keeps_recording_bar_gate_closed(self):
        from bomber.framework.dataprep.history import HistoryUnavailable
        fixture, references = self.fixture()
        session = live.assemble(self.args(fixture, history_minutes=3), references, None, ManualMd())
        fixture.addCleanup(session.runner.stop)
        controller = self.recording_controller()
        with self.assertRaises(HistoryUnavailable):
            controller.start(session, SimpleNamespace(trading_day="20260922"))
        self.assertFalse(session.runner.accept_bars)
        self.assertEqual(session.strategy.bars_used, 0)
        self.assertEqual(session.client.requests, ())
        self.assertIsNone(session.driver)
        self.assertIsNone(session.ledger)


if __name__ == "__main__":
    unittest.main(verbosity=2)
