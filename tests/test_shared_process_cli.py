"""三策略应用配置/离线入口规则；不加载SDK或联网。"""

from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal
import argparse
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import threading
from types import SimpleNamespace
from unittest.mock import patch

from scripts.integration.shared_account import config, prepare_session, server, worker, preflight


class ProcessCliTests(unittest.TestCase):
    def test_im_if_profile_builds_pinned_contracts_observed_budget_and_three_history_windows(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            observed, output = root/"observed.json", root/"session.json"
            contracts = {"IM": {"main": "IM2612.CFFEX", "secondary": "IM2703.CFFEX"},
                "IF": {"main": "IF2612.CFFEX"}}
            strategies = {"main_ema": {"IM": {"main": "IM2610.CFFEX"}},
                "cross_section": {"IM": {"main": "IM2610.CFFEX"}, "IF": {"main": "IF2610.CFFEX"}},
                "calendar_spread": {"IM": contracts["IM"]}}
            instruments = ("IM2610.CFFEX", "IM2612.CFFEX", "IM2703.CFFEX", "IF2610.CFFEX")
            observed.write_text(json.dumps({"status": "observed", "orders_submitted": 0,
                "verification": "readonly_dates_and_contracts", "environment": "realtime", "profile": "im-if",
                "session_arguments": {"trading_day": "20261016", "source_day": "20261015", "replay_md_trading_day": None},
                "contracts": contracts,
                "strategy_contracts": strategies,
                "instrument_specs": {i: {"tick": "0.2", "multiplier": "300" if i.startswith("IF") else "200"} for i in instruments},
                "quotes": {i: {"bid": "5000" if i.startswith("IF") else "10000", "ask": "5001" if i.startswith("IF") else "10001"} for i in instruments}}))
            with redirect_stdout(StringIO()):
                prepare_session.main(["--output", str(output), "--environment", "realtime", "--execution-mode", "independent",
                    "--profile", "im-if", "--use-observed-budget", "--observations", str(observed)])
            c = config.read_config(output)
            self.assertEqual(c["account"]["max_session_orders"], 200)
            self.assertEqual(c["account"]["max_gross_quantity"], "5")
            self.assertGreater(int(c["account"]["max_gross_notional"]), 9000000)
            self.assertEqual([row["strategy_id"] for row in c["strategies"]],
                ["ema-im-three", "cross-im-if-three", "calendar-im-three"])
            for row, minutes in zip(c["strategies"], (240,21,120)):
                argv = row["argv"]
                self.assertEqual(argv[argv.index("--history-minutes")+1], str(minutes))
                self.assertTrue(Path(argv[argv.index("--history-config")+1]).is_file())
                self.assertIn("--expected-instrument", argv)
            calendar = c["strategies"][2]["argv"]
            self.assertEqual(calendar[calendar.index("--leg1-role")+1], "main")
            self.assertEqual(calendar[calendar.index("--leg2-role")+1], "secondary")
            ema, cross = (r["argv"] for r in c["strategies"][:2])
            self.assertEqual(ema[ema.index("--expected-instrument")+1], "IM2610.CFFEX")
            self.assertIn("IM=IM2610.CFFEX", cross)
            self.assertIn("IF=IF2610.CFFEX", cross)
            self.assertIn("main=IM2612.CFFEX", calendar)
            self.assertIn("secondary=IM2703.CFFEX", calendar)

    def test_im_if_old_role_only_observation_is_rejected_before_output(self):
        with TemporaryDirectory() as directory:
            observed, output = Path(directory)/"observed.json", Path(directory)/"session.json"
            observed.write_text(json.dumps({"status": "observed", "orders_submitted": 0,
                "verification": "readonly_dates_and_contracts", "environment": "realtime", "profile": "im-if",
                "contracts": {"IM": {"main": "IM2612.CFFEX", "secondary": "IM2703.CFFEX"},
                    "IF": {"main": "IF2612.CFFEX"}}}))
            with redirect_stderr(StringIO()) as stderr, self.assertRaises(SystemExit):
                prepare_session.main(["--output", str(output), "--environment", "realtime", "--execution-mode", "independent",
                    "--profile", "im-if", "--use-observed-budget", "--observations", str(observed)])
            self.assertIn("逐策略因子合约", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_observed_budget_uses_factor_contracts_for_signals_and_raw_roles_for_calendar(self):
        raw = {"IM": {"main": "IM2612.CFFEX", "secondary": "IM2703.CFFEX"}, "IF": {"main": "IF2612.CFFEX"}}
        actual = {"main_ema": {"IM": {"main": "IM2610.CFFEX"}},
            "cross_section": {"IM": {"main": "IM2610.CFFEX"}, "IF": {"main": "IF2610.CFFEX"}},
            "calendar_spread": {"IM": raw["IM"]}}
        prices = {"IM2610.CFFEX": 100, "IM2612.CFFEX": 200, "IM2703.CFFEX": 300, "IF2610.CFFEX": 400}
        specs = {i: {"tick": 1, "multiplier": 10} for i in prices}
        quotes = {i: {"bid": price, "ask": price} for i, price in prices.items()}
        budget = prepare_session.observed_budget(raw, specs, quotes, ("IM", "IF"), product="IM",
            roles=("main", "secondary"), strategy_contracts=actual)
        self.assertEqual(budget["quantity"], 5)
        self.assertEqual(Decimal(budget["notional"]), 11050)
        self.assertEqual([r["instrument"] for r in budget["legs"]],
            ["IM2610.CFFEX", "IM2610.CFFEX", "IF2610.CFFEX", "IM2612.CFFEX", "IM2703.CFFEX"])

    def test_server_fault_capture_uses_original_gate_and_diagnostic_failure_is_separate(self):
        from scripts.integration.shared_account.server import capture_execution_failure
        diagnostics = {"is_reconciled": False, "service_failed": True,
            "recovery_required": True, "report_errors": ["conflicting fill"],
            "portfolio_failure": "portfolio send failed", "audit": []}
        host = SimpleNamespace(snapshot=lambda: {"execution_diagnostics": dict(diagnostics)})
        captured = capture_execution_failure(host)
        diagnostics.update(service_failed=False, is_reconciled=True)
        self.assertTrue(captured["service_failed"])
        self.assertFalse(captured["is_reconciled"])
        self.assertEqual(captured["report_errors"], ["conflicting fill"])
        self.assertIsNone(capture_execution_failure(None))
        def failed_snapshot():
            raise ValueError("diagnostic unavailable")
        self.assertEqual(capture_execution_failure(SimpleNamespace(snapshot=failed_snapshot)),
            {"diagnostic_error": "diagnostic unavailable"})

    def test_assembly_refreshes_expired_prepared_reference_before_runner_binding(self):
        from bomber.framework.dataprep.live_cache import LiveReferenceCache
        from scripts.integration.shared_account.factories import assemble_strategy_session
        clock = [0]
        cache = LiveReferenceCache(clock_ns=lambda: clock[0])
        assignment = SimpleNamespace(effective_ns=0, available_ns=0)
        class References:
            def refresh(self):
                cache.refresh(lambda: (assignment, "original-route", {
                    "observed_at_ns": clock[0], "freshness_policy": {"max_observation_age_seconds": 30}}))
            @property
            def spec(self):
                cache.snapshot(clock[0])
                return cache.value[1]
        references = References()
        references.refresh()
        prepared = references.spec
        # 后续策略和账户查询耗时35秒，首个策略仍使用原30秒限制。
        clock[0] = 35_000_000_000
        with self.assertRaisesRegex(RuntimeError, "观测过期"):
            _ = references.spec
        calls = []
        def assemble(args, refs, driver, upstream, **kwargs):
            calls.append(refs.spec)
            return SimpleNamespace()
        with patch.dict("sys.modules", {"scripts.integration.shared_account.rollover":
                SimpleNamespace(validate_strategy_identity=lambda *args: None)}):
            assemble_strategy_session(SimpleNamespace(assemble=assemble), None,
                SimpleNamespace(references=references, upstream=None), service=None, driver=None,
                strategy_id="ema", prepared_reference=prepared)
        self.assertEqual(calls, ["original-route"])
        clock[0] += 31_000_000_000
        with self.assertRaisesRegex(RuntimeError, "观测过期"):
            _ = references.spec

    def test_assembly_reference_refresh_failure_never_binds_runner(self):
        from scripts.integration.shared_account.factories import assemble_strategy_session
        from unittest.mock import Mock
        refresh = Mock(side_effect=RuntimeError("数据库查询失败"))
        assemble = Mock()
        with self.assertRaisesRegex(RuntimeError, "数据库查询失败"):
            assemble_strategy_session(SimpleNamespace(assemble=assemble), None,
                SimpleNamespace(references=SimpleNamespace(refresh=refresh), upstream=None),
                service=None, driver=None, strategy_id="ema", prepared_reference="original")
        assemble.assert_not_called()

    def test_assembly_changed_reference_requires_repreparing_instrument_metadata(self):
        from scripts.integration.shared_account.factories import assemble_strategy_session
        from unittest.mock import Mock
        references = SimpleNamespace(spec="old")
        references.refresh = lambda: setattr(references, "spec", "new-contract-or-factor")
        assemble = Mock()
        with self.assertRaisesRegex(RuntimeError, "条款已变化"):
            assemble_strategy_session(SimpleNamespace(assemble=assemble), None,
                SimpleNamespace(references=references, upstream=None), service=None, driver=None,
                strategy_id="ema", prepared_reference="old")
        assemble.assert_not_called()

    def test_four_product_session_has_separate_books_and_requires_observation_coverage(self):
        with TemporaryDirectory() as directory:
            old, updated, observed = (Path(directory) / name for name in ("old.json", "four.json", "observed.json"))
            common = ["--environment", "realtime", "--execution-mode", "independent",
                "--trading-day", "20261015", "--source-day", "20261014"]
            with redirect_stdout(StringIO()):
                prepare_session.main(["--output", str(old), *common])
                prepare_session.main(["--output", str(updated), "--cross-products", "CU,AL,AG,AU", *common])
            before, after = config.read_config(old), config.read_config(updated)
            cross = next(row for row in after["strategies"] if row["kind"] == "cross_section")
            self.assertEqual(cross["strategy_id"], "cross-cu-al-ag-au-three")
            self.assertEqual(cross["argv"][cross["argv"].index("--products") + 1], "CU,AL,AG,AU")
            self.assertEqual(before["account"], after["account"])
            self.assertTrue({before["account_state_file"], *(r["state_file"] for r in before["strategies"])}.isdisjoint(
                {after["account_state_file"], *(r["state_file"] for r in after["strategies"])}))
            data = {"status": "observed", "orders_submitted": 0,
                "verification": "readonly_dates_and_contracts", "environment": "realtime",
                "session_arguments": {"trading_day": "20261015", "source_day": "20261014", "replay_md_trading_day": None},
                "contracts": {"CU": {"main": "cu2611.SHFE", "secondary": "cu2612.SHFE", "far": "cu2701.SHFE"},
                              "AL": {"main": "al2611.SHFE"}}}
            observed.write_text(json.dumps(data))
            arguments = ["--output", str(Path(directory) / "observed-four.json"),
                "--environment", "realtime", "--execution-mode", "independent",
                "--cross-products", "CU,AL,AG,AU", "--observations", str(observed)]
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                prepare_session.main(arguments)
            data["contracts"].update(AG={"main": "ag2612.SHFE"}, AU={"main": "au2612.SHFE"})
            observed.write_text(json.dumps(data))
            with redirect_stdout(StringIO()):
                prepare_session.main(arguments)
            self.assertEqual(prepare_session.parse_cross_products("cu, al, ag, au"), ("CU", "AL", "AG", "AU"))
            for value in ("CU", "CU,CU", "CU,", "CU,AU2612"):
                with self.assertRaises(argparse.ArgumentTypeError):
                    prepare_session.parse_cross_products(value)

    def test_observed_budget_counts_shared_contract_twice_and_rounds_limit_up(self):
        contracts = {"CU": {"main": "cu-main", "secondary": "cu-second", "far": "cu-far"},
            "AL": {"main": "al"}, "AG": {"main": "ag"}, "AU": {"main": "au"}}
        specs = {i: {"tick": 1, "multiplier": 10} for i in ("cu-main", "cu-second", "cu-far", "al", "ag", "au")}
        quotes = {i: {"bid": 100, "ask": 101} for i in specs}
        result = prepare_session.observed_budget(contracts, specs, quotes, ("CU", "AL", "AG", "AU"))
        self.assertEqual((result["quantity"], result["notional"], result["rounded_notional_limit"]), (7, "7140", "100000"))
        self.assertEqual(sum(row["instrument"] == "cu-main" for row in result["legs"]), 2)
        quotes["au"]["ask"] = "NaN"
        with self.assertRaises(ValueError):
            prepare_session.observed_budget(contracts, specs, quotes, ("CU", "AL", "AG", "AU"))

    def test_worker_reference_query_allows_market_thread_and_serializes_execution(self):
        lock, progressed, calls = threading.RLock(), threading.Event(), []
        def market_tick():
            with lock:
                progressed.set()
        def refresh():
            tick = threading.Thread(target=market_tick)
            tick.start()
            try:
                self.assertTrue(progressed.wait(1), "数据库查询阻塞行情提交锁")
            finally:
                tick.join(timeout=1)
            calls.append("reference")
        def drain():
            self.assertTrue(lock._is_owned())
            calls.append("execution")
        def save():
            self.assertTrue(lock._is_owned())
            calls.append("saved")
        runner = SimpleNamespace(_submit_lock=lock, failure=None, fixed_spec="same",
            session_ready=lambda: True)
        session = SimpleNamespace(runner=runner, references=SimpleNamespace(spec="same", refresh=refresh),
            client=SimpleNamespace(drain_events=drain))
        worker.poll_strategy_session(session, save)
        self.assertEqual(calls, ["reference", "execution", "saved"])
        runner.failure = "原行情故障"
        with self.assertRaisesRegex(RuntimeError, "原行情故障"):
            worker.poll_strategy_session(session, save)
        self.assertEqual(calls, ["reference", "execution", "saved"])

    def test_worker_failed_health_preserves_diagnostic_and_does_not_save_or_submit(self):
        context = {"channel_readiness": {"ready": False,
            "issues": [{"code": "INSTRUMENT_UNHEALTHY", "detail": "AL"}]}}
        session = SimpleNamespace(runner=SimpleNamespace(_submit_lock=threading.RLock(),
            failure=None, fixed_spec="old", session_ready=lambda: False),
            references=SimpleNamespace(refresh=lambda: None, spec="new"),
            client=SimpleNamespace(drain_events=lambda: None))
        saved = []
        with patch.object(worker, "capture_session_failure", return_value=context) as capture:
            with self.assertRaisesRegex(RuntimeError, "行情或账户未就绪.*INSTRUMENT_UNHEALTHY"):
                worker.poll_strategy_session(session, lambda: saved.append(True))
            capture.assert_called_once_with(session)
        self.assertFalse(saved)
        session.runner.session_ready = lambda: True
        with patch.object(worker, "capture_session_failure", return_value=context):
            with self.assertRaisesRegex(RuntimeError, "参考合约或条款变化"):
                worker.poll_strategy_session(session, lambda: saved.append(True))
        self.assertFalse(saved)

    def test_worker_checks_fixed_reference_without_requiring_optional_runner_method(self):
        references = SimpleNamespace(spec=("CU", "AL", "version-1"))
        runner = SimpleNamespace(fixed_spec=references.spec)
        session = SimpleNamespace(runner=runner, references=references)
        self.assertTrue(worker.session_reference_is_current(session))
        references.spec = ("CU", "AL", "version-2")
        self.assertFalse(worker.session_reference_is_current(session))
        runner.reference_is_current = lambda: True
        self.assertTrue(worker.session_reference_is_current(session))
        runner.reference_is_current = lambda: False
        references.spec = runner.fixed_spec
        self.assertFalse(worker.session_reference_is_current(session))

    def test_three_modes_have_distinct_paths_and_default_does_not_connect(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            with redirect_stdout(StringIO()):
                prepare_session.main(["--output", str(path), "--environment", "replay",
                    "--execution-mode", "independent", "--trading-day", "20261014",
                    "--source-day", "20261013", "--replay-md-trading-day", "20261013"])
            document = config.read_config(path)
            self.assertEqual({r["kind"] for r in document["strategies"]},
                {"main_ema", "calendar_spread", "cross_section"})
            self.assertEqual(document["execution_mode"], "independent")
            files = {document["account_state_file"], *(r["state_file"] for r in document["strategies"])}
            self.assertEqual(len(files), 4)
            before = path.read_bytes()
            output = StringIO()
            with redirect_stdout(output):
                server.main(["--config", str(path), "--socket", str(Path(directory) / "sock"),
                             "--credentials-dir", str(Path(directory) / "keys")])
                worker.main(["--config", str(path), "--strategy-id", "calendar-cu-three",
                             "--credentials", str(Path(directory) / "not-created")])
                preflight.main(["--config", str(path)])
            self.assertEqual(output.getvalue().count("未连接"), 3)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(any(Path(p).exists() for p in files))
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                prepare_session.main(["--output", str(path), "--environment", "realtime",
                    "--execution-mode", "independent", "--trading-day", "20261014", "--source-day", "20261013"])

    def test_authorization_and_mode_are_not_inferred_from_connect(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            with redirect_stdout(StringIO()):
                prepare_session.main(["--output", str(path), "--environment", "realtime",
                    "--execution-mode", "netted", "--trading-day", "20261014", "--source-day", "20261013"])
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                server.main(["--config", str(path), "--socket", "/tmp/test.sock", "--credentials-dir", "/tmp/test-keys"])
            document = json.loads(path.read_text())
            document["execution_mode"] = "independent"
            path.write_text(json.dumps(document))
            for entry, extra in ((server, ["--socket", "/tmp/test.sock", "--credentials-dir", "/tmp/test-keys"]),
                                 (worker, ["--strategy-id", "ema-cu-three", "--credentials", "/tmp/no-file"])):
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    entry.main(["--config", str(path), "--connect", *extra])

    def test_dates_come_from_successful_readonly_observation_of_same_environment(self):
        with TemporaryDirectory() as directory:
            observed, output = Path(directory) / "observed.json", Path(directory) / "session.json"
            data = {"status": "observed", "orders_submitted": 0,
                "verification": "readonly_dates_and_contracts", "environment": "replay",
                "session_arguments": {"trading_day": "20261014", "source_day": "20261013",
                                      "replay_md_trading_day": "20261012"}}
            observed.write_text(json.dumps(data))
            with redirect_stdout(StringIO()):
                prepare_session.main(["--output", str(output), "--environment", "replay",
                    "--execution-mode", "independent", "--observations", str(observed)])
            self.assertEqual(config.read_config(output)["replay_md_trading_day"], "20261012")
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                prepare_session.main(["--output", str(Path(directory) / "wrong.json"), "--environment", "realtime",
                    "--execution-mode", "independent", "--observations", str(observed)])


if __name__ == "__main__":
    unittest.main()
