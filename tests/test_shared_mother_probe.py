"""受控双来源母单入口：纯预检规则与真实原生组件、替身传输的装配验证。"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from scripts.integration.shared_account import mother_probe as probe
from tests import test_shared_account as fixtures


class ProbePreflightTests(unittest.TestCase):
    def args(self, *extra):
        return probe.parse_args(["--trading-day", "20261014", *extra])

    def spec(self):
        return NS(symbol="CU2612", venue="SHFE", currency="CNY", product="CU",
                  listed=date(2025, 1, 1), last_trade=date(2026, 12, 15),
                  tick=Decimal(10), multiplier=Decimal(5))

    def quote(self):
        return {"BidPrice1": "111000", "AskPrice1": "111010", "LastPrice": "111000",
                "LowerLimitPrice": "100000", "UpperLimitPrice": "120000"}, NS(
                    trading_day="20261014", timestamp_regressed=False,
                    received_monotonic_ns=time.monotonic_ns(), ts_event=time.time_ns())

    def test_default_does_not_connect_and_submit_requires_concrete_plan(self):
        output = StringIO()
        with redirect_stdout(output):
            probe.main(["--trading-day", "20261014"])
        self.assertIn("未连接", output.getvalue())
        for extra in (("--submit",), ("--connect", "--submit", "--confirm-simnow"),
                      ("--max-notional", "NaN"), ("--max-notional", "2300001"),
                      ("--limit-price", "abc"),
                      ("--passive-ticks", "0"), ("--partial-wait-seconds", "0"),
                      ("--instrument", "cu2612.DCE"), ("--timeout", "0")):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                self.args(*extra)
        self.assertTrue(self.args("--connect", "--submit", "--confirm-simnow",
                                 "--instrument", "cu2612.SHFE", "--limit-price", "110900").submit)

    def test_protected_checkpoint_is_read_only_and_zero_net_contract_is_excluded(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "original.json"
            path.write_text(json.dumps({"payload": {"component_states": {"shared_account": {
                "known_instruments": ["cu2612.SHFE"]}}}}))
            before = path.read_bytes()
            instruments, hashes = probe.protected_state([path])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(len(hashes), 1)
            with self.assertRaisesRegex(RuntimeError, "禁止接管"):
                probe.select_spec([self.spec()], self.args(), {}, instruments)

    def test_selection_excludes_gross_hedges_and_near_expiry(self):
        spec = self.spec()
        for gross in ({"cu2612.SHFE": (1, 1)}, {"cu2612.SHFE": (1, 0)}):
            with self.assertRaises(RuntimeError):
                probe.select_spec([spec], self.args(), gross, set())
        spec.last_trade = self.args().day + timedelta(days=1)
        with self.assertRaises(RuntimeError):
            probe.select_spec([spec], self.args(), {}, set())

    def test_passive_price_and_two_source_budget_come_from_current_quote(self):
        plan = probe.price_plan(*self.quote(), self.args(), self.spec())
        self.assertEqual(plan["quantity"], 2)
        self.assertEqual(plan["limit_price"], "110900")
        self.assertEqual(plan["notional"], "1110000")
        for extra in (("--limit-price", "111000"), ("--limit-price", "110905"),
                      ("--limit-price", "99990"), ("--max-notional", "1100000")):
            with self.assertRaises(RuntimeError):
                probe.price_plan(*self.quote(), self.args(*extra), self.spec())

    def test_stale_wrong_day_regressed_and_invalid_quotes_fail_closed(self):
        for change in ({"trading_day": "20261015"}, {"timestamp_regressed": True},
                       {"received_monotonic_ns": time.monotonic_ns() - 4_000_000_000},
                       {"ts_event": time.time_ns() - 11_000_000_000}):
            raw, observation = self.quote()
            vars(observation).update(change)
            with self.assertRaises(RuntimeError):
                probe.price_plan(raw, observation, self.args(), self.spec())

    def test_partial_mode_allows_own_best_price_but_does_not_cross_spread(self):
        args = self.args("--cancel-after", "partial", "--passive-ticks", "0")
        self.assertEqual(probe.price_plan(*self.quote(), args, self.spec())["limit_price"], "111000")
        args.limit_price = Decimal("111010")
        with self.assertRaises(RuntimeError):
            probe.price_plan(*self.quote(), args, self.spec())
        # 挂单期间行情穿过原限价不能阻止撤单；仍检查时间、边界和预算。
        probe.price_plan(*self.quote(), args, self.spec(), enforce_passive=False)
        for bad in (None, "NaN", "1.7976931348623157e308"):
            raw, observation = self.quote()
            raw["BidPrice1"] = bad
            with self.assertRaises(RuntimeError):
                probe.price_plan(raw, observation, self.args(), self.spec())


class ProbeNativeTests(unittest.TestCase):
    setUp = fixtures.SharedAccountTests.setUp
    native_modules = fixtures.SharedAccountTests.native_modules
    native_account = fixtures.SharedAccountTests.native_account
    callback_host = fixtures.SharedAccountTests.callback_host

    def partial_case(self, cleanup):
        directory = cleanup.enter_context(TemporaryDirectory())
        host, transport, rows = self.callback_host(Path(directory))
        cleanup.callback(host.stop)
        host.probe_side = "BUY"
        return host, transport, rows

    def test_wait_partial_then_cancel_keeps_real_native_frozen_attribution(self):
        with ExitStack() as cleanup:
            host, transport, rows = self.partial_case(cleanup)
            def partial(_seconds):
                transport.fill(transport.sent[0]["OrderRef"], 1)
            with patch.object(probe.time, "sleep", side_effect=partial):
                result = probe.exercise(host, self.instrument, Decimal(2990), lambda: None,
                    timeout=1, cancel_after="partial", partial_wait_seconds=1)
            events = tuple(host.account.execution.backend.reports)
            self.assertEqual(probe.partial_cancel_result(result, events, "completed")["status"], "passed")
            self.assertEqual(result.wait_outcome, "partial_observed")
            self.assertEqual(len(transport.sent), 1)
            self.assertEqual((host.service.position_manager.position("one", "main"),
                              host.service.position_manager.position("two", "main")), (1, 0))
            self.assertTrue(rows[1].active)
            self.assertEqual(rows[0].session.client.event_diagnostics()["fills_delivered"], 1)
            self.assertEqual(probe.partial_cancel_result(result, events, "failed")["status"], "failed")

    def test_partial_wait_timeout_cancels_but_does_not_pass_partial_acceptance(self):
        with ExitStack() as cleanup:
            host, transport, rows = self.partial_case(cleanup)
            result = probe.exercise(host, self.instrument, Decimal(2990), lambda: None,
                timeout=1, cancel_after="partial", partial_wait_seconds=0)
            self.assertEqual(result.wait_outcome, "partial_wait_timeout")
            self.assertEqual(len(transport.canceled), 1)
            self.assertEqual(result.result(host, runtime_status="completed")["status"], "passed")
            self.assertEqual(probe.partial_cancel_result(result,
                host.account.execution.backend.reports, "completed")["status"], "not_exercised")
            self.assertFalse(rows[0].session.strategy.fills)

    def test_full_fill_before_partial_cancel_is_not_exercised_and_both_books_preserved(self):
        with ExitStack() as cleanup:
            host, transport, rows = self.partial_case(cleanup)
            with patch.object(probe.time, "sleep", side_effect=lambda seconds:
                              transport.fill(transport.sent[0]["OrderRef"])):
                result = probe.exercise(host, self.instrument, Decimal(2990), lambda: None,
                    timeout=1, cancel_after="partial", partial_wait_seconds=1)
            self.assertEqual(result.wait_outcome, "filled_before_cancel")
            self.assertEqual(probe.partial_cancel_result(result,
                host.account.execution.backend.reports, "completed")["status"], "not_exercised")
            self.assertFalse(transport.canceled)
            self.assertEqual((host.service.position_manager.position("one", "main"),
                              host.service.position_manager.position("two", "main")), (1, 1))

    def test_partial_only_during_timeout_cancel_does_not_prove_partial_trigger(self):
        with ExitStack() as cleanup:
            host, transport, rows = self.partial_case(cleanup)
            original_cancel = transport.cancel_order
            def late(ref):
                transport.fill(ref, 1)
                original_cancel(ref)
            transport.cancel_order = late
            result = probe.exercise(host, self.instrument, Decimal(2990), lambda: None,
                timeout=1, cancel_after="partial", partial_wait_seconds=0)
            self.assertEqual(probe.partial_cancel_result(result,
                host.account.execution.backend.reports, "completed")["status"], "not_exercised")
            self.assertEqual(host.service.position_manager.position("one", "main"), 1)

    def test_market_failure_during_partial_wait_still_cleans_up_live_mother(self):
        with ExitStack() as cleanup:
            host, transport, rows = self.partial_case(cleanup)
            def stale():
                raise RuntimeError("market stale")
            with self.assertRaisesRegex(RuntimeError, "market stale"):
                probe.exercise(host, self.instrument, Decimal(2990), lambda: None,
                    timeout=1, cancel_after="partial", partial_wait_seconds=1, observe_health=stale)
            host.stop()  # 实际入口finally同样走原宿主停机，不能遗留挂单。
            self.assertEqual(len(transport.sent), 1)
            self.assertEqual(len(transport.canceled), 1)
            self.assertEqual(host.account.final_active_orders, 0)

    def test_readonly_authenticated_td_passes_identity_check_without_enabling_orders(self):
        with TemporaryDirectory() as directory:
            modules = self.native_modules()
            _, _, transport, _, _ = self.native_account(Path(directory) / "unused.json", modules)
            driver = modules.driver.CtpNativeTraderDriver("account", "demo", transport)
            self.addCleanup(driver.stop)
            driver.start(lambda report: None)
            args = probe.parse_args(["--connect", "--trading-day", "20261013"])
            self.assertTrue(driver.is_simnow_authenticated)
            self.assertFalse(driver.is_simnow_session)
            probe.validate_td(driver, args)
            args.submit = True
            with self.assertRaisesRegex(RuntimeError, "发单授权"):
                probe.validate_td(driver, args)
            self.assertFalse(transport.sent)
            args.submit = False
            args.trading_day = "20261014"
            with self.assertRaisesRegex(RuntimeError, "交易日不符"):
                probe.validate_td(driver, args)
            driver.stop()
            self.assertFalse(driver.is_simnow_authenticated)
            args.trading_day = "20261013"
            with self.assertRaises(RuntimeError):
                probe.validate_td(driver, args)

    def test_real_probe_builder_preserves_external_positions_and_uses_fresh_state(self):
        with ExitStack() as cleanup:
            directory = Path(cleanup.enter_context(TemporaryDirectory()))
            modules = self.native_modules()
            unused, _, transport, _, resources = self.native_account(directory / "unused.json", modules, scoped=True)
            transport.gross = {"ag2612.SHFE": (0, 1)}
            args = probe.parse_args(["--trading-day", "20261013", "--connect", "--submit",
                "--confirm-simnow", "--instrument", "cu2612.SHFE", "--limit-price", "2990"])
            spec = NS(tick=Decimal(1), multiplier=Decimal(1))
            holder = {}
            def validate():
                if holder:
                    holder["host"].service.prices.update(self.instrument, 3000, time.time_ns() - 1_000_000_000)
            driver = modules.driver.CtpNativeTraderDriver("account", "demo", transport,
                enable_simnow_orders=True, max_session_orders=1, disconnect_handler=lambda reason: None)
            host = probe.build_host(args, driver, transport, spec, self.instrument,
                                    validate, resources, directory / "probe-account.json")
            holder["host"] = host
            validate()
            host.start()
            cleanup.callback(host.stop)
            result = probe.exercise(host, self.instrument, Decimal(2990), validate, timeout=1)
            self.assertEqual(result.mother["submitting_strategy"], "mother-probe-two")
            self.assertEqual(result.result(host, runtime_status="completed")["status"], "passed")
            host.stop()
            self.assertEqual(host.account.final_gross, {"ag2612.SHFE": (Decimal(0), Decimal(1))})
            self.assertEqual(host.account.final_active_orders, 0)
            self.assertTrue((directory / "probe-account.json").exists())
            self.assertFalse((directory / "unused.json").exists())

    def test_two_sources_form_one_explicit_limit_mother_and_non_submitter_cancels(self):
        with ExitStack() as cleanup:
            directory = cleanup.enter_context(TemporaryDirectory())
            host, transport, rows = self.callback_host(Path(directory))
            cleanup.callback(host.stop)
            host.probe_side = "BUY"
            result = probe.exercise(host, self.instrument, Decimal(2990), lambda: None, timeout=1)
            self.assertEqual(len(transport.sent), 1)
            self.assertEqual(transport.sent[0]["VolumeTotalOriginal"], 2)
            self.assertEqual(transport.sent[0]["LimitPrice"], 2990)
            self.assertEqual(transport.canceled, [transport.sent[0]["OrderRef"]])
            self.assertEqual([leg["quantity"] for leg in result.mother["legs"]], ["1", "1"])
            self.assertEqual(result.mother["submitting_strategy"], "two")
            self.assertFalse(rows[0].active)
            self.assertTrue(rows[1].active)
            self.assertEqual(host.service.snapshot_state()["sources"]["two"]["targets"][str(self.instrument)], "1")
            self.assertEqual(result.result(host, runtime_status="completed")["status"], "passed")
            self.assertEqual(host.service.position_manager.account_position("account", self.instrument), 0)

    def test_unexpected_partial_fill_keeps_attribution_and_is_reported(self):
        with ExitStack() as cleanup:
            directory = cleanup.enter_context(TemporaryDirectory())
            host, transport, rows = self.callback_host(Path(directory))
            cleanup.callback(host.stop)
            host.probe_side = "BUY"
            cancel = transport.cancel_order
            def partial_then_cancel(ref):
                transport.fill(ref, 1)
                cancel(ref)
            transport.cancel_order = partial_then_cancel
            result = probe.exercise(host, self.instrument, Decimal(2990), lambda: None, timeout=1)
            self.assertEqual(len(transport.sent), 1)
            self.assertTrue(result.result(host, runtime_status="completed")["partial_fill_observed"])
            self.assertEqual(host.service.position_manager.position("one", "main"), 1)
            self.assertEqual(host.service.position_manager.position("two", "main"), 0)
            self.assertEqual(rows[0].session.client.event_diagnostics()["fills_delivered"], 1)
            self.assertTrue(rows[1].active)


if __name__ == "__main__":
    unittest.main()
