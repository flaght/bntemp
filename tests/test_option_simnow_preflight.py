"""08只读迁移预检：旧状态/账户范围/今昨仓约束，不连接柜台。"""
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from scripts.integration.options import simnow_preflight as probe


def state():
    return NS(generation=3, saved_at_ns=1, payload={
        "component_states":{"session_identity":{"identity":{
            "strategy_id":probe.CLIENT,"mode":"simnow","valuation_day":"2026-10-13",
            "td_day":"20261013","environment":"realtime","config":{"option_slot":"13:36"},
            "sources":{"market":{"channel":"ctp"}},"labels":{"market-bars":"end"},
            "policy":{"account_reconciliation":"strict","position_source":"fills"}}},
            "vega_signal":{"option_goals":{"MO2611-C-7400":"-1"}}},
        "ctp_ledgers":{probe.CLIENT:{"trading_day":"20261013","positions":[{
            "instrument_id":"MO2611-C-7400.CFFEX","long_today":"0","long_yesterday":"0",
            "short_today":"1","short_yesterday":"0"}]}},
        "ctp_drivers":{probe.CLIENT:{"account_id":"fixture","broker_id":"9999","investor_id":"fixture","orders":[]}},
        "orders":{probe.CLIENT:[]},"positions":{
            "working_quantities":[],"strategy_positions":[{"strategy_id":probe.CLIENT,
                "target_key":"MO2611-C-7400.CFFEX","quantity":"-1"}],
            "account_positions":[{"key":{"client_id":probe.CLIENT,"instrument_id":"MO2611-C-7400.CFFEX"},"quantity":"-1"}]}})


