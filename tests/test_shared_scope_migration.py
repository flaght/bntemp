"""范围变更保持原成交和回报；真实组件、替身柜台，不联网。"""

from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.integration.shared_account import prepare_session, migrate_scope
from scripts.integration.shared_account.config import read_config
from tests import test_shared_account as fixtures


class ScopeMigrationTests(unittest.TestCase):
    setUp = fixtures.SharedAccountTests.setUp
    native_modules = fixtures.SharedAccountTests.native_modules
    native_account = fixtures.SharedAccountTests.native_account
    request = fixtures.SharedAccountTests.request
    submit = fixtures.SharedAccountTests.submit

    def test_traded_cffex_cross_migration_preserves_fills_journal_workers_quota_and_opens_new_leg(self):
        from bomber.framework.trader.execution.ipc import ExecutionEventJournal
        from bomber.framework.trader.execution.shared import SharedRiskBudget
        from importlib import import_module
        modules = self.native_modules()
        self.instrument = type(self.instrument).from_str("IM2610.CFFEX")
        all_ids = [self.instrument, *(type(self.instrument).from_str(p+"2610.CFFEX") for p in ("IF", "IH", "IC"))]
        with TemporaryDirectory() as directory:
            previous, updated = self.configs(directory)
            updated.pop("existing_position_policy", None)
            for config, products in ((previous, "IM,IF"), (updated, "IM,IH,IC,IF")):
                for row in config["strategies"]:
                    row["strategy_id"] = {"main_ema": "one", "cross_section": "two", "calendar_spread": "three"}[row["kind"]]
                    argv = row["argv"]
                    if row["kind"] == "cross_section":
                        argv[argv.index("--products")+1] = products
                        for p in products.split(","):
                            argv += ["--expected-instrument", p+"="+p+"2610.CFFEX"]
                    else:
                        argv[argv.index("--product")+1] = "IM"
            migrate_scope.validate_scope_configs(previous, updated)
            def build(config, instruments):
                account, service, broker, _, resources = self.native_account(Path(config["account_state_file"]), modules,
                    scoped=True, execution_mode="independent", extra_instruments=tuple(instruments[1:]), generic_close_venues=("CFFEX",))
                service._ports.pop("two")
                cross = service.strategy_client("two", routes={str(i): i for i in instruments}, budget=SharedRiskBudget(5, 50000))
                service.strategy_client("three", routes={"main": self.instrument}, budget=SharedRiskBudget(5, 50000))
                service.event_journal = ExecutionEventJournal(service._ports)
                self.configure_identity(account, service, config)
                for i in instruments:
                    service.prices.update(i, 3000, self.now)
                return account, service, broker, cross, resources
            old, service, broker, cross, _ = build(previous, all_ids[:2])
            old.start(resume=False); service.start(); cross.start(); old.authorize()
            targets = {all_ids[0]: Decimal(1), all_ids[1]: Decimal(-1)}
            request = replace(self.request("account", strategy="two", timestamp=self.now), targets=targets,
                metadata={"position_attribution": tuple(dict(strategy_id="two", target_key=str(i), instrument_id=str(i), quantity=str(q))
                    for i, q in targets.items())})
            cross.submit_targets(request)
            for sent in tuple(broker.sent): broker.fill(sent["OrderRef"])
            cross.drain_events()
            service.event_journal.acknowledge("two", len(service.event_journal.events["two"]))
            service.event_journal.requests["two:prior"] = {"digest": "prior", "status": "completed"}
            cross.stop(); old.stop()
            raw = old.manager.repository.load().payload
            original_files = {Path(previous["account_state_file"]): Path(previous["account_state_file"]).read_bytes()}
            for row in previous["strategies"]:
                worker_payload = {"identity": migrate_scope.worker_identity(row, previous), "revision": 1 if row["strategy_id"] == "two" else 0,
                    "cursor": len(service.event_journal.events[row["strategy_id"]]), "fills_received": 2 if row["strategy_id"] == "two" else 0}
                modules.persistence.JsonStateStore(row["state_file"]).save(worker_payload, expected_generation=0)
                original_files[Path(row["state_file"])] = Path(row["state_file"]).read_bytes()
            for name, config in (("old.json", previous), ("new.json", updated)):
                (Path(directory)/name).write_text(json.dumps(config))
            new, service2, broker2, _, resources = build(updated, all_ids)
            broker2.gross = dict(broker.gross)
            host = SimpleNamespace(account=new, service=service2, resources=resources,
                strategies={sid: SimpleNamespace() for sid in service2._ports})
            run = import_module("scripts.integration.shared_account.run")
            from bomber.framework.trader.runtime.live.local import strategy_state_lock
            def build_locked_host(config, **kwargs):
                # 保留真实build_host的文件锁边界；不能用无锁替身隐藏迁移重复加锁。
                resources.enter_context(strategy_state_lock(Path(config["account_state_file"])))
                return host
            start = new.start
            def start_with_locked_files(**kwargs):
                for path in (*original_files, Path(updated["account_state_file"]),
                             *(Path(r["state_file"]) for r in updated["strategies"])):
                    with self.assertRaisesRegex(RuntimeError, "已有进程使用"):
                        with strategy_state_lock(path):
                            self.fail("迁移权威核验期间状态锁已释放")
                return start(**kwargs)
            with patch.object(run, "build_host", side_effect=build_locked_host) as builder, \
                    patch.object(new, "start", side_effect=start_with_locked_files), \
                    patch.dict("sys.modules", {"dotenv": SimpleNamespace(load_dotenv=lambda *_: None)}), redirect_stdout(StringIO()):
                migrate_scope.main(["--from-config", str(Path(directory)/"old.json"), "--to-config", str(Path(directory)/"new.json"), "--connect"])
            self.assertTrue(builder.call_args.kwargs["read_only"])
            migrated = modules.persistence.JsonStateStore(updated["account_state_file"]).load().payload
            self.assertEqual(migrated["positions"]["strategy_positions"], raw["positions"]["strategy_positions"])
            self.assertEqual(migrated["component_states"]["shared_account"]["shared"]["event_journal"],
                raw["component_states"]["shared_account"]["shared"]["event_journal"])
            self.assertEqual(migrated["ctp_drivers"], raw["ctp_drivers"])
            self.assertEqual(new.driver.order_budget["submitted"], 2)
            self.assertEqual(new.driver.submitted_orders, 0)
            self.assertFalse(broker2.sent)
            for p, content in original_files.items(): self.assertEqual(p.read_bytes(), content)
            for path in (*original_files, Path(updated["account_state_file"]),
                         *(Path(r["state_file"]) for r in updated["strategies"])):
                with strategy_state_lock(path):
                    pass  # 迁移结束后所有锁均释放。
            for row in updated["strategies"]:
                saved = modules.persistence.JsonStateStore(row["state_file"]).load().payload
                if row["kind"] == "cross_section":
                    self.assertEqual((saved["revision"], saved["fills_received"], saved["cursor"]),
                        (1, 2, len(service.event_journal.events["two"])))
            # 用迁移后的真实共享/CTP组件恢复，原两腿保留，仅新增IH目标发新单。
            resumed, service3, broker3, cross3, _ = build(updated, all_ids)
            broker3.gross = dict(broker.gross)
            resumed.start(resume=True); service3.start(); cross3.start(); resumed.authorize()
            target4 = {**targets, all_ids[2]: Decimal(1), all_ids[3]: Decimal(0)}
            cross3.submit_targets(replace(request, revision=2, targets=target4, metadata={"position_attribution": tuple(
                dict(strategy_id="two", target_key=str(i), instrument_id=str(i), quantity=str(q)) for i, q in target4.items())}))
            self.assertEqual(len(broker3.sent), 1)
            self.assertEqual(broker3.sent[0]["InstrumentID"], "IH2610")
            self.assertGreater(int(broker3.sent[0]["OrderRef"]), raw["ctp_drivers"]["account"]["next_ref"])
            broker3.fill(broker3.sent[0]["OrderRef"])
            self.assertEqual(service3.position_manager.position("two", "IM2610.CFFEX"), 1)
            self.assertEqual(service3.position_manager.position("two", "IF2610.CFFEX"), -1)
            self.assertEqual(service3.position_manager.position("two", "IH2610.CFFEX"), 1)
            cross3.stop(); resumed.stop()
            # 未决状态、外部新增合约仓位及原路由变化不能被迁移隐藏。
            identity = self.configure_identity(new, service2, updated)
            declaration = service2.snapshot_state()
            for field in ("pending", "failed"):
                bad = deepcopy(raw); bad["component_states"]["shared_account"][field] = True
                with self.assertRaises(ValueError): migrate_scope.transform_scope_payload(bad, previous, updated, identity, declaration)
            bad = deepcopy(raw)
            bad["component_states"]["external_positions"]["gross"]["IH2610.CFFEX"] = ["1", "0"]
            with self.assertRaisesRegex(ValueError, "外部持仓"):
                migrate_scope.transform_scope_payload(bad, previous, updated, identity, declaration)
            bad = deepcopy(raw)
            bad["component_states"]["shared_account"]["shared"]["event_journal"]["requests"]["two:prior"]["status"] = "pending"
            with self.assertRaisesRegex(ValueError, "IPC请求完成"):
                migrate_scope.transform_scope_payload(bad, previous, updated, identity, declaration)
            changed_identity = deepcopy(identity)
            changed_identity["strategies"]["two"]["routes"]["routes"]["IM2610.CFFEX"] = "IM2612.CFFEX"
            with self.assertRaisesRegex(ValueError, "原路由"):
                migrate_scope.transform_scope_payload(raw, previous, updated, changed_identity, declaration)
            changed_config = deepcopy(updated)
            row = next(r for r in changed_config["strategies"] if r["kind"] == "cross_section")
            row["strategy_id"] = "new-cross"
            with self.assertRaisesRegex(ValueError, "策略ID"):
                migrate_scope.validate_scope_configs(previous, changed_config)

    def migration_files(self, directory):
        """仅准备进入宿主前所需的文件，用于真实flock及目标文件竞争回归。"""
        from bomber.framework.trader.persistence import JsonStateStore
        previous, updated = self.configs(directory)
        for name, config in (("old.json", previous), ("new.json", updated)):
            (Path(directory)/name).write_text(json.dumps(config))
        JsonStateStore(previous["account_state_file"]).save(
            {"component_states": {"shared_account": {"sources": {}}}}, expected_generation=0)
        return previous, updated, ["--from-config", str(Path(directory)/"old.json"),
                                  "--to-config", str(Path(directory)/"new.json"), "--connect"]

    def test_migration_refuses_occupied_original_and_destination_files_without_writes(self):
        from importlib import import_module
        self.native_modules()
        from bomber.framework.trader.runtime.live.local import strategy_state_lock
        run = import_module("scripts.integration.shared_account.run")
        with TemporaryDirectory() as directory:
            previous, updated, argv = self.migration_files(directory)
            original = Path(previous["account_state_file"]).read_bytes()
            destinations = [Path(updated["account_state_file"]), *(Path(r["state_file"]) for r in updated["strategies"])]
            paths = [Path(previous["account_state_file"]), *(Path(r["state_file"]) for r in previous["strategies"]), *destinations]
            for path in paths:
                with self.subTest(path=path), ExitStack() as resources:
                    def build_locked_host(config, **kwargs):
                        resources.enter_context(strategy_state_lock(Path(config["account_state_file"])))
                        self.fail("已有文件被占用时不得进入迁移核验")
                    with strategy_state_lock(path), \
                            patch.object(run, "build_host", side_effect=build_locked_host), \
                            patch.dict("sys.modules", {"dotenv": SimpleNamespace(load_dotenv=lambda *_: None)}), \
                            redirect_stdout(StringIO()), self.assertRaisesRegex(RuntimeError, "已有进程使用"):
                        migrate_scope.main(argv)
                self.assertEqual(Path(previous["account_state_file"]).read_bytes(), original)
                self.assertFalse(any(p.exists() for p in destinations))
                with strategy_state_lock(path):
                    pass

    def test_migration_rechecks_destination_after_host_acquires_account_lock(self):
        from importlib import import_module
        self.native_modules()
        from bomber.framework.trader.runtime.live.local import strategy_state_lock
        run = import_module("scripts.integration.shared_account.run")
        with TemporaryDirectory() as directory, ExitStack() as resources:
            previous, updated, argv = self.migration_files(directory)
            original = Path(previous["account_state_file"]).read_bytes()
            destination = Path(updated["account_state_file"])
            account = SimpleNamespace(driver=SimpleNamespace(submitted_orders=0),
                abort_preparation=Mock(), stop=Mock(), start=Mock())
            def competing_host(config, **kwargs):
                # 模拟初次存在性检查后，另一次运行先生成账本并释放锁。
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"existing checkpoint from another run")
                resources.enter_context(strategy_state_lock(destination))
                return SimpleNamespace(account=account, resources=resources)
            with patch.object(run, "build_host", side_effect=competing_host), \
                    patch.dict("sys.modules", {"dotenv": SimpleNamespace(load_dotenv=lambda *_: None)}), \
                    redirect_stdout(StringIO()), self.assertRaisesRegex(ValueError, "新检查点已存在"):
                migrate_scope.main(argv)
            self.assertEqual(destination.read_bytes(), b"existing checkpoint from another run")
            self.assertEqual(Path(previous["account_state_file"]).read_bytes(), original)
            account.start.assert_not_called()
            account.abort_preparation.assert_called_once()
            self.assertFalse(any(Path(r["state_file"]).exists() for r in updated["strategies"]))
            with strategy_state_lock(destination):
                pass

    def configs(self, directory):
        old, new = Path(directory)/"old.json", Path(directory)/"new.json"
        args = ["--environment", "realtime", "--execution-mode", "independent",
            "--trading-day", "20261013", "--source-day", "20261012"]
        with redirect_stdout(StringIO()):
            prepare_session.main(["--output", str(old), *args])
            prepare_session.main(["--output", str(new), *args, "--cross-products", "CU,AL,AG,AU",
                "--existing-position-policy", "preserve_external", "--ctp-future-buffer-ms", "100"])
        previous, updated = read_config(old), read_config(new)
        for config in (previous, updated):
            config["account"].update(client_id="account", max_gross_quantity="10", max_gross_notional="100000")
            for row in config["strategies"]:
                row["strategy_id"] = {"main_ema": "one", "calendar_spread": "three", "cross_section": "two"}[row["kind"]]
        next(row for row in updated["strategies"] if row["kind"] == "cross_section")["strategy_id"] = "four"
        return previous, updated

    def configure_identity(self, account, service, config):
        identity = {"trading_day": config["trading_day"], "environment": config["environment"],
            "account": config["account"], "managed_instruments": sorted(account.position_scope.instruments),
            "strategies": {row["strategy_id"]: {"argv": row["argv"], "kind": row["kind"],
                "routes": service._ports[row["strategy_id"]].configuration(), "reference": "fixed"}
                for row in config["strategies"]}}
        if config.get("existing_position_policy"):
            identity["existing_position_policy"] = config["existing_position_policy"]
        account.manager.state_components["session_identity"].identity = identity
        return identity

    def test_scope_migration_preserves_original_ema_fill_external_short_and_order_ref(self):
        from bomber.framework.trader.execution.ipc import ExecutionEventJournal
        from bomber.framework.trader.execution.shared import SharedRiskBudget
        modules = self.native_modules()
        with TemporaryDirectory() as directory:
            previous, updated = self.configs(directory)
            original = Path(previous["account_state_file"])
            old, service, transport, (one, _), _ = self.native_account(original, modules,
                scoped=True, execution_mode="independent")
            service.strategy_client("three", routes={"main": self.instrument}, budget=SharedRiskBudget(5, 50000))
            service.event_journal = ExecutionEventJournal(service._ports)
            self.configure_identity(old, service, previous)
            transport.gross = {"ag2612.SHFE": (0, 1)}
            old.start(resume=False); service.start(); one.start(); old.authorize()
            self.submit(one, 1); transport.fill(transport.sent[-1]["OrderRef"])
            one.stop(); old.stop()
            original_bytes = original.read_bytes()
            raw = old.manager.repository.load().payload
            ref = raw["ctp_drivers"]["account"]["next_ref"]
            extras = tuple(type(self.instrument).from_str(i) for i in ("al2611.SHFE", "ag2612.SHFE", "au2612.SHFE"))
            new, service2, broker2, _, _ = self.native_account(Path(updated["account_state_file"]), modules,
                scoped=True, execution_mode="independent", preserve_external=True, extra_instruments=extras)
            service2._ports.pop("two")
            service2.strategy_client("three", routes={"main": self.instrument}, budget=SharedRiskBudget(5, 50000))
            four = service2.strategy_client("four", routes={str(i): i for i in (self.instrument, *extras)},
                budget=SharedRiskBudget(5, 50000))
            service2.event_journal = ExecutionEventJournal(service2._ports)
            identity = self.configure_identity(new, service2, updated)
            declaration = service2.snapshot_state()
            transformed = migrate_scope.transform_scope_payload(raw, previous, updated, identity, declaration)
            self.assertEqual(raw["component_states"]["external_positions"]["version"], 1)
            self.assertEqual(transformed["component_states"]["external_positions"]["version"], 2)
            new.manager.repository = old.manager.repository
            broker2.gross = dict(transport.gross)
            new.start(resume=True, payload_transform=lambda _: transformed, commit_restored=False)
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertEqual(new.driver.checkpoint().next_ref, ref)
            self.assertEqual(service2.position_manager.position("one", "main"), 1)
            self.assertEqual(service2.position_manager.account_position("account", extras[1]), 0)
            self.assertEqual(service2.event_journal.events["one"], service.event_journal.events["one"])
            self.assertFalse(service2.event_journal.events["four"])
            destination = Path(updated["account_state_file"])
            migrate_scope.write_exclusive(destination, original_bytes)
            new.manager.repository = modules.persistence.JsonStateStore(destination)
            new.manager.commit_restore()
            service2.start(); four.start(); new.authorize()
            for i in (self.instrument, *extras): service2.prices.update(i, 3000, self.now)
            quantities = {i: Decimal(i == extras[1]) for i in (self.instrument, *extras)}
            base = self.request("account", strategy="four", timestamp=self.now)
            four.submit_targets(replace(base, targets=quantities, metadata={"position_attribution": tuple(
                {"strategy_id": "four", "target_key": str(i), "instrument_id": str(i), "quantity": str(q)}
                for i, q in quantities.items())}))
            self.assertEqual(broker2.sent[-1]["CombOffsetFlag"], "0")
            self.assertGreater(int(broker2.sent[-1]["OrderRef"]), ref)
            broker2.fill(broker2.sent[-1]["OrderRef"])
            self.assertEqual(broker2.gross["ag2612.SHFE"], (1, 1))
            self.assertEqual(service2.position_manager.position("four", "ag2612.SHFE"), 1)
            self.assertEqual(service2.position_manager.position("one", "main"), 1)
            new.stop()
            self.assertEqual(original.read_bytes(), original_bytes)
            corrupt = deepcopy(raw)
            corrupt["component_states"]["shared_account"]["shared"]["event_journal"]["events"]["two"] = [{"sequence": 1}]
            with self.assertRaisesRegex(ValueError, "执行历史"):
                migrate_scope.transform_scope_payload(corrupt, previous, updated, identity, declaration)

    def test_scope_config_change_cannot_reset_identity_or_change_algorithms(self):
        with TemporaryDirectory() as directory:
            old, new = self.configs(directory)
            migrate_scope.validate_scope_configs(old, new)
            for mutate in (lambda c: c["account"].update(client_id="other"),
                    lambda c: c.update(account_state_file=old["account_state_file"]),
                    lambda c: c["strategies"][0]["argv"].extend(["--fast", "9"]),
                    lambda c: c.update(trading_day="20261014")):
                changed = deepcopy(new); mutate(changed)
                with self.assertRaises(ValueError): migrate_scope.validate_scope_configs(old, changed)


if __name__ == "__main__":
    unittest.main()
