"""原生Driver/公共恢复的只读迁移回归；SDK实体与柜台/参考传输为夹具。"""
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib import import_module
from io import StringIO
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bomber.framework.market.basic.base import InstrumentId,InstrumentMeta
from bomber.framework.trader.contracts import TargetPortfolio
from bomber.framework.trader.portfolio import AccountTargetKey,PortfolioCoordinatorState
from bomber.framework.trader.execution.ctp.native_driver import CtpNativeTraderDriver,CtpTraderSession
from bomber.framework.trader.execution.events import AccountStateEvent,ActiveOrderSnapshot,CurrencyBalance
from bomber.framework.trader.persistence import JsonStateStore
from scripts.integration.options import simnow_migrate as migrate
from tests.test_option_vega_assembly import ReferenceService,Feed

runtime=import_module("demos.08_option_vega.live_runtime")
entry=import_module("demos.08_option_vega.run_live")
CLIENT=migrate.CLIENT
OPTION="MO2609-C-6600.CFFEX"


class Transport:
    is_test_transport=True
    account_id=investor_id="fixture"
    broker_id="9999"
    client_id=CLIENT
    production_mode=True
    front="tcp://182.254.243.31:30001"
    def __init__(self,day):
        self.day=day;self.revision=0;self.sent=[];self.canceled=[];self.bucket="1"
    def connect(self,*handlers):return CtpTraderSession("9999","fixture",self.day,7)
    def activate(self):pass
    def close(self):pass
    def query_positions(self):return {OPTION:Decimal(-1),"ag2612.SHFE":Decimal(-1)}
    def query_gross_positions(self):return {OPTION:(Decimal(0),Decimal(1)),"ag2612.SHFE":(Decimal(0),Decimal(1))}
    def query_position_details(self):return [dict(InstrumentID="MO2609-C-6600",ExchangeID="CFFEX",Position=1,
        PosiDirection="3",PositionDate=self.bucket,HedgeFlag="1",PositionCost=8220)]
    def query_account(self):
        self.revision+=1
        return AccountStateEvent(CLIENT,"fixture",self.revision,time.time_ns(),{"CNY":CurrencyBalance("CNY",total=100000,available=80000)})
    def query_active_orders(self):
        self.revision+=1
        return ActiveOrderSnapshot(CLIENT,"fixture",self.revision,time.time_ns(),())
    def send_order(self,fields):self.sent.append(fields);raise AssertionError("migration sent order")
    def cancel_order(self,ref):self.canceled.append(ref);raise AssertionError("migration canceled order")


