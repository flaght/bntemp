"""08/Hybrid worker编排和禁单归属注册；真实执行账本，柜台与时钟为离线替身。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace as NS
from unittest.mock import patch
from zoneinfo import ZoneInfo
import json
import unittest

from tests import test_hybrid_shared_layout as fixture
from bomber.framework.calendars.sessions import TradingWindow
from bomber.framework.trader.contracts import ExecutionRequest
from bomber.framework.trader.execution.shared import SharedRiskBudget
from bomber.framework.trader.execution.ipc import ExecutionEventJournal
from scripts.integration.shared_account import run_workers, register_worker
from scripts.integration.shared_account.continuous import ProcessWindowRuntime

ZONE = ZoneInfo('Asia/Shanghai')


def dt(text):
    return datetime.fromisoformat('2026-10-13T'+text).replace(tzinfo=ZONE)


class WorkerLaunchTests(unittest.TestCase):
    def test_declaration_preserves_hybrid_paths_parameters_and_daily_td_limit(self):
        root = Path(__file__).resolve().parents[1]
        path = root/'demos/08_option_vega/config/workers.json'
        config, sources = run_workers.load_config(path)
        base = run_workers.read_config(sources['base_config'])
        self.assertEqual(config['strategies'][:-1], base['strategies'])
        self.assertEqual(config['account_state_file'], base['account_state_file'])
        self.assertEqual(config['account']['max_session_orders'], base['account']['max_session_orders'])
        self.assertIsNone(config['account']['max_gross_quantity'])
        self.assertIsNone(config['account']['max_gross_notional'])
        from scripts.integration.shared_account.factories import strategy_arguments
        row = config['strategies'][-1]
        _, args = strategy_arguments(row, config, resume=False)
        self.assertFalse(args.run_forever)
        self.assertEqual(args.option_product, 'MO')
        self.assertEqual(args.config.max_option_lots, 1)
        self.assertEqual(args.config.max_total_option_lots, 2)
        self.assertEqual(args.config.max_future_lots, 1)

    def calendars(self):
        def calendar(windows):
            return NS(zone=ZONE, windows=lambda day: windows, is_trading_day=lambda _: True,
                window=lambda now: next((w for w in windows if w.start <= now < w.end), None),
                next_window=lambda now, before: next((w for w in windows if now < w.start <= before), None))
        rb = [TradingWindow('20261013', dt(a), dt(b), str(i)) for i, (a,b) in enumerate(
            [('09:00','10:15'), ('10:30','11:30'), ('13:30','15:00')])]
        mo = [TradingWindow('20261013', dt(a), dt(b), str(i)) for i,(a,b) in enumerate(
            [('09:30','11:30'), ('13:00','14:57')])]
        return run_workers.WorkerCalendars({'rb': calendar(rb), 'vega': calendar(mo)})

    def test_each_calendar_keeps_its_own_lunch_auction_and_preclose_boundaries(self):
        calendars = self.calendars()
        windows = calendars.windows('20261013')
        self.assertEqual([(w.start, w.end) for w in windows], [(dt('09:00'),dt('11:30')), (dt('13:00'),dt('15:00'))])
        self.assertEqual(calendars.window(dt('10:20')), windows[0])
        config = {'strategies': [{'strategy_id':'rb','kind':'hybrid_transformer'},
                                 {'strategy_id':'vega','kind':'option_vega'}]}
        plan = calendars.worker_windows(config, windows[0])
        self.assertEqual(plan['rb'][0].end, dt('10:14:30'))
        self.assertEqual(plan['vega'][0].end, dt('11:30'))
        self.assertEqual(calendars.worker_windows(config, windows[1])['vega'][0].end, dt('14:57'))

    def test_morning_processes_prepare_and_stop_independently_with_one_server(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = dict(environment='realtime', execution_mode='independent', trading_day='20261013', seconds=86400,
                report_dir=str(root), strategies=[dict(strategy_id=sid, kind=kind, state_file=str(root/(sid+'.json')), argv=[])
                    for sid,kind in [('rb','hybrid_transformer'), ('vega','option_vega')]])
            calendars = self.calendars(); window = calendars.windows('20261013')[0]
            now = [dt('08:50')]; commands, stopped = [], []
            class Stop:
                def is_set(self): return False
                def wait(self, seconds): now[0] += timedelta(minutes=1)
            def popen(command, **kwargs):
                commands.append((now[0], command))
                p=NS(returncode=None); p.poll=lambda:p.returncode
                def terminate():
                    p.returncode=0
                    stopped.append((now[0], command))
                    if '--strategy-id' in command:
                        sid=command[command.index('--strategy-id')+1]
                        (root/(sid+'.json')).write_text('{}')
                p.terminate=terminate
                if command[3].endswith('.server'):
                    credentials=Path(command[command.index('--credentials-dir')+1]);credentials.mkdir()
                    for row in config['strategies']:(credentials/(row['strategy_id']+'.json')).write_text('{}')
                return p
            runtime=ProcessWindowRuntime(config,window,Stop(),198,popen=popen,clock=lambda:now[0],
                worker_windows=calendars.worker_windows(config,window),prepare_before_open=True,prepare_seconds=600)
            runtime.run()
            self.assertEqual(len(commands),4)  # one TD, RB twice, MO once
            self.assertEqual([stamp for stamp,cmd in commands], [dt('08:50'),dt('08:50'),dt('09:20'),dt('10:20')])
            mo=commands[2][1]
            self.assertEqual(mo[3],'demos.08_option_vega.run_worker')
            self.assertEqual(mo[mo.index('--stop-at')+1],dt('11:30').isoformat())
            self.assertNotIn('--resume',commands[1][1])
            self.assertIn('--resume',commands[3][1])
            self.assertTrue(stopped[-1][1][3].endswith('.server'))

    def test_account_role_publishes_discovery_without_starting_any_worker(self):
        with TemporaryDirectory() as directory:
            root=Path(directory); now=[dt('09:00')]; commands=[]
            window=TradingWindow('20261013',now[0],dt('09:01'),'morning')
            config=dict(execution_mode='independent',report_dir=str(root),strategies=[dict(strategy_id='vega')])
            discovery=root/'runtime/current.json'
            class Stop:
                def is_set(self):return False
                def wait(self, seconds):
                    self_test.assertTrue(discovery.is_file())
                    published=json.loads(discovery.read_text())
                    self_test.assertEqual(published['trading_day'],'20261013')
                    self_test.assertNotIn('key',published)
                    now[0]=window.end
            self_test=self
            def popen(command,**kwargs):
                commands.append(command)
                credentials=Path(command[command.index('--credentials-dir')+1]);credentials.mkdir()
                (credentials/'vega.json').write_text('{}')
                process=NS(returncode=None)
                process.poll=lambda:process.returncode
                process.terminate=lambda:setattr(process,'returncode',0)
                return process
            ProcessWindowRuntime(config,window,Stop(),200,popen=popen,clock=lambda:now[0],
                worker_windows={},discovery_file=discovery).run()
            self.assertEqual(len(commands),1)
            self.assertTrue(commands[0][3].endswith('.server'))
            self.assertFalse(discovery.exists())

    def test_separate_terminal_never_creates_a_td_service_and_checks_published_config(self):
        with TemporaryDirectory() as directory:
            root=Path(directory); published=root/'config.json'; socket=root/'exec.sock';socket.touch()
            config=dict(version=1,trading_day='20261013',seconds=86400)
            published.write_text('{}');discovery=root/'current.json'
            window=TradingWindow('20261013',dt('09:00'),dt('11:30'),'morning')
            discovery.write_text(json.dumps(dict(config=str(published),socket=str(socket),
                credentials_dir=str(root/'credentials'),trading_day='20261013',
                start=window.start.isoformat(),end=window.end.isoformat())))
            with patch.object(run_workers,'datetime') as clock, patch.object(run_workers,'read_config',return_value=config), \
                    patch.object(ProcessWindowRuntime,'run_scheduled_workers') as workers, \
                    patch.object(ProcessWindowRuntime,'run',side_effect=AssertionError('不得创建TD')):
                clock.now.return_value=dt('09:30');clock.fromisoformat=datetime.fromisoformat
                result=run_workers.run_remote_window(config,window,Event(),{'vega':()},discovery,600)
                self.assertEqual(result['status'],'completed')
                workers.assert_called_once_with(published,root/'credentials')
                with self.assertRaisesRegex(ValueError,'启动声明'):
                    run_workers.run_remote_window(dict(config,seconds=100,version=2),window,Event(),{},discovery,600)

    def test_duplicate_worker_launcher_selection_is_checked_without_connecting(self):
        root=Path(__file__).resolve().parents[1]
        from contextlib import redirect_stdout,redirect_stderr
        from io import StringIO
        args=['--config',str(root/'demos/08_option_vega/config/workers.json')]
        with redirect_stdout(StringIO()),patch.object(run_workers,'load_calendars',side_effect=AssertionError('不得查询')):
            run_workers.main(args+['--role','worker','--kind','option_vega'])
        with redirect_stderr(StringIO()),self.assertRaises(SystemExit):
            run_workers.main(args+['--role','account','--kind','option_vega'])


class WorkerRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.HybridSharedLayoutTests('runTest');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.sid='option-vega-live'
        self.base_account,self.base_service,self.base_transport=self.f.make_account(preserve_external=True)
        self.base_account.manager.repository.path=self.f.root/'base.json'
        self.base_ports=self.ports(self.base_service)
        self.base_config={'account':self.budget(), 'strategies':[dict(strategy_id=sid,kind='hybrid_transformer',argv=[])
            for sid in ('hybrid-rb','hybrid-p')]}
        self.previous={sid:dict(row,trading_day='20261013') for sid,row in
            ((row['strategy_id'],row) for row in self.base_config['strategies'])}
        self.old_identity=dict(account=self.budget(),strategies=self.previous,trading_day='20261013',
            managed_instruments=sorted(self.base_account.position_scope.instruments))
        self.base_account.manager.state_components['session_identity'].identity=deepcopy(self.old_identity)
        self.start(self.base_account,self.base_service,self.base_ports)
        self.submit(self.base_ports[0], self.f.rb, 'rb_main', 2)
        self.base_transport.fill(self.base_transport.sent[-1]['OrderRef'])
        self.base_ports[0].drain_events()
        for sid, rows in self.base_service.event_journal.events.items():
            self.base_service.event_journal.acknowledge(sid,len(rows))
        self.base=deepcopy(self.base_account.manager.save().payload)
        self.option_account,self.option_service,self.option_transport=self.f.make_account(preserve_external=True)
        self.option_account.manager.repository.path=self.f.root/'option.json'
        self.option_service.event_journal=ExecutionEventJournal((self.sid,))
        self.option_port=self.option_service.strategy_client(self.sid, routes={str(self.f.p):self.f.p}, budget=SharedRiskBudget(2,100000))
        self.reference={'td_day':'20261013'}
        self.option_identity=dict(strategy_id=self.sid,kind='option_vega',trading_day='20261013',reference=self.reference,argv=[])
        self.option_account.manager.state_components['session_identity'].identity=dict(
            strategies={self.sid:self.option_identity},trading_day='20261013')
        self.option_account.driver._next_ref=100
        self.start(self.option_account,self.option_service,[self.option_port])
        self.submit(self.option_port,self.f.p,str(self.f.p),-1)
        self.option_transport.fill(self.option_transport.sent[-1]['OrderRef']);self.option_port.drain_events()
        self.option_service.event_journal.acknowledge(self.sid,len(self.option_service.event_journal.events[self.sid]))
        self.option=deepcopy(self.option_account.manager.save().payload)
        self.signal=dict(identity=deepcopy(self.option_identity),revision=1,signal={'goals':[-1]},
            cursor=self.option_service.event_journal.ack[self.sid])
        self.new_account,self.new_service,self.new_transport=self.f.make_account(preserve_external=True)
        self.new_account.driver._max_session_orders=200
        self.new_account.manager.repository.path=self.f.root/'registered.json'
        self.ports(self.new_service)
        self.new_service.strategy_client(self.sid,routes={str(self.f.p):self.f.p},budget=SharedRiskBudget(2,100000))
        self.new_service.budget=SharedRiskBudget(None,None)
        self.new_service.event_journal=ExecutionEventJournal(('hybrid-rb','hybrid-p',self.sid))
        self.current=deepcopy(self.old_identity)
        self.current['account'].update(max_gross_quantity=None,max_gross_notional=None)
        self.current['strategies'][self.sid]=deepcopy(self.option_identity)
        self.new_account.manager.state_components['session_identity'].identity=self.current
        self.new_account.option_sessions={self.sid:NS(restore_signal=lambda signal:None)}
        self.new_transport.gross={str(self.f.rb):(2,0),str(self.f.p):(0,1)}
        self.addCleanup(self.new_account.driver.stop)

    def budget(self):
        return dict(client_id='account',max_gross_quantity=4,max_gross_notional=1000000,max_session_orders=200,limit_offset_ticks=0)

    def ports(self, service):
        return [service.strategy_client(sid,routes={key:i},budget=SharedRiskBudget(2,100000))
            for sid,key,i in [('hybrid-rb','rb_main',self.f.rb),('hybrid-p','p_main',self.f.p)]]

    def start(self, account, service, ports):
        account.start(resume=False);service.start();account.authorize()
        for port in ports:port.start();port.ready_check=lambda:True
        self.addCleanup(account.driver.stop)

    def submit(self, port, instrument, key, quantity):
        port.service.prices.update(instrument,Decimal(100),self.f.now)
        port.submit_targets(ExecutionRequest(port.strategy_id,1,port.client_id,self.f.now,
            {instrument:Decimal(quantity)},'DIRECT',logical_targets={key:Decimal(quantity)},
            metadata={'position_attribution': [dict(strategy_id=port.strategy_id, target_key=key,
                instrument_id=str(instrument), quantity=str(quantity))]}))

    def transform(self, payload):
        return register_worker.registration_payload(self.new_account,payload,self.option,self.signal,self.base_config,strategy_limits_only=True)

    def test_real_account_restore_keeps_rb_quantity_option_short_costs_and_shared_budget(self):
        from bomber.framework.trader.persistence import JsonStateStore
        original=deepcopy((self.base,self.option,self.signal))
        JsonStateStore(self.new_account.manager.repository.path).save(self.base,expected_generation=0)
        self.new_account.start(resume=True,payload_transform=self.transform)
        self.assertEqual(self.new_service.position_manager.position('hybrid-rb','rb_main'),2)
        self.assertEqual(self.new_service.position_manager.position(self.sid,str(self.f.p)),-1)
        self.assertEqual(self.new_account.ledger.snapshot(self.f.p).short_total,1)
        self.assertEqual(self.new_account.driver.order_budget['submitted'],2)
        self.assertFalse(self.new_transport.sent)
        self.assertEqual(original,(self.base,self.option,self.signal))
        self.assertEqual(len(self.new_account.client.order_state_machine.states()),2)
        self.assertTrue(list(self.f.root.glob('registered.json.before-rollover-*.json')))

    def test_original_option_execution_client_can_differ_without_changing_order_ids(self):
        from bomber.framework.trader.persistence import JsonStateStore
        original_ids={row['client_order_id'] for row in self.option['orders']['account']}
        self.option=register_worker.remap_client(self.option,'account','legacy-option')
        for name in ('orders','ctp_ledgers','ctp_drivers'):
            self.option[name]['legacy-option']=self.option[name].pop('account')
        self.option['ctp_drivers']['legacy-option']['driver_id']='legacy-option'
        JsonStateStore(self.new_account.manager.repository.path).save(self.base,expected_generation=0)
        self.new_account.start(resume=True,payload_transform=self.transform)
        orders=self.new_account.client.order_state_machine.states()
        self.assertTrue(original_ids <= {row.client_order_id for row in orders})
        self.assertEqual({row.backend_id for row in orders},{'account'})
        self.assertEqual(self.new_service.position_manager.position(self.sid,str(self.f.p)),-1)
        self.assertFalse(self.new_transport.sent)

    def test_older_option_ledger_rolls_own_costs_into_yesterday_without_using_broker_average(self):
        source=deepcopy(self.option)
        source['ctp_ledgers']['account']['trading_day']='20261012'
        result=deepcopy(self.base)
        register_worker.merge_positions(result,source,'account','account','20261013','20261012')
        row=next(r for r in result['ctp_ledgers']['account']['positions'] if r['instrument_id']==str(self.f.p))
        self.assertEqual((row['short_today'],row['short_yesterday']),('0','1'))
        self.assertEqual(Decimal(row['short_yesterday_basis']),Decimal(100))

    def test_missing_physical_option_position_never_commits_registration(self):
        from bomber.framework.trader.persistence import JsonStateStore
        store=JsonStateStore(self.new_account.manager.repository.path);store.save(self.base,expected_generation=0)
        raw=store.path.read_bytes();self.new_transport.gross={str(self.f.rb):(2,0)}
        with self.assertRaisesRegex(RuntimeError,'柜台仓位不足'):
            self.new_account.start(resume=True,payload_transform=self.transform)
        self.assertEqual(store.path.read_bytes(),raw)
        self.assertFalse(self.new_transport.sent)

    def test_changed_hybrid_lots_or_shared_td_limit_is_rejected(self):
        self.current['strategies']['hybrid-rb']['argv']=['--max-contracts','5']
        with self.assertRaisesRegex(ValueError,'Hybrid模型'):
            self.transform(deepcopy(self.base))
        self.current['strategies']['hybrid-rb']['argv']=[]
        self.current['account']['max_session_orders']=400
        with self.assertRaisesRegex(ValueError,'每日额度'):
            self.transform(deepcopy(self.base))

    def test_source_budget_difference_reports_checkpoint_and_configuration_without_modification(self):
        config=deepcopy(self.base_config)
        config['account']['max_session_orders']=100
        original=deepcopy((self.base,config))
        with self.assertRaisesRegex(ValueError,'max_session_orders') as caught:
            register_worker.validate_source_configuration(self.base,config)
        self.assertIn('"checkpoint": 200',str(caught.exception))
        self.assertIn('"config": 100',str(caught.exception))
        self.assertEqual((self.base,config),original)

    def test_offline_and_apply_both_reject_source_mismatch_before_building_host(self):
        original=deepcopy(self.base_config)
        original['account']['max_session_orders']=100
        for row in original['strategies']:
            row['state_file']=str(self.f.root/(row['strategy_id']+'.json'))
        config=dict(account_state_file=str(self.base_account.manager.repository.path),
            account=deepcopy(original['account']),strategies=deepcopy(original['strategies'])+[
                dict(strategy_id=self.sid,kind='option_vega',state_file=str(self.f.root/'worker.json'))])
        sources=dict(base_config='original.json',option_checkpoint=None,option_signal=None,strategy_limits_only=True)
        argv=['--config','workers.json','--trading-day','20261013','--option-checkpoint',str(self.f.root/'option.json')]
        for flags in ([],['--connect','--apply']):
            with self.subTest(flags=flags), patch.object(register_worker,'load_config',return_value=(config,sources)), \
                    patch.object(register_worker,'read_config',return_value=original), \
                    patch('scripts.integration.shared_account.run.build_host') as build:
                with self.assertRaisesRegex(ValueError,'max_session_orders'):
                    register_worker.main(argv+flags)
                build.assert_not_called()

    def test_unacknowledged_fills_and_different_ctp_account_are_rejected(self):
        broken=deepcopy(self.base)
        broken['component_states']['shared_account']['shared']['event_journal']['ack']['hybrid-rb']=0
        with self.assertRaisesRegex(ValueError,'未决订单'):
            self.transform(broken)
        self.option['ctp_drivers']['account']['investor_id']='other'
        with self.assertRaisesRegex(ValueError,'同一个CTP账户'):
            self.transform(deepcopy(self.base))

    def test_execution_id_remap_preserves_strategy_and_original_order_id(self):
        row={'client_id':'old','backend_id':'old','strategy_id':'old','client_order_id':'old-20261013-1'}
        mapped=register_worker.remap_client(row,'old','new')
        self.assertEqual(mapped,dict(client_id='new',backend_id='new',strategy_id='old',client_order_id='old-20261013-1'))
        self.assertEqual(row['client_id'],'old')

    def test_same_contract_opposing_ownership_does_not_net_away_positions_or_costs(self):
        base=deepcopy(self.base);source=deepcopy(self.option)
        # 同合约多2和空1必须保留两个方向的CTP份额，逻辑归属也各自保留。
        for row in source['ctp_ledgers']['account']['positions']:
            if row['instrument_id']==str(self.f.p):row['instrument_id']=str(self.f.rb)
        register_worker.merge_positions(base,source,'account','account','20261013','20261013')
        row=next(r for r in base['ctp_ledgers']['account']['positions'] if r['instrument_id']==str(self.f.rb))
        self.assertEqual((Decimal(row['long_today']),Decimal(row['short_today'])),(2,1))
        self.assertEqual(Decimal(row['long_today_basis']),Decimal(100))
        self.assertEqual(Decimal(row['short_today_basis']),Decimal(100))


if __name__ == '__main__':
    unittest.main()
