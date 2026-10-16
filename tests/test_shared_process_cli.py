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

from scripts.integration.shared_account import config, prepare_session, prepare_night, server, worker, preflight


class ProcessCliTests(unittest.TestCase):
    def test_ctp_age_policy_validates_without_changing_worker_recovery_identity(self):
        from copy import deepcopy
        from scripts.integration.shared_account.migrate_scope import worker_identity
        with TemporaryDirectory() as directory:
            p = Path(directory)/"session.json"
            with redirect_stdout(StringIO()):
                prepare_session.main(["--output", str(p), "--environment", "realtime",
                    "--execution-mode", "independent", "--trading-day", "20261019", "--source-day", "20261016"])
            original = config.read_config(p)
            raw = json.loads(p.read_text())
            raw["ctp_instrument_max_age_seconds"] = 30
            p.write_text(json.dumps(raw))
            updated = config.read_config(p)
            self.assertEqual(config.ctp_instrument_max_age_seconds(updated), 30)
            for old, new in zip(original["strategies"], updated["strategies"]):
                self.assertEqual(worker_identity(old, original), worker_identity(new, updated))
            for invalid in (False, True, 0, -1, 121, "30", float("nan"), float("inf")):
                candidate = deepcopy(raw)
                candidate["ctp_instrument_max_age_seconds"] = invalid
                p.write_text(json.dumps(candidate))
                with self.assertRaisesRegex(ValueError, "ctp_instrument_max_age_seconds"):
                    config.read_config(p)
            self.assertEqual(config.ctp_instrument_max_age_seconds(original), 10)

    def test_ctp_age_policy_reaches_all_three_factories_without_editing_original_argv(self):
        from scripts.integration.shared_account import factories
        from copy import deepcopy
        from argparse import Namespace
        class Entry:
            def parse_args(self, argv, **kwargs):
                parser = argparse.ArgumentParser()
                parser.add_argument("--instrument-max-age-seconds", type=float, default=10)
                parsed, _ = parser.parse_known_args(argv)
                return parsed
        row = {"kind": "main_ema", "argv": ["--instrument-max-age-seconds", "10"], "state_file": "old-worker.json"}
        c = {"trading_day": "20261019", "seconds": 60, "environment": "realtime", "ctp_instrument_max_age_seconds": 30}
        original = deepcopy(row["argv"])
        with patch.object(factories, "import_module", return_value=Entry()):
            for kind in ("main_ema", "calendar_spread", "cross_section"):
                row["kind"] = kind
                _, args = factories.strategy_arguments(row, c, resume=True)
                self.assertEqual(args.instrument_max_age_seconds, 30)
                self.assertEqual(row["argv"], original)
            c.pop("ctp_instrument_max_age_seconds")
            self.assertEqual(factories.strategy_arguments(row, c, resume=True)[1].instrument_max_age_seconds, 10)
        entry = SimpleNamespace(parse_args=lambda *args, **kwargs: Namespace(market_source="dolphindb", market_bar_table="bars",
            market_bar_timestamp="end", market_volume_mode="per-bar"))
        c["ctp_instrument_max_age_seconds"] = 30
        with patch.object(factories, "import_module", return_value=entry), self.assertRaisesRegex(ValueError, "CTP行情"):
            factories.strategy_arguments(row, c, resume=False)

    def night_observation(self, path):
        contracts = {p: {"main": p.lower()+"2612."+("INE" if p == "SC" else "SHFE")}
                     for p in prepare_night.PRODUCTS}
        contracts["CU"].update(secondary="cu2701.SHFE", far="cu2702.SHFE")
        routes = {"main_ema": {"CU": {"main": "cu2611.SHFE"}},
            "cross_section": {p: {"main": "cu2611.SHFE" if p == "CU" else v["main"]} for p, v in contracts.items()},
            "calendar_spread": {"CU": contracts["CU"]}}
        instruments = {i for products in routes.values() for roles in products.values() for i in roles.values()}
        data = {"status": "observed", "orders_submitted": 0, "verification": "readonly_dates_and_contracts",
            "environment": "realtime", "profile": "commodity", "cross_products": list(prepare_night.PRODUCTS),
            "session_arguments": {"trading_day": "20261019", "source_day": "20261016", "replay_md_trading_day": None},
            "contracts": contracts, "strategy_contracts": routes,
            "instrument_specs": {i: {"tick": "1", "multiplier": "1000" if i.startswith("au") else "5"} for i in instruments},
            "quotes": {i: {"bid": "1000" if i.startswith("au") else "100000",
                           "ask": "1001" if i.startswith("au") else "100001"} for i in instruments}}
        path.write_text(json.dumps(data))
        return data

    def test_night_prepare_ignores_existing_generated_and_reuses_identical_config_without_rewrite(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)/"night-cu-1792135000000000000"
            root.mkdir()
            observed, env = root/"observed.json", Path(directory)/"night.env"
            self.night_observation(observed)
            generated = root/"generated.json"
            generated.write_bytes(b"original intermediate file left by previous command")
            argv = ["--observations", str(observed), "--output-dir", str(root), "--env-file", str(env)]
            with redirect_stdout(StringIO()):
                prepare_night.main(argv)
            out = root/"session.json"
            original = out.read_bytes(), out.stat().st_mtime_ns
            c = config.read_config(out)
            self.assertEqual(c["account"]["max_gross_quantity"], "7")
            self.assertEqual(c["account"]["max_session_orders"], 200)
            self.assertEqual(c["existing_position_policy"], "preserve_external")
            self.assertEqual(c["account"]["max_gross_notional"], "4100000")
            self.assertEqual(c["account_state_file"], str((root/"account.json").resolve()))
            for row in c["strategies"]:
                a = row["argv"]
                self.assertEqual(a[a.index("--history-minutes")+1], "0")
                self.assertNotIn("--history-config", a)
                self.assertNotIn("--history-file", a)
                self.assertFalse(Path(row["state_file"]).exists())
                if row["kind"] == "cross_section":
                    self.assertEqual(a[a.index("--products")+1], "CU,AU,AG,SC")
                    self.assertEqual(a[a.index("--max-notional")+1], "1100000")
                    self.assertEqual(a[a.index("--target-notional")+1], "2100000")
                    self.assertIn("CU=cu2611.SHFE", a)
            with redirect_stdout(StringIO()):
                prepare_night.main(argv)
            self.assertEqual((out.read_bytes(), out.stat().st_mtime_ns), original)
            self.assertEqual(generated.read_bytes(), b"original intermediate file left by previous command")
            self.assertIn("NIGHT_TAG=1792135000000000000", env.read_text())
            # 已有状态时必须改走恢复入口，不能再按首次启动覆写配置/环境。
            Path(c["account_state_file"]).write_bytes(b"existing ledger")
            env_before = env.read_bytes()
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                prepare_night.main(argv)
            self.assertEqual(out.read_bytes(), original[0])
            self.assertEqual(Path(c["account_state_file"]).read_bytes(), b"existing ledger")
            self.assertEqual(env.read_bytes(), env_before)

    def test_night_prepare_rejects_bad_observation_before_config_or_environment_write(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)/"night-cu-1792135000000000000"
            root.mkdir()
            observed, env = root/"observed.json", Path(directory)/"night.env"
            original = self.night_observation(observed)
            for mutate in (lambda d: d.update(status="failed"), lambda d: d.update(cross_products=["CU", "AU"]),
                    lambda d: d["quotes"]["au2612.SHFE"].update(ask="0"),
                    lambda d: d["strategy_contracts"]["calendar_spread"]["CU"].update(far="cu2701.SHFE")):
                from copy import deepcopy
                data = deepcopy(original)
                mutate(data)
                observed.write_text(json.dumps(data))
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    prepare_night.main(["--observations", str(observed), "--output-dir", str(root), "--env-file", str(env)])
                self.assertFalse((root/"session.json").exists())
                self.assertFalse((root/"generated.json").exists())
                self.assertFalse(env.exists())

    def test_night_prepare_never_rewrites_different_session_or_old_worker_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)/"night-cu-1792135000000000000"
            root.mkdir()
            observed, env, out = root/"observed.json", Path(directory)/"night.env", root/"session.json"
            self.night_observation(observed)
            argv = ["--observations", str(observed), "--output-dir", str(root), "--env-file", str(env)]
            for old_path, content in ((out, b'{"version": 9}'), (root/"main_ema.json", b"old worker state")):
                old_path.write_bytes(content)
                with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                    prepare_night.main(argv)
                self.assertEqual(old_path.read_bytes(), content)
                self.assertFalse(env.exists())
                old_path.unlink()

    def test_cffex_four_profile_preserves_traded_identities_in_separate_migration_files(self):
        from scripts.integration.shared_account.migrate_scope import validate_scope_configs
        with TemporaryDirectory() as directory:
            root = Path(directory)
            observed, original, expanded = (root/name for name in ("observed.json", "old.json", "four.json"))
            contracts = {p: {"main": p+"2612.CFFEX"} for p in ("IM", "IH", "IC", "IF")}
            contracts["IM"]["secondary"] = "IM2703.CFFEX"
            routes = {"main_ema": {"IM": {"main": "IM2610.CFFEX"}},
                "calendar_spread": {"IM": contracts["IM"]},
                "cross_section": {p: {"main": p+"2610.CFFEX"} for p in ("IM", "IF")}}
            instruments = ("IM2610.CFFEX", "IM2612.CFFEX", "IM2703.CFFEX", "IF2610.CFFEX", "IH2610.CFFEX", "IC2610.CFFEX")
            data = {"status": "observed", "orders_submitted": 0, "verification": "readonly_dates_and_contracts",
                "environment": "realtime", "profile": "im-if", "contracts": contracts, "strategy_contracts": routes,
                "session_arguments": {"trading_day": "20261016", "source_day": "20261015", "replay_md_trading_day": None},
                "instrument_specs": {i: {"tick": "0.2", "multiplier": 300 if i.startswith(("IH", "IF")) else 200} for i in instruments},
                "quotes": {i: {"bid": 5000 if i.startswith(("IH", "IF")) else 10000,
                    "ask": 5001 if i.startswith(("IH", "IF")) else 10001} for i in instruments}}
            common = ["--environment", "realtime", "--execution-mode", "independent", "--use-observed-budget", "--observations", str(observed)]
            observed.write_text(json.dumps(data))
            with redirect_stdout(StringIO()):
                prepare_session.main(["--output", str(original), "--profile", "im-if", *common])
            old = config.read_config(original)
            state = Path(old["account_state_file"])
            state.parent.mkdir(parents=True)
            state.write_bytes(b"untouched existing checkpoint")
            data["profile"] = "cffex-four"
            data["strategy_contracts"]["cross_section"] = {p: {"main": p+"2610.CFFEX"} for p in ("IM", "IH", "IC", "IF")}
            observed.write_text(json.dumps(data))
            with redirect_stdout(StringIO()):
                prepare_session.main(["--output", str(expanded), "--profile", "cffex-four", "--from-config", str(original), *common])
            new = config.read_config(expanded)
            validate_scope_configs(old, new)
            self.assertEqual(new["account"]["client_id"], old["account"]["client_id"])
            self.assertEqual([r["strategy_id"] for r in new["strategies"]], [r["strategy_id"] for r in old["strategies"]])
            self.assertEqual(new["account"]["max_gross_quantity"], "7")
            self.assertEqual(new["account"]["max_session_orders"], 200)
            self.assertNotEqual(new["account_state_file"], old["account_state_file"])
            cross = new["strategies"][1]["argv"]
            self.assertEqual(cross[cross.index("--products")+1], "IM,IH,IC,IF")
            self.assertEqual(cross[cross.index("--history-minutes")+1], "21")
            self.assertEqual(cross[cross.index("--group-fraction")+1], "0.5")
            self.assertGreater(Decimal(cross[cross.index("--target-notional")+1]), 4000000)
            self.assertEqual(state.read_bytes(), b"untouched existing checkpoint")
            for i in (0, 2):
                self.assertEqual(new["strategies"][i]["argv"], old["strategies"][i]["argv"])
            for p in ("IM", "IH", "IC", "IF"):
                self.assertIn(p+"="+p+"2610.CFFEX", cross)

    def test_cffex_profile_never_silently_ignores_explicit_cross_products(self):
        self.assertEqual(prepare_session.profile_cross_products("cffex-four", None), ("IM", "IH", "IC", "IF"))
        with self.assertRaisesRegex(ValueError, "静默忽略"):
            prepare_session.profile_cross_products("im-if", ("IM", "IH", "IC", "IF"))

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