class Driver:
    def __init__(self):
        self.starts=self.stops=self.submits=self.cancels=0
        self.trading_day="20261014"
        self.gross={"MO2611-C-7400.CFFEX":(Decimal(0),Decimal(1)),
            "ag2612.SHFE":(Decimal(0),Decimal(1))}
        self.details=[{"InstrumentID":"MO2611-C-7400","ExchangeID":"CFFEX",
            "Position":1,"PosiDirection":"3","PositionDate":"2","HedgeFlag":"1","PositionCost":8200}]
        self.transport=NS(account_id="fixture",broker_id="9999",investor_id="fixture",
            query_gross_positions=lambda:self.gross,query_position_details=lambda:self.details)
        self.active=0
    def start(self,handler):self.starts+=1
    def stop(self):self.stops+=1
    def reconcile_account_state(self):return NS(balances={"CNY":NS(available=Decimal(100000))})
    def reconcile_active_orders(self):return NS(orders=tuple(range(self.active)))
    def submit_order(self,*args):self.submits+=1;raise AssertionError("readonly probe submitted order")
    def cancel_order(self,*args):self.cancels+=1;raise AssertionError("readonly probe canceled order")


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.saved,self.driver=state(),Driver()
        self.report={"checkpoint":probe.checkpoint_summary(self.saved),"cleanup_errors":[]}

    def run_inspection(self):
        return probe.inspect_counterparty(self.driver,"20261014",self.saved,self.report)

    def test_same_owned_gross_next_day_uses_authoritative_yesterday_bucket(self):
        self.run_inspection()
        self.assertEqual(self.report["managed_gross"],{"MO2611-C-7400.CFFEX":(0,1)})
        self.assertEqual(self.report["authoritative_buckets"]["MO2611-C-7400.CFFEX"]["quantities"],(0,0,0,1))
        self.assertEqual(self.report["checkpoint"]["saved_buckets"]["MO2611-C-7400.CFFEX"],(0,0,1,0))
        self.assertTrue(self.report["gross_matches_original_state"])
        self.assertIn("ag2612.SHFE",self.report["external_gross"])
        self.assertEqual((self.driver.submits,self.driver.cancels,self.driver.stops),(0,0,1))

    def test_dual_direction_gross_cannot_pass_on_equal_net(self):
        self.driver.gross["MO2611-C-7400.CFFEX"]=(1,2)
        self.driver.details.append({**self.driver.details[0],"Position":1,"PosiDirection":"2"})
        self.driver.details[0]["Position"]=2
        with self.assertRaisesRegex(ValueError,"总仓与原状态不一致"):self.run_inspection()
        self.assertEqual(self.driver.stops,1)

    def test_current_active_orders_are_reported_without_cancel(self):
        self.driver.active=1
        with self.assertRaisesRegex(ValueError,"活动订单"):self.run_inspection()
        self.assertEqual(self.report["active_orders"],1)
        self.assertEqual(self.driver.cancels,0)

    def test_wrong_account_or_non_simnow_fails_before_connection(self):
        for name,value in (("account_id","other"),("broker_id","1234")):
            with self.subTest(name=name):
                self.driver=Driver();setattr(self.driver.transport,name,value)
                with self.assertRaisesRegex(ValueError,"同一SimNow账户"):self.run_inspection()
                self.assertEqual(self.driver.starts,0)

    def test_wrong_td_day_and_inconsistent_bucket_fail_and_close(self):
        self.driver.trading_day="20261015"
        with self.assertRaisesRegex(ValueError,"TD交易日"):self.run_inspection()
        self.driver=Driver();self.driver.details=[]
        with self.assertRaisesRegex(ValueError,"今昨仓明细与总仓"):self.run_inspection()
        self.assertEqual(self.driver.stops,1)

    def test_old_working_order_and_unowned_position_reject(self):
        self.saved.payload["positions"]["working_quantities"]=[{"quantity":"1"}]
        with self.assertRaisesRegex(ValueError,"在途"):probe.checkpoint_summary(self.saved)
        self.saved=state();self.saved.payload["positions"]["strategy_positions"]=[]
        with self.assertRaisesRegex(ValueError,"归属"):probe.checkpoint_summary(self.saved)

    def test_old_zero_net_dual_gross_cannot_be_treated_as_flat(self):
        self.saved.payload["ctp_ledgers"][probe.CLIENT]["positions"][0]["long_today"]="1"
        self.saved.payload["positions"]["strategy_positions"]=[]
        self.saved.payload["positions"]["account_positions"]=[]
        with self.assertRaisesRegex(ValueError,"双向总仓"):probe.checkpoint_summary(self.saved)

    def test_main_preserves_original_bytes_reports_migration_and_keeps_orders_disabled(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import json
        with TemporaryDirectory() as directory:
            path=Path(directory)/"original.json";path.write_bytes(b"fixture bytes")
            before=path.read_bytes()
            with patch("dotenv.load_dotenv"), \
                    patch("bomber.framework.trader.persistence.JsonStateStore.load",return_value=self.saved), \
                    patch("scripts.integration.ctp.td_readonly.build_readonly_driver",return_value=self.driver), \
                    redirect_stdout(StringIO()):
                code=probe.main(["--connect","--expected-trading-day","20261014","--state-file",str(path),
                    "--report-dir",str(Path(directory)/"reports")])
            result=json.loads(next((Path(directory)/"reports").glob("*/summary.json")).read_text())
            self.assertEqual(code,0)
            self.assertEqual(path.read_bytes(),before)
            self.assertTrue(result["migration_required"])
            self.assertFalse(result["can_start_orders"])
            self.assertFalse(result["orders_enabled"])
            self.assertFalse(result["state_modified"])
            self.assertEqual(result["status"],"completed")

    def test_corrupt_original_file_fails_before_connection_and_is_preserved(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import json
        with TemporaryDirectory() as directory:
            path=Path(directory)/"original.json";path.write_text('{"checksum":"wrong"}')
            before=path.read_bytes()
            with patch("dotenv.load_dotenv"), \
                    patch("scripts.integration.ctp.td_readonly.build_readonly_driver") as build, \
                    redirect_stdout(StringIO()):
                code=probe.main(["--connect","--expected-trading-day","20261014","--state-file",str(path),
                    "--report-dir",str(Path(directory)/"reports")])
            self.assertEqual(code,1)
            build.assert_not_called()
            self.assertEqual(path.read_bytes(),before)
            result=json.loads(next((Path(directory)/"reports").glob("*/summary.json")).read_text())
            self.assertIn("校验和",result["failure"])
            self.assertFalse(result["state_modified"])


if __name__=="__main__":unittest.main()
