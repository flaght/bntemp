"""合并预检只读、不放大限额、不按净仓抵消策略归属；不加载原生SDK。"""
from copy import deepcopy
from types import SimpleNamespace as NS
import unittest

from scripts.integration.shared_account.join_option import inspect_join


class JoinInspectionTests(unittest.TestCase):
    def setUp(self):
        self.config = {"execution_mode": "independent", "existing_position_policy": "preserve_external",
            "account": {"client_id": "hybrid", "max_gross_quantity": 4,
                "max_gross_notional": 2000000, "max_session_orders": 200, "limit_offset_ticks": 1},
            "strategies": [{"strategy_id": sid, "kind": "hybrid_transformer", "argv": ["--product", p]}
                for sid, p in (("rb", "RB"), ("p", "P"))]}
        declarations = {row["strategy_id"]: deepcopy(row) for row in self.config["strategies"]}
        self.account = NS(generation=11400, payload={"component_states": {
            "session_identity": {"identity": {"account": deepcopy(self.config["account"]),
                "strategies": declarations, "trading_day": "20261110"}},
            "shared_account": {"failed": False, "pending": False,
                "sources": {"rb": {"revision": 6}}, "shared": {"order_budget": {"submitted": 10}}},
            "external_positions": {"gross": {"MO2611-C-6900.CFFEX": ["0", "1"],
                "MO2611-C-7100.CFFEX": ["0", "1"]}}},
            "positions": {"strategy_positions": [{"strategy_id": "rb", "target_key": "rb_main", "quantity": "2"}]}})
        self.signals = {sid: NS(generation=10, payload={"identity": row, "signal": {"batches": [1]}, "revision": 6})
                        for sid, row in declarations.items()}
        self.option = NS(generation=148, payload={"component_states": {
            "session_identity": {"identity": {"strategy_id": "option-vega-live", "td_day": "20261027", "mode": "simnow"}},
            "vega_signal": {}}, "positions": {"strategy_positions": [
                {"strategy_id": "option-vega-live", "target_key": key, "quantity": "-1"}
                for key in ("MO2611-C-6900.CFFEX", "MO2611-C-7100.CFFEX")]}})

    def inspect(self):
        return inspect_join(self.config, self.account, self.option, self.signals)

    def test_reported_rb_and_two_options_fill_unchanged_four_lot_budget(self):
        originals = deepcopy((self.config, self.account.payload, self.option.payload,
                              {sid: row.payload for sid, row in self.signals.items()}))
        report = self.inspect()
        self.assertEqual((report["owned_gross_quantity"], report["remaining_quantity"]), ("4", "0"))
        self.assertEqual(report["account_limits"], self.config["account"])
        self.assertFalse(report["can_start_orders"])
        self.assertFalse(report["broker_current_verified"])
        self.assertFalse(report["state_modified"])
        self.assertEqual(report["hybrid"]["signal_generations"], {"rb": 10, "p": 10})
        self.assertEqual(report["option_in_saved_external_baseline"]["MO2611-C-7100.CFFEX"], ["0", "1"])
        self.assertEqual(originals, (self.config, self.account.payload, self.option.payload,
                                     {sid: row.payload for sid, row in self.signals.items()}))

    def test_shared_option_checkpoint_is_identified_without_rewriting(self):
        self.option.payload["component_states"]["session_identity"]["identity"] = {
            "strategies": {"option-vega-live": {"kind": "option_vega"}}, "trading_day": "20261027"}
        report = self.inspect()
        self.assertEqual(report["option"]["format"], "shared_account")
        self.assertEqual(report["option"]["generation"], 148)

    def test_same_instrument_opposing_strategies_do_not_net_budget(self):
        self.account.payload["positions"]["strategy_positions"][0]["target_key"] = "MO2611-C-6900.CFFEX"
        self.assertEqual(self.inspect()["owned_gross_quantity"], "4")

    def test_over_budget_does_not_increase_limit(self):
        self.account.payload["positions"]["strategy_positions"][0]["quantity"] = "3"
        report = self.inspect()
        self.assertIn("OWNED_QUANTITY_EXCEEDS_UNCHANGED_ACCOUNT_LIMIT", report["blockers"])
        self.assertEqual(report["account_limits"]["max_gross_quantity"], 4)

    def test_strategy_only_plan_removes_aggregate_limits_without_changing_hybrid(self):
        originals = deepcopy((self.config, self.account.payload, self.option.payload))
        report = inspect_join(self.config, self.account, self.option, self.signals, strategy_limits_only=True)
        self.assertEqual(report["risk_scope"], "strategy")
        self.assertIsNone(report["account_limits"]["max_gross_quantity"])
        self.assertIsNone(report["account_limits"]["max_gross_notional"])
        self.assertIsNone(report["remaining_quantity"])
        self.assertEqual(report["account_limits"]["max_session_orders"], 200)
        self.assertEqual(report["source_account_limits"]["max_gross_quantity"], 4)
        self.assertFalse(report["can_start_orders"])
        self.assertEqual(originals, (self.config, self.account.payload, self.option.payload))

    def test_model_positions_are_not_treated_as_ctp_ownership(self):
        self.option.payload["component_states"]["session_identity"]["identity"]["mode"] = "model"
        with self.assertRaisesRegex(ValueError, "SimNow"):
            self.inspect()

    def test_zero_position_strategy_signal_is_still_required(self):
        del self.signals["p"]
        with self.assertRaisesRegex(ValueError, "零仓策略"):
            self.inspect()

    def test_missing_batch_state_is_rejected(self):
        del self.signals["rb"].payload["signal"]
        with self.assertRaisesRegex(ValueError, "批次状态"):
            self.inspect()

    def test_changed_account_limit_is_not_silently_accepted(self):
        self.config["account"]["max_gross_quantity"] = 10
        with self.assertRaisesRegex(ValueError, "身份、限额"):
            self.inspect()

    def test_unknown_strategy_ownership_is_never_dropped(self):
        self.option.payload["positions"]["strategy_positions"].append(
            {"strategy_id": "another", "target_key": "rb_main", "quantity": "1"})
        with self.assertRaisesRegex(ValueError, "未声明"):
            self.inspect()

    def test_working_orders_block_migration(self):
        self.account.payload["positions"]["working_quantities"] = [{"quantity": "1"}]
        self.assertIn("WORKING_ORDERS_PRESENT", self.inspect()["blockers"])

    def test_unknown_external_baseline_is_not_presented_as_broker_confirmation(self):
        self.account.payload["component_states"]["external_positions"]["gross"] = {}
        report = self.inspect()
        self.assertIsNone(report["option_in_saved_external_baseline"]["MO2611-C-7100.CFFEX"])
        self.assertFalse(report["broker_current_verified"])


if __name__ == "__main__":
    unittest.main()
