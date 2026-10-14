"""范围变更保持原成交和回报；真实组件、替身柜台，不联网。"""

from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.integration.shared_account import prepare_session, migrate_scope
from scripts.integration.shared_account.config import read_config
from tests import test_shared_account as fixtures


class ScopeMigrationTests(unittest.TestCase):
    setUp = fixtures.SharedAccountTests.setUp
    native_modules = fixtures.SharedAccountTests.native_modules
    native_account = fixtures.SharedAccountTests.native_account
    request = fixtures.SharedAccountTests.request
    submit = fixtures.SharedAccountTests.submit

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
