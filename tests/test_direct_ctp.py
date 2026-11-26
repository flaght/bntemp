"""独立进程执行：真实Driver/状态机/持久化，只有柜台用可控替身。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from unittest.mock import Mock
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace as NS
import unittest
import csv
import json

from tests import test_hybrid_shared_layout  # 初始化SDK兼容测试环境。
from tests import test_shared_account as fixture


class DirectCtpTests(unittest.TestCase):
    def setUp(self):
        fixture.SharedAccountTests.setUp(self)
        self.native_modules()
    native_modules = fixture.SharedAccountTests.native_modules
    native_account = fixture.SharedAccountTests.native_account
    submit = fixture.SharedAccountTests.submit
    request = fixture.SharedAccountTests.request
    client = fixture.SharedAccountTests.client

    def test_second_account_uses_explicit_paths_without_connecting_or_changing_defaults(self):
        from contextlib import redirect_stdout
        from io import StringIO
        app = import_module('demos.direct_ctp')
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = json.loads((app.PROJECT/'demos/shared_account/account.json').read_text())
            config['account']['client_id'] = 'option-vega-z01'
            account_config = root/'account.json'
            account_config.write_text(json.dumps(config))
            options = ['--account-config', str(account_config), '--strategy-id', 'option-vega-z01',
                '--env-file', str(root/'config/.env'),
                '--account-state-file', str(root/'state.json'), '--state-file', str(root/'signal.json'),
                '--counter-file', str(root/'counter.sqlite3'), '--report-dir', str(root/'reports')]
            with patch.object(app, 'build_direct_host', side_effect=AssertionError('不能连接柜台')):
                output = StringIO()
                with redirect_stdout(output):
                    app.main('option_vega', options)
                checked = json.loads(output.getvalue())
                self.assertEqual(checked['strategies'], ['option-vega-z01'])
                for name, path in (('state_file','state.json'), ('report_dir','reports'),
                                   ('counter_file','counter.sqlite3'), ('env_file','config/.env')):
                    self.assertEqual(checked[name], str((root/path).resolve()))
                self.assertFalse(checked['orders_enabled'])
                self.assertEqual(sorted(p.name for p in root.iterdir()), ['account.json'])
                for kind, folder in (('option_vega','08_option_vega'),
                                     ('hybrid_transformer','10_hybrid_transformer')):
                    output = StringIO()
                    with redirect_stdout(output):
                        app.main(kind, [])
                    self.assertEqual(json.loads(output.getvalue())['report_dir'],
                                     str(app.PROJECT/'demos'/folder/'results/direct'))

    def test_z01_launch_profile_resolves_separate_paths_without_account_connections(self):
        from contextlib import redirect_stdout
        from io import StringIO
        app = import_module('demos.direct_ctp')
        output = StringIO()
        with patch.object(app, 'build_direct_host', side_effect=AssertionError('不能连接柜台')), \
                redirect_stdout(output):
            app.main('option_vega', ['--config-file',
                str(app.PROJECT/'demos/z01_shared_account/startup.yaml'), '--config-id','option_z01'])
        result = json.loads(output.getvalue())
        self.assertFalse(result['orders_enabled'])
        self.assertEqual(result['strategies'], ['option-vega-z01'])
        self.assertEqual(result['execution_algorithm'], 'limit_reprice')
        for name in ('state_file','report_dir','env_file'):
            self.assertTrue(Path(result[name]).is_relative_to(app.PROJECT/'demos/z01_08_option_vega'))
        self.assertTrue(Path(result['counter_file']).is_relative_to(app.PROJECT/'demos/z01_shared_account'))

    def option_report(self, account, service, directory, *, prepared=True):
        app=import_module('demos.direct_ctp')
        session=NS(strategy=NS(strategy_id='one',signals=[],option_goals={},future_goals={},last_decision={}),
            runner=NS(failure=None,frame_audit=[],position_manager=service.position_manager),
            references=NS(instrument_ids={str(self.instrument):self.instrument}),session_identity={'config':{'nav':'40000000'}})
        item=NS(strategy_id='one',session=session)
        host=NS(account=account,service=service,strategies={'one':item},_prepared=prepared)
        app.configure_direct_reports(host,dict(report_dir=str(directory),trading_day='20261013',
            strategies=[dict(strategy_id='one',kind='option_vega')]))
        return host,session,host.direct_reports[0]

    def test_direct_daily_report_exports_real_fills_owned_positions_and_signals_without_new_bar(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            account,service,broker,(one,two)=self.make(root/'state.json')
            self.start(account,service,one);two.start()
            self.submit(two,1);broker.fill(broker.sent[-1]['OrderRef'])
            host,session,adapter=self.option_report(account,service,root/'reports')
            session.strategy.signals=[{'action':'WAIT','reason':'no entry'}, {'action':'ENTRY','targets':{'MO':'-1'}}]
            adapter.record()
            self.submit(one,2);broker.fill(broker.sent[-1]['OrderRef'])
            checkpoint=(root/'state.json').read_bytes()
            adapter.record();adapter.record()
            path=adapter.report.path
            with (path/'fills.csv').open(newline='') as stream:fills=list(csv.DictReader(stream))
            self.assertEqual(len(fills),1)
            self.assertEqual((fills[0]['strategy_id'],fills[0]['quantity']),('one','2'))
            self.assertEqual(D(fills[0]['price']),3000)
            self.assertEqual(fills[0]['trading_day'],'20261013')
            with (path/'orders.csv').open(newline='') as stream:orders=list(csv.DictReader(stream))
            self.assertTrue(any(row['status']=='FILLED' for row in orders))
            snapshot=json.loads((path/'positions.json').read_text())
            self.assertEqual(snapshot['positions'],[dict(strategy_id='one',instrument_id=str(self.instrument),quantities=['2','0','0','0'])])
            self.assertEqual(len((path/'signals.jsonl').read_text().splitlines()),2)
            self.assertIn('FillEvent',(path/'events.txt').read_text())
            self.assertEqual((root/'state.json').read_bytes(),checkpoint)
            self.assertFalse((path/'sessions.jsonl').exists())
            session.finish_report();session.finish_report()
            self.assertEqual(len((path/'sessions.jsonl').read_text().splitlines()),1)
            self.assertTrue((path/'strategy_state.json').exists())
            self.assertEqual(path.name,'one-20261013')
            one.stop();two.stop();account.stop()

    def test_daily_report_restart_backfills_unexported_fills_without_duplicate_trade_rows(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            account,service,broker,(one,_) = self.make(root/'state.json')
            self.start(account,service,one)
            self.submit(one,2);broker.fill(broker.sent[-1]['OrderRef'])
            _,session,first=self.option_report(account,service,root/'reports')
            first.record();session.finish_report()
            events=(first.report.path/'events.txt').read_bytes()
            _,restarted,second=self.option_report(account,service,root/'reports')
            second.record()
            self.assertEqual((second.report.path/'events.txt').read_bytes(),events)
            self.submit(one,0,revision=2);broker.fill(broker.sent[-1]['OrderRef'])
            second.record();restarted.finish_report()
            self.assertEqual(first.report.path,second.report.path)
            with (second.report.path/'fills.csv').open(newline='') as stream:rows=list(csv.DictReader(stream))
            self.assertEqual(len(rows),2)
            self.assertEqual(len({row['event_id'] for row in rows}),2)
            self.assertEqual(len((second.report.path/'sessions.jsonl').read_text().splitlines()),2)
            with (second.report.path/'positions.csv').open(newline='') as stream:positions=list(csv.DictReader(stream))
            self.assertEqual(positions[-1]['long_today'],'0')
            one.stop();account.stop()

    def test_unprepared_daily_report_does_not_replace_previous_position_or_strategy_snapshot(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            account,service,broker,(one,_) = self.make(root/'state.json')
            self.start(account,service,one)
            self.submit(one,2);broker.fill(broker.sent[-1]['OrderRef'])
            _,session,first=self.option_report(account,service,root/'reports')
            session.finish_report()
            previous={name:(first.report.path/name).read_bytes() for name in ('positions.json','strategy_state.json','fills.csv')}
            _,failed,second=self.option_report(account,service,root/'reports',prepared=False)
            second.record();failed.finish_report()
            for name,value in previous.items():self.assertEqual((second.report.path/name).read_bytes(),value)
            self.assertEqual(json.loads((second.report.path/'summary.json').read_text())['status'],'failed')
            one.stop();account.stop()

    def test_night_session_report_uses_td_day_instead_of_calendar_date(self):
        stamp=int(datetime(2026,10,12,13,5,tzinfo=timezone.utc).timestamp()*1e9)
        with TemporaryDirectory() as directory,patch('time.time_ns',return_value=stamp):
            root=Path(directory)
            account,service,broker,(one,_) = self.make(root/'state.json')
            self.start(account,service,one)
            self.submit(one,1);broker.fill(broker.sent[-1]['OrderRef'])
            _,session,adapter=self.option_report(account,service,root/'reports')
            session.finish_report()
            with (adapter.report.path/'fills.csv').open(newline='') as stream:row=next(csv.DictReader(stream))
            self.assertEqual(row['trading_day'],'20261013')
            self.assertTrue(row['report_time'].startswith('2026-10-12T21:05:00'))
            self.assertEqual(adapter.report.path.name,'one-20261013')
            one.stop();account.stop()

    def test_vega_connected_entry_loads_calendar_from_provider_and_dispatches_direct_window(self):
        app=import_module('demos.direct_ctp')
        session_inputs=import_module('bomber.framework.dataprep.session_inputs')
        from bomber.framework.calendars.sessions import TradingWindow
        calendar=NS(binding=NS(asset_type='OPTION',hours_code='MO'),segments=(),overrides=())
        now=datetime.now(timezone.utc)
        window=TradingWindow('20261111',now,now+timedelta(hours=1),'test')
        stop=Event()
        test=self
        class Continuous:
            def __init__(self, selected_calendar, factory, **kwargs):
                test.assertIs(selected_calendar,calendar)
                test.assertTrue(kwargs['run_forever'])
                self.factory=factory
            def run(self):
                return self.factory(window,stop).run()
        with TemporaryDirectory() as directory, \
                patch('scripts.integration.shared_account.config.load_environment') as load_env, \
                patch.object(session_inputs,'load_session_inputs',return_value=calendar) as load_calendar, \
                patch('bomber.framework.trader.runtime.live.channels.ctp.required',return_value='direct-entry-fixture'), \
                patch('bomber.framework.trader.runtime.live.continuous.ContinuousPositionRuntime',Continuous), \
                patch.object(app,'run_window',return_value={'status':'completed'}) as run_window, \
                patch.object(app,'build_direct_host',side_effect=AssertionError('不能连接CTP')):
            root=Path(directory)
            result=app.main('option_vega',['--connect','--enable-orders','--confirm-simnow','--run-forever',
                '--env-file',str(root/'.env'),'--account-state-file',str(root/'state.json'),
                '--state-file',str(root/'signal.json')])
            self.assertEqual(result,{'status':'completed'})
            load_env.assert_called_once_with(root/'.env')
            load_calendar.assert_called_once()
            declaration=load_calendar.call_args.args[0]
            self.assertEqual((declaration.product,declaration.asset_type),('MO','OPTION'))
            run_window.assert_called_once()
            current,selected_window,event=run_window.call_args.args
            self.assertEqual(current['strategies'][0]['strategy_id'],'option-vega-direct')
            self.assertEqual(current['trading_day'],'20261111')
            self.assertEqual(selected_window.end,window.end-timedelta(seconds=180))
            self.assertIs(event,stop)
            self.assertIsNone(run_window.call_args.kwargs['import_state'])

    def make(self, path, *, generic_close_venues=(), extra_instruments=()):
        modules = self.native_modules()
        direct = import_module('bomber.framework.trader.execution.ctp.independent')
        life = import_module('bomber.framework.trader.runtime.live.channels.ctp_independent')
        with patch.object(modules.driver,'CtpNativeTraderDriver',direct.IndependentCtpDriver), \
                patch.object(modules.lifecycle,'CtpSharedAccountLifecycle',life.IndependentCtpLifecycle):
            account,service,broker,ports,resources = self.native_account(path,modules,
                scoped=True,preserve_external=True,execution_mode='independent',
                generic_close_venues=generic_close_venues,extra_instruments=extra_instruments)
        connect = broker.connect
        broker.connect = lambda *args: replace(connect(*args),front_id=1,session_id=2)
        raw = broker.raw
        broker.raw = lambda ref: dict(raw(ref), Direction=broker.active[ref]['Direction'],
            OffsetFlag=broker.active[ref]['CombOffsetFlag'], TradingDay='20261013')
        return account,service,broker,ports

    def start(self, account,service,one,resume=False):
        account.start(resume=resume); service.start(); one.start(); account.authorize()

    def test_foreign_session_same_ref_does_not_change_own_order_or_positions(self):
        with TemporaryDirectory() as directory:
            account,service,broker,(one,two) = self.make(Path(directory)/'state.json')
            self.start(account,service,one)
            self.submit(one,2)
            ref = broker.sent[-1]['OrderRef']
            foreign = dict(broker.raw(ref),FrontID=9,SessionID=99,OrderSysID='OTHER',OrderStatus='3')
            account.driver.on_order(foreign)
            account.driver.on_trade(dict(foreign,TradeID='FOREIGN',Volume=1,Price=3000))
            self.assertTrue(account.driver.is_simnow_authenticated)
            self.assertEqual(service.position_manager.position('one','main'),0)
            broker.fill(ref)
            self.assertEqual(service.position_manager.position('one','main'),2)
            self.assertEqual(service.position_manager.position('two','main'),0)
            one.stop();account.stop()

    def test_ownership_and_freeze_saved_before_send_and_partial_cancel_releases_remainder(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            account,service,broker,(one,_) = self.make(path)
            self.start(account,service,one)
            original = broker.send_order
            def send(fields):
                saved = account.manager.repository.load().payload['component_states']
                self.assertIn(fields['OrderRef'],saved['ctp_order_owners']['records'])
                self.assertEqual(len(saved['strategy_buckets']['frozen']),1)
                original(fields)
            broker.send_order = send
            self.submit(one,3);broker.fill(broker.sent[-1]['OrderRef'])
            self.submit(one,0,revision=2)
            ref = broker.sent[-1]['OrderRef']
            self.assertEqual(account.driver.buckets.available('one',self.instrument),(0,0))
            broker.fill(ref,1)
            self.assertEqual(account.driver.buckets.quantities('one',self.instrument),[2,0,0,0])
            broker.cancel_order(ref)
            self.assertEqual(account.driver.buckets.available('one',self.instrument),(2,0))
            one.stop();account.stop()

    def test_yesterday_must_fill_before_today_and_cancel_does_not_skip_yesterday(self):
        with TemporaryDirectory() as directory:
            account,service,broker,(one,_) = self.make(Path(directory)/'state.json')
            self.start(account,service,one)
            self.submit(one,4);broker.fill(broker.sent[-1]['OrderRef'])
            # 已核验的本策略2今+2昨；保持总仓及成本不变。
            state = account.ledger.state()
            position = replace(state.positions[self.instrument],long_today=D(2),long_yesterday=D(2),long_yesterday_basis=D(3000))
            account.ledger.restore(replace(state,positions={self.instrument:position}))
            account.driver.buckets.positions[('one',str(self.instrument))] = [D(2),D(2),D(0),D(0)]
            self.submit(one,0,revision=2)
            first = broker.sent[-1]
            self.assertEqual((first['CombOffsetFlag'],first['VolumeTotalOriginal']),('4',2))
            broker.cancel_order(first['OrderRef'])
            self.now += 1
            one.continue_target('one','main',self.now,instrument_id=self.instrument)
            again = broker.sent[-1]
            self.assertEqual(again['CombOffsetFlag'],'4')
            broker.fill(again['OrderRef'])
            self.now += 1
            one.continue_target('one','main',self.now,instrument_id=self.instrument)
            today = broker.sent[-1]
            self.assertEqual((today['CombOffsetFlag'],today['VolumeTotalOriginal']),('3',2))
            broker.fill(today['OrderRef'])
            self.assertEqual(service.position_manager.position('one','main'),0)
            one.stop();account.stop()

    def test_terminal_mapping_deduplicates_old_trade_after_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            account,service,broker,(one,_) = self.make(path)
            self.start(account,service,one)
            self.submit(one,2)
            ref=broker.sent[-1]['OrderRef']; raw=broker.raw(ref)
            broker.fill(ref)
            duplicate = dict(raw,TradeID=f'T{ref}-2',Volume=2,Price=3000)
            one.stop();account.stop()
            other,target,transport,(port,_) = self.make(path)
            transport.gross=deepcopy(broker.gross)
            connect=transport.connect
            transport.connect=lambda *args:replace(connect(*args),front_id=8,session_id=80)
            self.start(other,target,port,resume=True)
            other.driver.on_trade(duplicate)
            self.assertEqual(target.position_manager.position('one','main'),2)
            self.assertFalse(transport.sent)
            port.stop();other.stop()

    def test_active_old_session_order_recovers_and_releases_only_its_frozen_bucket(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            account,service,broker,(one,_) = self.make(path)
            self.start(account,service,one)
            self.submit(one,2)
            ref=broker.sent[-1]['OrderRef']
            account.driver.stop()
            other,target,transport,(port,_) = self.make(path)
            transport.active=deepcopy(broker.active)
            connect=transport.connect
            transport.connect=lambda *args:replace(connect(*args),front_id=8,session_id=80)
            self.start(other,target,port,resume=True)
            self.assertIn(ref,other.driver._orders)
            transport.fill(ref)
            self.assertEqual(target.position_manager.position('one','main'),2)
            self.assertFalse(other.driver.buckets.frozen)
            self.assertFalse(transport.sent)
            port.stop();other.stop()

    def test_external_other_process_positions_can_change_without_adopting_them(self):
        with TemporaryDirectory() as directory:
            account,service,broker,(one,_) = self.make(Path(directory)/'state.json')
            broker.gross={'IF2611.CFFEX':(1,0)}
            self.start(account,service,one)
            broker.gross={'IF2611.CFFEX':(3,0),'MO2611-C-7000.CFFEX':(0,2)}
            account._validate_account()
            self.assertEqual(service.position_manager.position('one','main'),0)
            self.assertFalse(broker.sent)
            one.stop();account.stop()

    def test_concurrent_foreign_trade_uses_one_position_snapshot_and_keeps_each_owner(self):
        with TemporaryDirectory() as directory:
            account,service,broker,(one,two) = self.make(Path(directory)/'state.json')
            self.start(account,service,one);two.start()
            self.submit(one,2);broker.fill(broker.sent[-1]['OrderRef'])
            self.submit(two,-1);broker.fill(broker.sent[-1]['OrderRef'])
            ledger = account.ledger.state()
            positions = service.position_manager.state()
            query = broker.query_position_details
            def foreign_trade_after_snapshot():
                rows = query()
                # 其他进程在完整回报之后成交，且交易的是本进程也在管理的合约。
                broker.gross[str(self.instrument)] = (5, 3)
                broker.gross['IF2611.CFFEX'] = (1, 0)
                return rows
            with patch.object(broker,'query_position_details',side_effect=foreign_trade_after_snapshot) as details, \
                    patch.object(broker,'query_gross_positions',side_effect=AssertionError('不能另查总仓')):
                account._next_check = 0
                account.poll()
                details.assert_called_once_with()
                self.assertEqual(account.external_gross,{})
                details.reset_mock()
                account._next_check = 0
                account.poll()
                details.assert_called_once_with()
            self.assertEqual(account.external_gross,{str(self.instrument):(3,2),'IF2611.CFFEX':(1,0)})
            self.assertEqual(account.ledger.state(),ledger)
            self.assertEqual(service.position_manager.state(),positions)
            self.assertEqual(service.position_manager.position('one','main'),2)
            self.assertEqual(service.position_manager.position('two','main'),-1)
            self.assertEqual(len(broker.sent),2)
            one.stop();two.stop();account.stop()

    def test_account_counter_is_atomic_across_independent_connections_and_restarts(self):
        direct=import_module('bomber.framework.trader.execution.ctp.independent')
        with TemporaryDirectory() as directory:
            path=Path(directory)/'counter.sqlite3'
            counters=[direct.DailyOrderCounter(path,'9999:demo',7) for _ in range(2)]
            def send(i):
                try:counters[i%2].reserve('20261111',str(i));return True
                except direct.OrderBudgetExhausted:return False
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(send,range(30))),7)
            restored=direct.DailyOrderCounter(path,'9999:demo',7)
            with self.assertRaises(direct.OrderBudgetExhausted):restored.check('20261111')
            restored.check('20261112')

    def test_strategy_buckets_never_freeze_other_strategy_position(self):
        direct=import_module('bomber.framework.trader.execution.ctp.independent')
        contracts=import_module('bomber.framework.trader.execution.contracts')
        buckets=direct.StrategyBuckets('20261111')
        buckets.positions[('other',str(self.instrument))]=[D(0),D(5),D(0),D(0)]
        order=contracts.OrderIntent('one','account',self.instrument,'SELL',2,
            position_effect='CLOSE_YESTERDAY',reduce_only=True)
        with self.assertRaisesRegex(RuntimeError,'本策略'):
            buckets.reserve('id',order)

    def test_import_keeps_ten_positions_revisions_and_cost_and_does_not_write_source(self):
        direct_app=import_module('demos.direct_ctp')
        with TemporaryDirectory() as directory:
            root=Path(directory)
            modules=self.native_modules()
            old,source,broker,(one,_),_=self.native_account(root/'old.json',modules,
                scoped=True,preserve_external=True,execution_mode='independent')
            identity=dict(account={'client_id':'account'},environment='realtime',trading_day='20261013',
                          strategies={'one':{},'two':{}})
            old.manager.state_components['session_identity'].identity=deepcopy(identity)
            self.start(old,source,one)
            self.submit(one,2);broker.fill(broker.sent[-1]['OrderRef'])
            one.stop();old.stop()
            original=(root/'old.json').read_bytes()
            saved=old.manager.repository.load().payload
            other,target,transport,(port,_) = self.make(root/'direct.json')
            transport.gross=deepcopy(broker.gross)
            other.manager.state_components['session_identity'].identity=deepcopy(identity)
            other.initial_payload=direct_app.project_hybrid(saved,other)
            other.import_transform=lambda payload:direct_app.prepare_payload(other,payload)
            other.start_preparation(resume=True)
            self.assertFalse((root/'direct.json').exists())
            target.start();port.start();other.authorize()
            restored=other.manager.repository.load().payload
            self.assertEqual((root/'old.json').read_bytes(),original)
            self.assertEqual(restored['positions']['attribution']['owned'],saved['positions']['attribution']['owned'])
            self.assertEqual(restored['ctp_ledgers'],saved['ctp_ledgers'])
            self.assertEqual(restored['component_states']['shared_account']['sources'],saved['component_states']['shared_account']['sources'])
            self.assertEqual(other.driver.buckets.quantities('one',self.instrument),[2,0,0,0])
            self.assertFalse(transport.sent)
            port.stop();other.stop()

    def test_partial_fill_while_disconnected_is_recovered_from_trade_query(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            account,service,broker,(one,_) = self.make(path)
            self.start(account,service,one);self.submit(one,3)
            ref=broker.sent[-1]['OrderRef'];raw=broker.raw(ref)
            account.driver.stop()
            other,target,transport,(port,_) = self.make(path)
            transport.active=deepcopy(broker.active)
            transport.active[ref]['filled']=1
            transport.gross={str(self.instrument):(1,0)}
            terminal=dict(raw,OrderStatus='1',OrderSubmitStatus='3',VolumeTotalOriginal=3,VolumeTraded=1,VolumeTotal=2)
            transport.queried_order_states={f'CTP-9999-demo-20261013-{ref}':terminal}
            trade=dict(raw,TradeID='REAL-MISSED',Volume=1,Price=2998,TradeDate='20261013',TradeTime='09:15:00')
            transport.query_trades=lambda:(trade,trade)
            self.start(other,target,port,resume=True)
            self.assertEqual(target.position_manager.position('one','main'),1)
            self.assertEqual(other.ledger.snapshot(self.instrument).long_today_basis,2998)
            self.assertEqual(target.position_manager.working_quantity('account',self.instrument),2)
            self.assertFalse(transport.sent)
            port.stop();other.stop()

    def test_missing_trade_evidence_keeps_old_checkpoint_and_frozen_order(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            account,service,broker,(one,_) = self.make(path)
            self.start(account,service,one);self.submit(one,3)
            ref=broker.sent[-1]['OrderRef'];raw=broker.raw(ref)
            account.driver.stop();original=path.read_bytes()
            other,target,transport,(port,_) = self.make(path)
            transport.active=deepcopy(broker.active);transport.active[ref]['filled']=1
            transport.gross={str(self.instrument):(1,0)}
            transport.queried_order_states={f'CTP-9999-demo-20261013-{ref}':dict(raw,VolumeTraded=1)}
            transport.query_trades=lambda:()
            with self.assertRaisesRegex(RuntimeError,'真实成交证据'):
                other.start(resume=True)
            self.assertEqual(path.read_bytes(),original)
            self.assertFalse(transport.sent or transport.canceled)

    def test_unknown_trade_before_order_is_buffered_then_attributed_once(self):
        with TemporaryDirectory() as directory:
            account,service,broker,(one,_) = self.make(Path(directory)/'state.json')
            self.start(account,service,one)
            send=broker.send_order
            def early(fields):
                callback=broker.on_order
                broker.on_order=lambda raw:None
                send(fields)
                broker.on_order=callback
                ref=fields['OrderRef'];order=broker.raw(ref)
                broker.fill(ref)
                self.assertEqual(service.position_manager.position('one','main'),0)
                callback(dict(order,OrderStatus='0',VolumeTraded=fields['VolumeTotalOriginal']))
            broker.send_order=early
            self.submit(one,2)
            self.assertEqual(service.position_manager.position('one','main'),2)
            self.assertFalse(account.driver._unmatched_trades)
            one.stop();account.stop()

    def test_real_transport_filters_query_collisions_and_cancels_original_session_after_relogin(self):
        direct=import_module('bomber.framework.trader.execution.ctp.independent')
        api_fixture=import_module('tests.run_p4_ctp_td_transport')
        driver_module=import_module('bomber.framework.trader.execution.ctp.native_driver')
        contracts=import_module('bomber.framework.trader.execution.contracts')
        instrument=type(self.instrument).from_str('rb2704.SHFE')
        raw=dict(BrokerID='9999',InvestorID='demo',InstrumentID='rb2704',ExchangeID='SHFE',
            OrderRef='18',FrontID=1,SessionID=2,OrderSysID='OWN',Direction='0',
            OrderStatus='3',OrderSubmitStatus='3',VolumeTotalOriginal=2,VolumeTraded=0,VolumeTotal=2)
        foreign=dict(raw,FrontID=9,SessionID=99,OrderSysID='FOREIGN')
        class Api(api_fixture.FakeTdApi):
            def init(self):
                super().init()
                self.onRtnOrder(foreign)
            def reqUserLogin(self,data,reqid):
                self.onRspUserLogin(dict(BrokerID='9999',UserID='demo',TradingDay='20260922',
                    MaxOrderRef='18',FrontID=8,SessionID=80),{},reqid,True)
                return 0
            def reqQryOrder(self,data,reqid):
                self.onRspQryOrder(foreign,{},reqid,False)
                self.onRspQryOrder(raw,{},reqid,True)
                return 0
            def reqOrderAction(self,data,reqid):
                self.action=deepcopy(data)
                return 0
        transport=direct.IndependentTdTransport(client_id='account',account_id='demo',
            front='tcp://fake:1234',broker_id='9999',investor_id='demo',password='fake-password',
            app_id='fake-app',auth_code='fake-auth',td_api_base=Api,timeout_seconds=.1)
        book=direct.OrderIdentityBook('9999','demo');book.day='20260922'
        book.add('18','CTP-9999-demo-20260922-18',contracts.OrderIntent('one','account',instrument,
            'BUY',2,position_effect='OPEN'),driver_module.CtpTraderSession('9999','demo','20260922',18,1,2))
        transport.accept_order=book.accept_order
        received=[]
        session=transport.connect(received.append,received.append,lambda _:None)
        try:
            self.assertEqual((session.front_id,session.session_id),(8,80))
            transport.activate()
            self.assertFalse(received)
            snapshot=transport.query_active_orders()
            self.assertEqual(len(snapshot.orders),1)
            self.assertEqual(transport._last_orders['18']['OrderSysID'],'OWN')
            transport.cancel_order('18')
            self.assertEqual((transport._api.action['FrontID'],transport._api.action['SessionID']),(1,2))
        finally:
            transport.close()

    def test_terminal_close_missing_on_disconnect_recovers_real_fill_without_resend(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            account,service,broker,(one,_) = self.make(path)
            self.start(account,service,one)
            self.submit(one,3);broker.fill(broker.sent[-1]['OrderRef'])
            self.submit(one,0,revision=2)
            ref=broker.sent[-1]['OrderRef'];raw=broker.raw(ref)
            account.driver.stop()
            other,target,transport,(port,_) = self.make(path)
            terminal=dict(raw,OrderStatus='0',OrderSubmitStatus='3',VolumeTotalOriginal=3,VolumeTraded=3,VolumeTotal=0)
            transport.queried_order_states={f'CTP-9999-demo-20261013-{ref}':terminal}
            transport.query_trades=lambda:(dict(raw,TradeID='REAL-CLOSE',Volume=3,Price=2998,
                TradeDate='20261013',TradeTime='10:15:00'),)
            self.start(other,target,port,resume=True)
            self.assertEqual(target.position_manager.position('one','main'),0)
            self.assertEqual(other.ledger.snapshot(self.instrument).long_total,0)
            self.assertFalse(transport.sent or other.driver.buckets.frozen)
            port.stop();other.stop()

    def test_cross_day_rolls_only_owned_today_to_yesterday_without_new_order(self):
        app=import_module('demos.direct_ctp')
        with TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            account,service,broker,(one,_) = self.make(path)
            identity=dict(account={'client_id':'account'},environment='realtime',trading_day='20261013',
                strategies={sid:dict(strategy_id=sid,kind='hybrid_transformer',trading_day='20261013',
                    environment='realtime',argv=[],reference='fixed',routes={}) for sid in ('one','two')})
            account.manager.state_components['session_identity'].identity=deepcopy(identity)
            self.start(account,service,one)
            self.submit(one,2);broker.fill(broker.sent[-1]['OrderRef'])
            one.stop();account.stop()
            other,target,transport,(port,_) = self.make(path)
            other.expected_day=other.driver.book.day=other.driver.buckets.day='20261014'
            current=deepcopy(identity);current['trading_day']='20261014'
            for row in current['strategies'].values():row['trading_day']='20261014'
            other.manager.state_components['session_identity'].identity=current
            other.import_transform=lambda payload:app.prepare_payload(other,payload)
            connect=transport.connect
            transport.connect=lambda *args:replace(connect(*args),trading_day='20261014',front_id=8,session_id=80)
            transport.gross=deepcopy(broker.gross)
            details=transport.query_position_details
            transport.query_position_details=lambda:[dict(row,TodayPosition=0,PositionDate='2') for row in details()]
            self.start(other,target,port,resume=True)
            self.assertEqual(other.driver.buckets.quantities('one',self.instrument),[0,2,0,0])
            self.assertEqual(other.ledger.snapshot(self.instrument).long_yesterday,2)
            self.assertEqual(target.position_manager.position('one','main'),2)
            self.assertFalse(transport.sent)
            port.stop();other.stop()

    def option_scope_recovery(self, directory):
        app=import_module('demos.direct_ctp')
        root=Path(directory);path=root/'state.json'
        old,service,broker,(one,two)=self.make(path)
        reference=dict(mode='simnow',environment='realtime',td_day='20261013',valuation_day='2026-10-13',
            policy=dict(version=1,account_reconciliation='strict',position_source='fills'),
            config={'max_market_age_seconds':300},calendar_manifest={'calendar_id':'china.cffex'},
            sources={'market':{'channel':'dolphindb','max_age_seconds':120}},labels={})
        identity=dict(account={'client_id':'account'},environment='realtime',trading_day='20261013',
            managed_instruments=sorted(old.position_scope.instruments),strategies={
                port.strategy_id:dict(strategy_id=port.strategy_id,kind='option_vega' if port is one else 'hybrid_transformer',
                    trading_day='20261013',environment='realtime',argv=[],routes=port.configuration(),
                    reference=deepcopy(reference) if port is one else 'fixed') for port in (one,two)})
        old.manager.state_components['session_identity'].identity=deepcopy(identity)
        counter=import_module('bomber.framework.trader.execution.ctp.independent').DailyOrderCounter(
            root/'counter.sqlite3','9999:demo',2)
        old.driver.daily_counter=counter
        self.start(old,service,one);two.start()
        self.submit(one,-1);broker.fill(broker.sent[-1]['OrderRef'])
        self.submit(two,2);broker.fill(broker.sent[-1]['OrderRef'])
        one.stop();two.stop();old.stop()
        original=path.read_bytes();payload=old.manager.repository.load().payload
        added=tuple(type(self.instrument).from_str(code) for code in ('MO2611-C-5400.CFFEX','MO2612-C-5400.CFFEX'))
        account,target,transport,(port,second)=self.make(path,extra_instruments=added)
        port.routes.update({str(i):i for i in added})
        second.instrument_limits={self.instrument:second.instrument_limits[self.instrument]}
        account.expected_day=account.driver.book.day=account.driver.buckets.day='20261014'
        current=deepcopy(identity);current.update(trading_day='20261014',managed_instruments=sorted(account.position_scope.instruments))
        for p in (port,second):
            row=current['strategies'][p.strategy_id]
            row.update(trading_day='20261014',routes=p.configuration())
        current['strategies']['one']['reference'].update(td_day='20261014',valuation_day='2026-10-14')
        account.manager.state_components['session_identity'].identity=current
        account.import_transform=lambda data:app.prepare_payload(account,data)
        connect=transport.connect
        transport.connect=lambda *args:replace(connect(*args),trading_day='20261014')
        transport.gross=deepcopy(broker.gross)
        details=transport.query_position_details
        transport.query_position_details=lambda:[dict(row,TodayPosition=0,PositionDate='2') for row in details()]
        account.driver.daily_counter=counter
        return NS(app=app,path=path,payload=payload,original=original,account=account,service=target,
            transport=transport,port=port,second=second,current=current,added=added,counter=counter)

    def test_same_day_market_age_change_restores_original_checkpoint_owners_and_daily_count(self):
        with TemporaryDirectory() as directory:
            case=self.option_scope_recovery(directory)
            account,service,broker,(one,two)=self.make(case.path)
            identity=deepcopy(case.payload['component_states']['session_identity']['identity'])
            identity['strategies']['one']['reference']['sources']['market']['max_age_seconds']=300
            account.manager.state_components['session_identity'].identity=identity
            account.import_transform=lambda data:case.app.prepare_payload(account,data)
            broker.gross=deepcopy(case.transport.gross)
            account.start_preparation(resume=True)
            self.assertEqual(case.path.read_bytes(),case.original)
            self.assertEqual(service.position_manager.position('one','main'),-1)
            self.assertEqual(service.position_manager.position('two','main'),2)
            service.start();one.start();two.start();account.authorize()
            with self.assertRaises(import_module('bomber.framework.trader.execution.ctp.independent').OrderBudgetExhausted):
                case.counter.check('20261013')
            self.assertFalse(broker.sent)
            one.stop();two.stop();account.stop()

    def test_same_day_identity_check_accepts_exact_identity_and_keeps_date_environment_guards(self):
        from scripts.integration.shared_account.rollover import validate_strategy_identity
        for kind in ('option_vega', 'hybrid_transformer'):
            old = dict(strategy_id='one', kind=kind, trading_day='20261126',
                environment='realtime', argv=[], routes={}, reference={'config': {}})
            before = deepcopy(old)
            validate_strategy_identity(old, deepcopy(old))
            self.assertEqual(old, before)
            for change in ({'trading_day': '20261125'}, {'environment': 'replay'}):
                with self.subTest(kind=kind, change=change), self.assertRaises(ValueError):
                    validate_strategy_identity(old, {**old, **change})

    def test_same_day_reference_mismatch_reports_paths_and_preserves_saved_book(self):
        with TemporaryDirectory() as directory:
            case = self.option_scope_recovery(directory)
            original = deepcopy(case.payload)
            base = deepcopy(original['component_states']['session_identity']['identity'])
            case.account.expected_day = base['trading_day']
            for field, path in (
                    ('fingerprint', 'session.reference.reference_fingerprint'),
                    ('config', 'session.reference.config.max_market_age_seconds'),
                    ('source', 'session.reference.sources.market.table'),
                    ('routes', 'session.routes.limits'),
                    ('argv', 'session.argv')):
                identity = deepcopy(base)
                row = identity['strategies']['one']
                secret = 'value-must-not-appear-in-error'
                if field == 'fingerprint': row['reference']['reference_fingerprint'] = secret
                elif field == 'config': row['reference']['config']['max_market_age_seconds'] = 299
                elif field == 'source': row['reference']['sources']['market']['table'] = secret
                elif field == 'routes': row['routes']['limits'] = {secret: {}}
                else: row['argv'] = [secret]
                case.account.manager.state_components['session_identity'].identity = identity
                with self.subTest(field=field), self.assertRaisesRegex(
                        ValueError, '共享同日恢复配置不一致') as caught:
                    case.app.prepare_payload(case.account, case.payload)
                self.assertIn(path, str(caught.exception))
                if field != 'routes': self.assertNotIn(secret, str(caught.exception))
                self.assertEqual(case.payload, original)
                self.assertEqual(case.path.read_bytes(), case.original)
                self.assertFalse(case.transport.sent or case.transport.canceled)

    def test_cross_day_option_scope_additions_restore_owners_zero_targets_and_keep_daily_count(self):
        with TemporaryDirectory() as directory:
            case=self.option_scope_recovery(directory)
            before=deepcopy(case.payload)
            transformed=case.app.prepare_payload(case.account,case.payload)
            self.assertEqual(case.payload,before)
            self.assertEqual(case.path.read_bytes(),case.original)
            self.assertEqual(transformed['positions'],before['positions'])
            self.assertEqual(transformed['orders'],before['orders'])
            shared=transformed['component_states']['shared_account']
            source=shared['sources']['one']
            for instrument in case.added:self.assertEqual(source['targets'][str(instrument)],'0')
            self.assertEqual(source['revision'],before['component_states']['shared_account']['sources']['one']['revision'])
            self.start(case.account,case.service,case.port,resume=True);case.second.start()
            self.assertEqual(case.service.position_manager.position('one','main'),-1)
            self.assertEqual(case.service.position_manager.position('two','main'),2)
            self.assertEqual(case.account.driver.buckets.quantities('one',self.instrument),[0,0,0,1])
            for instrument in case.added:
                self.assertEqual(case.account.ledger.snapshot(instrument).net_position,0)
                self.assertEqual(case.service.position_manager.position('one',str(instrument)),0)
            with self.assertRaises(import_module('bomber.framework.trader.execution.ctp.independent').OrderBudgetExhausted):
                case.counter.check('20261013')
            self.assertFalse(case.transport.sent)
            case.port.stop();case.second.stop();case.account.stop()

    def test_legacy_entry_default_fields_restore_same_and_next_day_without_resetting_positions_or_counter(self):
        for cross_day in (False,True):
            with self.subTest(cross_day=cross_day),TemporaryDirectory() as directory:
                case=self.option_scope_recovery(directory)
                if cross_day:
                    account,service,broker,one,two=case.account,case.service,case.transport,case.port,case.second
                    identity=case.current
                else:
                    account,service,broker,(one,two)=self.make(case.path)
                    identity=deepcopy(case.payload['component_states']['session_identity']['identity'])
                    broker.gross=deepcopy(case.transport.gross)
                    account.manager.state_components['session_identity'].identity=identity
                    account.import_transform=lambda data:case.app.prepare_payload(account,data)
                identity['strategies']['one']['reference']['config'].update(
                    entry_quote_policy='synchronized',index_signal_minutes=1)
                account.start_preparation(resume=True)
                self.assertEqual(case.path.read_bytes(),case.original)
                self.assertEqual(service.position_manager.position('one','main'),-1)
                self.assertEqual(service.position_manager.position('two','main'),2)
                self.assertEqual(account.driver.buckets.quantities('one',self.instrument),
                    [0,0,0,1] if cross_day else [0,0,1,0])
                with self.assertRaises(import_module('bomber.framework.trader.execution.ctp.independent').OrderBudgetExhausted):
                    case.counter.check('20261013')
                self.assertFalse(broker.sent)
                service.start();one.start();two.start();account.authorize()
                one.stop();two.stop();account.stop()

    def test_option_scope_addition_rejects_other_changes_and_keeps_original_checkpoint(self):
        for fault in ('same_day','removed','old_limit','budget','hybrid','unknown_scope','frozen'):
            with self.subTest(fault=fault),TemporaryDirectory() as directory:
                case=self.option_scope_recovery(directory)
                if fault=='same_day':case.current['trading_day']='20261013';case.account.expected_day='20261013'
                elif fault=='removed':case.current['managed_instruments'].remove(str(self.instrument))
                elif fault=='old_limit':
                    case.current['strategies']['one']['routes']['limits'][str(self.instrument)]['max_abs_position']='999'
                elif fault=='budget':case.current['account']['max_session_orders']=999
                elif fault=='hybrid':case.current['strategies']['two']['argv']=['changed']
                elif fault=='unknown_scope':case.current['managed_instruments'].append('OTHER.SHFE')
                else:case.payload['component_states']['strategy_buckets']['frozen']['old-order']={}
                before=deepcopy(case.payload)
                with self.assertRaises(ValueError):case.app.prepare_payload(case.account,case.payload)
                self.assertEqual(case.payload,before)
                self.assertEqual(case.path.read_bytes(),case.original)
                self.assertFalse(case.transport.sent)

    def test_network_disconnect_rebuilds_host_from_same_files_without_stopping_stale_account(self):
        app=import_module('demos.direct_ctp')
        class Stop(Event):
            def wait(self,timeout=None):return self.is_set()
        stop=Stop();now=datetime.now(timezone.utc)
        window=NS(start=now-timedelta(minutes=1),end=now+timedelta(minutes=1),trading_day='20261111')
        def host(disconnected):
            driver=NS(last_disconnect={'reason':0x1001} if disconnected else None,
                is_simnow_authenticated=not disconnected,stop=Mock())
            return NS(account=NS(driver=driver),service=NS(client=NS(report_errors=()),_checkpoint=Mock()),
                strategies={},resources=NS(close=Mock()),start=Mock(),stop=Mock(),snapshot=lambda:{})
        old,new=host(True),host(False)
        old.poll=Mock(side_effect=RuntimeError('network disconnected'))
        new.poll=stop.set
        with TemporaryDirectory() as directory, patch.object(app,'build_direct_host',side_effect=[old,new]) as build:
            result=app.run_window(dict(report_dir=directory,strategies=[{'strategy_id':'one'}]),window,stop,
                import_state=Path(directory)/'old.json',counter_file=Path(directory)/'counter.sqlite3')
            self.assertEqual(result,{'status':'completed'})
            self.assertEqual(build.call_count,2)
            self.assertEqual(build.call_args_list[0],build.call_args_list[1])
            old.stop.assert_not_called()
            old.account.driver.stop.assert_called_once()
            self.assertIsNone(old.service._checkpoint)
            new.stop.assert_called_once()

    def test_independent_query_failures_disarm_without_rewriting_positions_checkpoint_or_counter(self):
        errors=import_module('bomber.framework.trader.execution.ctp.query_errors')
        direct=import_module('bomber.framework.trader.execution.ctp.independent')
        cases=[(stage,TimeoutError(stage+' timeout')) for stage in ('account','positions','orders')]
        cases += [('orders',errors.CtpQueryRejected('orders','reqQryOrder',999,-2))]
        for stage,error in cases:
            with self.subTest(stage=stage,error=str(error)),TemporaryDirectory() as directory:
                path=Path(directory)/'state.json'
                account,service,broker,(one,two)=self.make(path)
                counter=direct.DailyOrderCounter(Path(directory)/'counter.sqlite3','9999:demo',2)
                account.driver.daily_counter=counter
                self.start(account,service,one);two.start()
                self.submit(one,2);broker.fill(broker.sent[-1]['OrderRef'])
                self.submit(two,-1);broker.fill(broker.sent[-1]['OrderRef'])
                before=path.read_bytes();ledger=account.ledger.state()
                buckets=deepcopy(account.driver.buckets.snapshot_state())
                method={'account':'query_account','positions':'query_position_details','orders':'query_active_orders'}[stage]
                with patch.object(broker,method,side_effect=error):
                    account._next_check=0
                    with self.assertRaises(type(error)) as caught:account.poll()
                self.assertIs(caught.exception,error)
                self.assertEqual(account.query_failure,str(error))
                self.assertFalse(account.client.is_armed)
                self.assertTrue(service.position_manager.is_recovery_required(service.client_id))
                self.assertEqual(account.ledger.state(),ledger)
                self.assertEqual(account.driver.buckets.snapshot_state(),buckets)
                self.assertEqual(path.read_bytes(),before)
                self.assertEqual(service.position_manager.position('one','main'),2)
                self.assertEqual(service.position_manager.position('two','main'),-1)
                with self.assertRaises(direct.OrderBudgetExhausted):counter.check('20261013')
                self.assertEqual(len(broker.sent),2)
                account.driver.stop()

    def test_query_timeout_can_finish_authoritative_stop_after_late_fill_without_rearming(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            account,service,broker,(one,_)=self.make(path)
            self.start(account,service,one)
            self.submit(one,2);ref=broker.sent[-1]['OrderRef']
            broker.fill(ref,1)
            with patch.object(broker,'query_active_orders',side_effect=TimeoutError('orders timeout')):
                account._next_check=0
                with self.assertRaises(TimeoutError):account.poll()
            self.assertFalse(account.client.is_armed)
            self.assertTrue(account.driver.buckets.frozen)
            broker.fill(ref,1)  # 旧连接仍有效，迟到真实成交仍按原订单归属入账。
            one.stop();account.stop()
            self.assertEqual(account.final_active_orders,0)
            self.assertEqual(service.position_manager.position('one','main'),2)
            self.assertFalse(account.client.is_armed)
            self.assertFalse(account.driver.buckets.frozen)
            self.assertEqual(len(broker.sent),1)
            owned=json.loads(path.read_text())['payload']['positions']['attribution']['owned']
            self.assertEqual(sum(D(row['quantity']) for row in owned if row['strategy_id']=='one'),2)

    def test_timeout_then_stop_disconnect_restores_real_owners_and_original_daily_counter(self):
        app=import_module('demos.direct_ctp')
        direct=import_module('bomber.framework.trader.execution.ctp.independent')
        class Stop(Event):
            def wait(self,timeout=None):return self.is_set()
        stop=Stop();now=datetime.now(timezone.utc)
        window=NS(start=now-timedelta(minutes=1),end=now+timedelta(minutes=1),trading_day='20261013')
        with TemporaryDirectory() as directory:
            root=Path(directory);path=root/'state.json';signal=root/'signal.json';counter_file=root/'counter.sqlite3'
            signal.write_text('{"original_signal":true}')
            account,service,broker,(one,two)=self.make(path)
            account.driver.daily_counter=direct.DailyOrderCounter(counter_file,'9999:demo',2)
            self.start(account,service,one);two.start()
            self.submit(one,2);broker.fill(broker.sent[-1]['OrderRef'])
            self.submit(two,-1);broker.fill(broker.sent[-1]['OrderRef'])
            before=path.read_bytes();signal_before=signal.read_bytes();gross=deepcopy(broker.gross)
            old=NS(account=account,service=service,strategies={},resources=NS(close=Mock()),
                start=Mock(),snapshot=lambda:{})
            def poll():
                with patch.object(broker,'query_active_orders',side_effect=TimeoutError('orders timeout')):
                    account._next_check=0;account.poll()
            old.poll=poll
            def stop_old():
                self.assertFalse(account.client.is_armed)
                account.driver.on_disconnect(0x2001)
                raise RuntimeError('CTP等待旧查询结束时前置断开')
            old.stop=Mock(side_effect=stop_old)
            restored=[]
            def build(config,**kwargs):
                self.assertEqual(config['account_state_file'],str(path))
                self.assertEqual(kwargs['counter_file'],counter_file)
                self.assertEqual(signal.read_bytes(),signal_before)
                if not restored:
                    restored.append(old)
                    return old
                self.assertEqual(path.read_bytes(),before)
                self.assertIsNone(service._checkpoint)
                next_account,next_service,transport,(port,second)=self.make(path)
                transport.gross=deepcopy(gross)
                next_account.driver.daily_counter=direct.DailyOrderCounter(counter_file,'9999:demo',2)
                def start():
                    self.start(next_account,next_service,port,resume=True);second.start()
                    self.assertEqual(next_service.position_manager.position('one','main'),2)
                    self.assertEqual(next_service.position_manager.position('two','main'),-1)
                    self.assertEqual(next_account.driver.buckets.quantities('one',self.instrument),[2,0,0,0])
                    self.assertEqual(next_account.driver.buckets.quantities('two',self.instrument),[0,0,1,0])
                    with self.assertRaises(direct.OrderBudgetExhausted):next_account.driver.daily_counter.check('20261013')
                    self.assertFalse(transport.sent)
                def finish():
                    port.stop();second.stop();next_account.stop()
                new=NS(account=next_account,service=next_service,start=start,poll=stop.set,
                    stop=Mock(side_effect=finish),snapshot=lambda:{})
                restored.append(new)
                return new
            config=dict(report_dir=directory,account_state_file=str(path),strategies=[{'strategy_id':'one'}])
            with patch.object(app,'build_direct_host',side_effect=build) as factory:
                self.assertEqual(app.run_window(config,window,stop,import_state=None,counter_file=counter_file),
                    {'status':'completed'})
            self.assertEqual(factory.call_count,2)
            self.assertEqual(factory.call_args_list[0],factory.call_args_list[1])
            self.assertEqual(signal.read_bytes(),signal_before)
            old.stop.assert_called_once();restored[1].stop.assert_called_once()

    def test_stop_failure_without_recoverable_disconnect_does_not_reconnect(self):
        app=import_module('demos.direct_ctp')
        for reason,report_errors in ((None,()),(-1,()),(0x2001,('ownership conflict',))):
            with self.subTest(reason=reason,report_errors=report_errors),TemporaryDirectory() as directory:
                now=datetime.now(timezone.utc)
                window=NS(start=now,end=now+timedelta(minutes=1),trading_day='20261116')
                driver=NS(last_disconnect=None,is_simnow_authenticated=True)
                host=NS(account=NS(driver=driver),service=NS(client=NS(report_errors=())),
                    start=Mock(),poll=Mock(side_effect=TimeoutError('orders timeout')),snapshot=lambda:{})
                def fail_stop():
                    driver.last_disconnect=None if reason is None else {'reason':reason}
                    host.service.client.report_errors=report_errors
                    raise RuntimeError('stop reconciliation failed')
                host.stop=Mock(side_effect=fail_stop)
                with patch.object(app,'build_direct_host',return_value=host) as build, \
                        patch.object(app,'dispose_disconnected') as dispose:
                    with self.assertRaisesRegex(RuntimeError,'stop reconciliation failed'):
                        app.run_window(dict(report_dir=directory,strategies=[]),window,Event(),
                            import_state=None,counter_file=Path(directory)/'counter.sqlite3')
                self.assertEqual(build.call_count,1);dispose.assert_called_once_with(host)

    def test_stop_disconnect_then_query_timeouts_exhaust_three_reconnect_attempts(self):
        app=import_module('demos.direct_ctp')
        class Stop(Event):
            def wait(self,timeout=None):return self.is_set()
        now=datetime.now(timezone.utc)
        window=NS(start=now,end=now+timedelta(minutes=1),trading_day='20261116')
        def make_host():
            driver=NS(last_disconnect=None,is_simnow_authenticated=True)
            host=NS(account=NS(driver=driver),service=NS(client=NS(report_errors=())),start=Mock(),
                poll=Mock(side_effect=TimeoutError('orders timeout')),snapshot=lambda:{})
            def fail_stop():
                driver.last_disconnect={'reason':0x2001}
                raise RuntimeError('heartbeat timeout during stop')
            host.stop=Mock(side_effect=fail_stop)
            return host
        hosts=[make_host() for _ in range(4)]
        with TemporaryDirectory() as directory,patch.object(app,'build_direct_host',side_effect=hosts) as build, \
                patch.object(app,'dispose_disconnected') as dispose:
            with self.assertRaises(TimeoutError):
                app.run_window(dict(report_dir=directory,strategies=[]),window,Stop(),
                    import_state=None,counter_file=Path(directory)/'counter.sqlite3')
        self.assertEqual(build.call_count,4)
        self.assertEqual(dispose.call_count,4)
        hosts[0].stop.assert_called_once()

    def test_query_timeout_during_reconnect_cannot_override_ownership_or_report_conflict(self):
        app=import_module('demos.direct_ctp')
        class Stop(Event):
            def wait(self,timeout=None):return self.is_set()
        for reason,reports in ((-1,()),(0x2001,('report conflict',))):
            with self.subTest(reason=reason,reports=reports),TemporaryDirectory() as directory:
                now=datetime.now(timezone.utc)
                window=NS(start=now,end=now+timedelta(minutes=1),trading_day='20261116')
                def host(code,errors,error):
                    return NS(account=NS(driver=NS(last_disconnect={'reason':code},is_simnow_authenticated=False)),
                        service=NS(client=NS(report_errors=errors)),start=Mock(),stop=Mock(),
                        poll=Mock(side_effect=error),snapshot=lambda:{})
                old=host(0x2001,(),RuntimeError('network disconnect'))
                conflicted=host(reason,reports,TimeoutError('query timeout after conflict'))
                with patch.object(app,'build_direct_host',side_effect=[old,conflicted]) as build, \
                        patch.object(app,'dispose_disconnected') as dispose:
                    with self.assertRaisesRegex(TimeoutError,'after conflict'):
                        app.run_window(dict(report_dir=directory,strategies=[]),window,Stop(),
                            import_state=None,counter_file=Path(directory)/'counter.sqlite3')
                self.assertEqual(build.call_count,2)
                self.assertEqual(dispose.call_count,2)
                conflicted.stop.assert_not_called()

    def test_preparation_that_crosses_stop_boundary_never_enters_poll_or_trading(self):
        app=import_module('demos.direct_ctp')
        now=datetime.now(timezone.utc)
        window=NS(start=now-timedelta(minutes=1),end=now+timedelta(seconds=1),trading_day='20261111')
        host=NS(account=NS(driver=NS(is_simnow_authenticated=True)),stop=Mock(),poll=Mock(),snapshot=lambda:{})
        host.start=lambda:host.preparation_check()
        clock=Mock();clock.now.side_effect=[now,window.end+timedelta(seconds=1)]
        with TemporaryDirectory() as directory, patch.object(app,'build_direct_host',return_value=host),patch.object(app,'datetime',clock):
            result=app.run_window(dict(report_dir=directory,strategies=[{'strategy_id':'one'}]),window,Event(),
                import_state=None,counter_file=Path(directory)/'counter.sqlite3')
            self.assertEqual(result,{'status':'completed'})
            host.poll.assert_not_called()
            host.stop.assert_called_once()

    def test_generic_close_consumes_the_owners_bucket_without_using_another_strategys_yesterday(self):
        self.instrument=type(self.instrument).from_str('p2701.DCE')
        with TemporaryDirectory() as directory:
            account,service,broker,(one,two)=self.make(Path(directory)/'state.json',generic_close_venues=('DCE',))
            self.start(account,service,one);two.start()
            self.submit(one,3);broker.fill(broker.sent[-1]['OrderRef'])
            self.submit(two,2);broker.fill(broker.sent[-1]['OrderRef'])
            state=account.ledger.state()
            position=replace(state.positions[self.instrument],long_today=D(2),long_yesterday=D(3),long_yesterday_basis=D(3000))
            account.ledger.restore(replace(state,positions={self.instrument:position}))
            account.driver.buckets.positions[('one',str(self.instrument))]=[D(0),D(3),D(0),D(0)]
            self.submit(two,0,revision=2)
            fields=broker.sent[-1]
            self.assertEqual(fields['CombOffsetFlag'],'1')
            broker.fill(fields['OrderRef'])
            self.assertEqual(account.ledger.snapshot(self.instrument).long_yesterday,3)
            self.assertEqual(account.ledger.snapshot(self.instrument).long_today,0)
            self.assertEqual(account.driver.buckets.quantities('one',self.instrument),[0,3,0,0])
            account._validate_account()
            one.stop();two.stop();account.stop()


if __name__ == '__main__':
    unittest.main()