def inputs(day,channel):
    service=ReferenceService();service.valuation_day=day
    service.manifest=lambda:{"ready":True,"fingerprint":service.fingerprint}
    calendar=tuple(day+timedelta(days=n) for n in range(65) if (day+timedelta(days=n)).weekday()<5)
    refs=runtime.OptionReferences(service,day.strftime("%Y%m%d"),calendar,
        calendar_manifest={"calendar_id":"china.cffex","fingerprint":"fixture"})
    market,index=Feed("fixture-market"),Feed("fixture-index")
    for key,instrument in refs.instrument_ids.items():
        market.register_instrument(InstrumentMeta(instrument,price_precision=1,
            multiplier=Decimal(100 if key.startswith("MO") else 200)))
    index_id=InstrumentId.from_str("000852.XSHG")
    index.register_instrument(InstrumentMeta(index_id,price_precision=4,size_precision=4))
    index_identity={"channel":"dolphindb-dfs","database":"dfs://min_bar","table":"index_1min","availability":"observed",
        "poll_seconds":.2,"query_timeout":2,"price_precision":4,"size_precision":4,"timestamp_label":"end"}
    return NS(references=refs,feeds={"market-bars":market,"index-bars":index},
        labels={"market-bars":"end" if channel=="dolphindb" else "inclusive-end","index-bars":"end"},index_id=index_id,
        sources={**{key:"market-bars" for key in refs.instrument_ids},"000852":"index-bars"},
        ctp_upstream=None,ctp_bar_feed=None,dolphin_feeds=(),feature_query_feeds=(),
        source_identity={"index":index_identity,"market":{"channel":channel}})


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/"original.json";self.output=self.root/"new.json"
        self.old_day=date(2026,8,13);self.new_day=date(2026,8,14)
        self.old_transport=Transport("20260813")
        old_driver=CtpNativeTraderDriver(CLIENT,"fixture",self.old_transport)
        old_args=entry.parse_args(["--connect","--mode","simnow","--enable-orders","--confirm-simnow",
            "--expected-trading-day","20260813","--state-file",str(self.source),"--option-slot","13:36",
            "--delta-targets","0.25","--frame-wait-seconds","20"])
        old=runtime.assemble(old_args,inputs(self.old_day,"ctp"),old_driver)
        instrument=InstrumentId.from_str(OPTION)
        old.runner.position_manager.set_account_position(CLIENT,instrument,-1)
        old.runner.position_manager.attribution.adopt(CLIENT,instrument,CLIENT,OPTION,-1)
        old.ledger.apply_fill(instrument,"SELL","OPEN",1,Decimal("82.2"),100)
        values={str(i):Decimal(-1) if str(i)==OPTION else Decimal(0) for i in old.references.instrument_ids.values()}
        old.runner.target_store.apply(TargetPortfolio(CLIENT,1,1,values))
        contribution={AccountTargetKey(CLIENT,InstrumentId.from_str(key)):value for key,value in values.items()}
        old.runner.portfolio_coordinator.restore(PortfolioCoordinatorState(1,{CLIENT:contribution},{CLIENT:1},frozenset(contribution)))
        old.strategy.option_goals["MO2609-C-6600"]=Decimal(-1);old.strategy.sent_days={self.old_day}
        old.strategy.submissions=old.strategy.entry_decisions=1
        old_driver.start(lambda r:None);old.manager.save();old_driver.stop()
        self.saved=JsonStateStore(self.source).load();self.before=self.source.read_bytes()
        self.new_transport=Transport("20260814")
        self.current=inputs(self.new_day,"dolphindb")
        argv=migrate.runtime_argv(self.identity,"20260814",self.output,"stream_cffex_1min","end","cumulative",self.root/"live")
        self.new_args=entry.parse_args(argv)
        self.new_session=runtime.assemble(self.new_args,self.current,CtpNativeTraderDriver(CLIENT,"fixture",self.new_transport))
        self.new_identity=self.new_session.manager.state_components["session_identity"].snapshot_state()["identity"]
        self.observation={"checkpoint":migrate.validate_original(self.saved),"observed_td_trading_day":"20260814",
            "active_orders":0,"gross_matches_original_state":True,"managed_gross":{OPTION:(Decimal(0),Decimal(1))},
            "authoritative_buckets":{OPTION:{"quantities":[0,0,1,0],"position_costs":[0,0,8220,0]}}}

    @property
    def identity(self):return self.saved.payload["component_states"]["session_identity"]["identity"]

    def transform(self,identity=None,observation=None,saved=None,multipliers=None):
        return migrate.transform_payload(saved or self.saved,identity or self.new_identity,observation or self.observation,
            multipliers or {OPTION:100,"IM2609.CFFEX":200})

    def test_retains_ownership_order_history_and_uses_observed_today_bucket(self):
        payload,changes=self.transform()
        self.assertEqual(payload["positions"]["attribution"],self.saved.payload["positions"]["attribution"])
        self.assertEqual(payload["orders"],self.saved.payload["orders"])
        self.assertEqual(payload["ctp_drivers"],self.saved.payload["ctp_drivers"])
        ledger=payload["ctp_ledgers"][CLIENT]
        self.assertEqual(ledger["trading_day"],"20260814")
        self.assertEqual(ledger["positions"][0]["short_today"],"1")
        self.assertEqual(Decimal(ledger["positions"][0]["short_today_basis"]),Decimal("82.2"))
        self.assertEqual(payload["component_states"]["vega_signal"]["sent_days"],["2026-08-14"])
        self.assertEqual(self.source.read_bytes(),self.before)

    def test_observed_yesterday_bucket_is_preserved_instead_of_date_guessing(self):
        self.observation["authoritative_buckets"][OPTION]={"quantities":[0,0,0,1],"position_costs":[0,0,0,8220]}
        payload,_=self.transform()
        self.assertEqual(payload["ctp_ledgers"][CLIENT]["positions"][0]["short_yesterday"],"1")

    def test_strategy_parameter_or_index_identity_change_is_rejected(self):
        for branch in ("config","index"):
            identity=deepcopy(self.new_identity)
            if branch=="config":identity["config"]["max_option_lots"]=9
            else:identity["sources"]["index"]["table"]="other"
            with self.assertRaisesRegex(ValueError,"策略/风险|指数来源"):self.transform(identity=identity)

    def test_missing_owned_leg_or_changed_held_terms_is_rejected(self):
        with self.assertRaisesRegex(ValueError,"非零持仓腿"):self.transform(multipliers={"IM2609.CFFEX":200})
        identity=deepcopy(self.new_identity);identity["contracts"]["MO2609-C-6600"]["strike"]="9999"
        with self.assertRaisesRegex(ValueError,"条款"):self.transform(identity=identity)

    def test_nonzero_removed_target_and_wrong_current_buckets_reject(self):
        saved=NS(payload=deepcopy(self.saved.payload))
        saved.payload["targets"][0]["targets"]["MO2609-C-9999.CFFEX"]="-1"
        with self.assertRaisesRegex(ValueError,"非零旧目标"):self.transform(saved=saved)
        self.observation["authoritative_buckets"][OPTION]["quantities"]=[0,0,2,0]
        with self.assertRaisesRegex(ValueError,"分桶与总仓"):self.transform()

    def test_added_zero_routes_and_removed_zero_strategy_keys_are_explicit(self):
        saved=NS(payload=deepcopy(self.saved.payload))
        retired="MO2609-C-9999.CFFEX"
        saved.payload["targets"][0]["targets"][retired]="0"
        saved.payload["positions"]["strategy_positions"].append({"strategy_id":CLIENT,"target_key":retired,"quantity":"0"})
        added="MO2609-C-6800.CFFEX"
        payload,changes=self.transform(saved=saved,multipliers={OPTION:100,"IM2609.CFFEX":200,added:100})
        self.assertEqual(changes["added_zero_routes"],[added]);self.assertEqual(changes["removed_zero_routes"],[retired])
        self.assertEqual(payload["targets"][0]["targets"][added],"0")
        self.assertNotIn(retired,[row["target_key"] for row in payload["positions"]["strategy_positions"]])

    def run_main(self,digest=None):
        with patch("dotenv.load_dotenv"),patch("bomber.framework.trader.runtime.live.channels.ctp.build_simnow_transport",return_value=self.new_transport), \
                patch.object(entry,"prepare_inputs",side_effect=lambda args,context,lifecycle:self.current),redirect_stdout(StringIO()):
            code=migrate.main(["--connect","--source-state",str(self.source),"--source-sha256",digest or hashlib.sha256(self.before).hexdigest(),
                "--expected-trading-day","20260814","--output-state",str(self.output),"--report-dir",str(self.root/"reports")])
        report=json.loads(next((self.root/"reports").glob("*/summary.json")).read_text())
        return code,report

    def test_complete_preparation_uses_native_readonly_recovery_no_orders_and_no_feed_start(self):
        code,report=self.run_main()
        self.assertEqual((code,report["status"]),(0,"prepared"),report.get("failure"))
        self.assertEqual(self.source.read_bytes(),self.before)
        self.assertEqual((self.new_transport.sent,self.new_transport.canceled),([],[]))
        self.assertTrue(all(not feed.is_connected for feed in self.current.feeds.values()))
        self.assertFalse(report["orders_enabled"]);self.assertFalse(report["strategy_started"])
        saved=JsonStateStore(self.output).load()
        self.assertGreaterEqual(saved.generation,2)
        self.assertEqual(saved.payload["ctp_drivers"][CLIENT]["trading_day"],"20260814")
        self.assertEqual(saved.payload["component_states"]["vega_signal"]["sent_days"],["2026-08-14"])
        args=entry.parse_args(report["resume_argv"])
        self.assertTrue(args.resume);self.assertEqual(args.config.option_slot,"13:36")
        self.assertIsNone(args.flatten_day)

    def test_changed_source_digest_rejects_before_connection_or_new_state(self):
        code,report=self.run_main(digest="0"*64)
        self.assertEqual(code,1);self.assertIn("摘要",report["failure"])
        self.assertFalse(self.output.exists());self.assertEqual(self.source.read_bytes(),self.before)

    def test_prepared_state_restores_native_session_and_fresh_frame_without_reopening(self):
        from contextlib import ExitStack
        from bomber.framework.trader.runtime.live.contracts import LiveRunContext
        from bomber.framework.market.basic.base import make_bar
        recovery=import_module("demos.08_option_vega.recovery")
        code,report=self.run_main();self.assertEqual(code,0,report.get("failure"))
        args=entry.parse_args(report["resume_argv"])
        transport=Transport("20260814")
        # 模拟正式传输身份；所有I/O仍由本测试方法实现，不创建原生网络API。
        transport.is_test_transport=False
        with patch("bomber.framework.trader.execution.ctp.CtpNativeTraderDriver",CtpNativeTraderDriver,create=True):
            lifecycle=recovery.OptionCtpLifecycle(transport,md_front=None,orders=True,resume=True,
                scope_to_reference_instruments=True,expected_trading_day="20260814",valuation_day="20260814",
                state_file=self.output,require_fills=False)
        stamp=int(datetime(2026,8,14,13,36,tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())*1_000_000_000
        now=[stamp+14_000_000_000]
        with ExitStack() as resources:
            context=lifecycle.prepare(resources);context.references=self.current.references
            lifecycle.finish_preparation(context)
            session=runtime.assemble(args,self.current,lifecycle.driver)
            session.runner.clock_ns=lambda:now[0]
            lifecycle.start(session,context)
            for key,price in (("MO2609-C-6600",82.2),("IM2609",6500),("000852",6400)):
                identity=next(i for i,value in session.runner.key_by_instrument.items() if value==key)
                feed_id=session.runner.sources[key]
                session.runner.publish(feed_id,make_bar(instrument_id=identity,open=price,high=price,low=price,
                    close=price,volume=1,ts_event=stamp,ts_init=now[0],meta=session.feeds[feed_id].get_instrument_meta(identity)))
            now[0]+=20_000_000_000;session.runner.flush_frames()
            self.assertTrue(lifecycle.resume_ready)
            self.assertEqual(session.strategy.frames,1)
            self.assertEqual(session.strategy.entry_audit,[])
            self.assertEqual(session.runner.position_manager.position(CLIENT,OPTION),Decimal(-1))
            self.assertEqual(session.strategy.option_goals["MO2609-C-6600"],Decimal(-1))
            stopped=lifecycle.shutdown(session,context)
            self.assertEqual(stopped["final_managed_gross"],{OPTION:(Decimal(0),Decimal(1))})
            self.assertEqual(stopped["final_active_orders"],0)
        self.assertEqual((transport.sent,transport.canceled),([],[]))
        self.assertEqual(self.source.read_bytes(),self.before)

    def test_existing_output_is_not_overwritten(self):
        self.output.write_text("preserved")
        with redirect_stdout(StringIO()),self.assertRaises(SystemExit):self.run_main()
        self.assertEqual(self.output.read_text(),"preserved")

    def test_resume_wrapper_handles_ctrl_c_at_poll_boundary_and_calls_stop(self):
        resume=import_module("scripts.integration.options.simnow_resume")
        code,report=self.run_main();self.assertEqual(code,0,report.get("failure"))
        path=next((self.root/"reports").glob("*/summary.json"))
        signals={}
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,8,14,14,40,tzinfo=tz)
        class Runtime:
            def __init__(self):self.polls=0;self.stopped=0;self.controller=NS(poll=self.poll)
            def poll(self,session):self.polls+=1;return True
            def run(self):
                self.first=self.controller.poll(None)
                signals[resume.signal.SIGINT](resume.signal.SIGINT,None)
                self.after=self.controller.poll(None)
            def stop(self):self.stopped+=1
        observed=Runtime()
        with patch("dotenv.load_dotenv"),patch.object(resume,"datetime",Clock), \
                patch.object(entry,"build_runtime",return_value=observed), \
                patch.object(resume.signal,"signal",side_effect=lambda k,v:signals.__setitem__(k,v)),redirect_stdout(StringIO()):
            resume.main(["--migration-report",str(path)])
        self.assertTrue(observed.first);self.assertFalse(observed.after)
        self.assertEqual((observed.polls,observed.stopped),(1,1))

    def test_resume_after_close_rejects_before_building_runtime(self):
        resume=import_module("scripts.integration.options.simnow_resume")
        code,report=self.run_main();self.assertEqual(code,0,report.get("failure"))
        path=next((self.root/"reports").glob("*/summary.json"))
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return cls(2026,8,14,15,0,tzinfo=tz)
        with patch("dotenv.load_dotenv"),patch.object(resume,"datetime",Clock),patch.object(entry,"build_runtime") as build:
            with self.assertRaisesRegex(ValueError,"收盘前"):resume.main(["--migration-report",str(path)])
            build.assert_not_called()


if __name__=="__main__":unittest.main()
