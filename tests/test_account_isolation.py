"""纯账户配置、状态迁移、执行登记及跨日；真实执行组件配离线柜台。"""
import builtins
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from tests import test_hybrid_shared_layout as fixtures
from bomber.framework.calendars.state import calendar_to_state, calendar_from_state
from bomber.framework.trader.persistence import JsonStateStore
from bomber.framework.trader.runtime.live.continuous import orders_for_day
from bomber.framework.trader.execution.registry import ExecutionRegistry
from bomber.framework.trader.execution.ipc import AccountExecutionProtocol, wire
from bomber.framework.trader.execution.ipc_client import RemoteAccountService
from demos.shared_account import account
from bomber.framework.trader.runtime.live import account as account_service, remote as native
from scripts.integration.shared_account import migrate_account
from bomber.framework.trader.runtime.live.account import read_account_config, account_identity
from bomber.framework.trader.runtime.live.remote import read_strategy_rows
from importlib import import_module
HYBRID_PATHS = import_module('demos.10_hybrid_transformer.run_shared').PATH_ARGUMENTS

ROOT = Path(__file__).resolve().parents[1]


class AccountConfigurationTests(unittest.TestCase):
    def test_account_json_and_offline_entry_never_read_a_strategy_json(self):
        path = ROOT/'demos/shared_account/account.json'
        opened = []
        actual = Path.read_text
        def read(path, *args, **kwargs):
            opened.append(path.resolve())
            if any(part in str(path) for part in ('/08_option_vega/', '/10_hybrid_transformer/')):
                raise AssertionError('账户不应读取策略目录')
            return actual(path,*args,**kwargs)
        with patch.object(Path,'read_text',read), redirect_stdout(StringIO()) as output:
            account.main(['--config',str(path)])
        self.assertIn('未读取任何策略配置',output.getvalue())
        self.assertEqual(opened,[path.resolve()])
        config = read_account_config(path)
        self.assertNotIn('/08_option_vega/',json.dumps(config))
        self.assertNotIn('/10_hybrid_transformer/',json.dumps(config))
        self.assertNotIn('/scripts/',json.dumps(config))
        self.assertNotIn('strategies',config)
        self.assertNotIn('strategy_configs',config)
        self.assertNotIn('registration',config)

    def test_account_rejects_strategy_references_and_migration_sources(self):
        original = json.loads((ROOT/'demos/shared_account/account.json').read_text())
        with TemporaryDirectory() as directory:
            path = Path(directory)/'account.json'
            for key in ('strategies','strategy_configs','registration','argv','model'):
                path.write_text(json.dumps({**original,key:[]}))
                with self.subTest(key=key), self.assertRaisesRegex(ValueError,'账户JSON'):
                    read_account_config(path)

    def test_hybrid_instances_have_only_strategy_rows_and_preserve_old_parameters(self):
        account = read_account_config(ROOT/'demos/shared_account/account.json')
        path = ROOT/'demos/10_hybrid_transformer/config/instances_rb_p.json'
        rows = read_strategy_rows(path,path_arguments=HYBRID_PATHS)
        from scripts.integration.shared_account.config import read_config
        previous = read_config(path.with_name('shared_rb_p.json'))['strategies']
        self.assertEqual(rows,previous)
        self.assertEqual(set(json.loads(path.read_text())),{'version','strategies'})

    def test_native_strategy_only_reads_account_and_its_own_declaration(self):
        path = ROOT/'demos/shared_account/account.json'
        settings = read_account_config(path)
        rows = read_strategy_rows(ROOT/'demos/10_hybrid_transformer/config/instances_rb_p.json',path_arguments=HYBRID_PATHS)
        original, opened = Path.read_text, []
        def read(current,*args,**kwargs):
            opened.append(current.resolve())
            return original(current,*args,**kwargs)
        with patch.object(Path,'read_text',read):
            config = native.load_account_connection(path,rows)
        self.assertEqual(set(opened),{path.resolve()})
        self.assertEqual(config['strategies'],rows)


class AccountExecutionTests(unittest.TestCase):
    setUp = fixtures.HybridSharedLayoutTests.setUp
    make_account = fixtures.HybridSharedLayoutTests.make_account
    session = fixtures.HybridSharedLayoutTests.session
    start = fixtures.HybridSharedLayoutTests.start
    bar = fixtures.HybridSharedLayoutTests.bar

    def migrated(self, *, hold=False):
        self.account,self.service,self.transport = self.make_account(preserve_external=True)
        self.account.driver._max_session_orders = 200
        rb,p = self.session('RB'),self.session('P')
        self.start(rb,p)
        if hold:
            self.bar(rb,'2026-10-13 09:01',1)
            self.transport.fill(self.transport.sent[-1]['OrderRef']); rb.client.drain_events()
        for session in (rb,p):
            session.runner.stop()
        for sid,events in self.service.event_journal.events.items():
            self.service.event_journal.acknowledge(sid,len(events))
        self.account.stop()
        host = NS(service=self.service,strategies={sid:NS(session=session)
            for sid,session in [('hybrid-rb',rb),('hybrid-p',p)]})
        registry = migrate_account.registry_from_host(host)
        settings = read_account_config(ROOT/'demos/shared_account/account.json')
        settings.update(trading_day='20261013',account_state_file=str(self.root/'public'/'state.json'),
            report_dir=str(self.root/'public'/'reports'))
        settings['account'].update(client_id='account',max_gross_quantity=4,max_gross_notional=1000000,
            limit_offset_ticks=0)
        payload = JsonStateStore(self.root/'account.json').load().payload
        payload['component_states']['session_identity']['identity'] = dict(
            trading_day='20261013',environment='realtime',account=deepcopy(settings['account']),
            strategies={'hybrid-rb':{},'hybrid-p':{}})
        isolated = migrate_account.isolate_payload(payload,settings,registry)
        JsonStateStore(settings['account_state_file']).save(isolated,expected_generation=0)
        return settings,payload,isolated,registry

    def test_migration_preserves_orders_fills_ownership_and_daily_count(self):
        settings, previous, migrated, registry = self.migrated(hold=True)
        for key in ('positions','orders','portfolio','ctp_drivers','ctp_ledgers','targets'):
            self.assertEqual(previous[key],migrated[key],key)
        self.assertEqual(orders_for_day(previous,'20261013'),orders_for_day(migrated,'20261013'))
        self.assertEqual(orders_for_day(migrated,'20261013'),1)
        self.assertNotIn('argv',json.dumps(registry))
        self.assertNotIn('strategy_id',json.dumps(registry['sessions']))
        for session in registry['sessions']:
            calendar = calendar_from_state(session)
            self.assertTrue(calendar.windows(datetime(2026,10,13).date()))
        self.assertEqual(migrated['component_states']['session_identity']['identity'],
            account_identity(settings,registry['ports']))

    def build_pure(self, settings, remaining=199):
        original = builtins.__import__
        def guarded(name,*args,**kwargs):
            if name.startswith('demos.') or name.endswith('.factories') or name == 'factories':
                raise AssertionError('账户服务不应导入策略工厂或模型')
            return original(name,*args,**kwargs)
        with patch('bomber.framework.trader.runtime.live.channels.ctp.build_simnow_transport',return_value=self.transport), \
                patch('builtins.__import__',side_effect=guarded):
            host = account_service.build_host(settings,resume=True,session_order_budget=remaining)
            self.addCleanup(host.resources.close)
            host.service.clock_ns = lambda:self.now
            host.account.start(resume=True)
            host.service.start(); host.account.authorize()
        self.addCleanup(host.account.driver.stop)
        return host

    def test_pure_account_restores_held_position_without_importing_strategy_code(self):
        settings,previous,migrated,registry = self.migrated(hold=True)
        original = (self.root/'account.json').read_bytes()
        host = self.build_pure(settings)
        self.assertEqual(host.service.position_manager.position('hybrid-rb','rb_main'),2)
        self.assertEqual(host.service.event_journal.ack,self.service.event_journal.ack)
        host.account.stop(); host.service.stop()
        self.assertEqual((self.root/'account.json').read_bytes(),original)
        self.assertEqual(orders_for_day(JsonStateStore(settings['account_state_file']).load().payload,'20261013'),1)

    def migration_fixture(self):
        settings,source,_,_ = self.migrated(hold=True)
        settings['account_state_file'] = str(self.root/'isolated-account.json')
        rows = [dict(strategy_id=sid,kind=kind,argv=[],state_file=str(self.root/(sid+'-signal.json')))
                for sid,kind in [('hybrid-rb','hybrid_transformer'),('hybrid-p','option_vega')]]
        identity = source['component_states']['session_identity']['identity']
        identity['strategies'] = {row['strategy_id']:dict(row,trading_day='20261013') for row in rows}
        self.account.manager.state_components['session_identity'].identity = deepcopy(identity)
        path = self.root/'account.json'
        store = JsonStateStore(path)
        store.save(source,expected_generation=store.load().generation)
        for row in rows:
            JsonStateStore(row['state_file']).save(dict(version=1,
                identity=identity['strategies'][row['strategy_id']],signal={'batches':row['strategy_id']}),expected_generation=0)
        self.service.stop()
        combined = {**deepcopy(settings),'account_state_file':str(path),'strategies':rows}
        config_path = self.root/'account-config.json'
        config_path.write_text(json.dumps(settings))
        args = ['--account-config',str(config_path),'--source-config','explicit-old-launch.json','--trading-day','20261013']
        return settings,combined,path,args

    def test_migration_cli_reads_old_files_only_explicitly_and_leaves_them_unchanged(self):
        settings,combined,source,args = self.migration_fixture()
        raw = source.read_bytes()
        snapshot = {Path(row['state_file']):Path(row['state_file']).read_bytes() for row in combined['strategies']}
        def build(current,**kwargs):
            self.assertTrue(kwargs['read_only'])
            self.assertNotEqual(current['account_state_file'],str(source))
            self.account.manager.repository.path = Path(current['account_state_file'])
            return NS(account=self.account,service=self.service,resources=self.account.resources,
                strategies={sid:NS(session=session) for sid,session in zip(
                    ('hybrid-rb','hybrid-p'),self.sessions[-2:])})
        with patch('scripts.integration.shared_account.run_workers.load_config',return_value=(combined,{})), \
                patch('scripts.integration.shared_account.config.load_environment'), \
                patch('scripts.integration.shared_account.run.build_host',side_effect=build) as builder, \
                redirect_stdout(StringIO()) as output:
            migrate_account.main(args)
            builder.assert_not_called()
            migrate_account.main(args+['--connect','--apply'])
        self.assertIn('"status": "migrated"',output.getvalue())
        self.assertEqual(source.read_bytes(),raw)
        self.assertTrue(all(path.read_bytes() == content for path,content in snapshot.items()))
        migrated = JsonStateStore(settings['account_state_file']).load().payload
        self.assertIn('execution_registry',migrated['component_states'])
        self.assertEqual(migrated['orders'],JsonStateStore(source).load().payload['orders'])

    def test_migration_failure_does_not_create_destination_or_change_source(self):
        settings,combined,source,args = self.migration_fixture()
        raw = source.read_bytes()
        with patch('scripts.integration.shared_account.run_workers.load_config',return_value=(combined,{})), \
                patch('scripts.integration.shared_account.config.load_environment'), \
                patch('scripts.integration.shared_account.run.build_host',side_effect=RuntimeError('fixture readonly failure')):
            with self.assertRaisesRegex(RuntimeError,'readonly failure'):
                migrate_account.main(args+['--connect','--apply'])
        self.assertEqual(source.read_bytes(),raw)
        self.assertFalse(Path(settings['account_state_file']).exists())

    def test_route_refresh_is_authenticated_preserves_other_owner_and_is_persisted(self):
        settings,_,_,registry = self.migrated()
        host = self.build_pure(settings)
        protocol = AccountExecutionProtocol(host.service,keys={'hybrid-rb':'rb','hybrid-p':'p'},
            trading_day='20261013',configure=host.configure)
        old = registry['ports']['hybrid-rb']
        configuration = deepcopy(old)
        new_instrument = 'rb2705.SHFE'
        configuration['routes']['rb_main'] = new_instrument
        configuration['limits'] = {new_instrument:next(iter(old['limits'].values()))}
        spec = {new_instrument:dict(tick='1.0',multiplier='1.00')}
        message = dict(version=1,strategy_id='hybrid-rb',key='rb',action='configure',
            data=dict(configuration=configuration,instruments=spec))
        other = host.service._ports['hybrid-p'].configuration()
        result = protocol.handle(message)
        self.assertEqual(result['state']['configuration'],configuration)
        self.assertEqual(host.service._ports['hybrid-p'].configuration(),other)
        saved = JsonStateStore(settings['account_state_file']).load().payload
        self.assertEqual(saved['component_states']['execution_registry']['ports']['hybrid-rb'],configuration)
        self.assertEqual(saved['component_states']['shared_account']['shared']['routes']['hybrid-rb'],configuration)
        protocol.handle(dict(version=1,strategy_id='hybrid-rb',key='rb',action='attach',data={'configuration':configuration}))
        with self.assertRaisesRegex(ValueError,'租约'):
            protocol.handle(message)
        protocol.stop_owner('hybrid-rb')
        host.account.stop(); host.service.stop()

    def test_route_refresh_cannot_remove_owned_position_or_raise_product_limits(self):
        settings,_,_,registry = self.migrated(hold=True)
        host = self.build_pure(settings)
        original = Path(settings['account_state_file']).read_bytes()
        port = deepcopy(registry['ports']['hybrid-rb'])
        replacement = 'rb2705.SHFE'
        port['routes']['rb_main'] = replacement
        port['limits'] = {replacement:next(iter(port['limits'].values()))}
        with self.assertRaisesRegex(ValueError,'持仓|目标'):
            host.configure('hybrid-rb',port,{replacement:registry['instruments']['rb2701.SHFE']})
        self.assertEqual(Path(settings['account_state_file']).read_bytes(),original)
        increased = deepcopy(registry['ports']['hybrid-rb'])
        increased['limits']['rb2701.SHFE']['max_abs_position'] = '3'
        with self.assertRaisesRegex(ValueError,'限额'):
            host.configure('hybrid-rb',increased,{'rb2701.SHFE':registry['instruments']['rb2701.SHFE']})
        self.assertEqual(Path(settings['account_state_file']).read_bytes(),original)
        host.account.stop(); host.service.stop()

    def test_account_cross_day_uses_its_ledger_and_preserves_owned_cost_without_strategy_imports(self):
        settings,_,_,_ = self.migrated(hold=True)
        path = Path(settings['account_state_file'])
        before = path.read_bytes()
        settings['trading_day'] = '20261014'
        connect, details = self.transport.connect, self.transport.query_position_details
        def next_day(*args):
            return replace(connect(*args),trading_day='20261014')
        def yesterday():
            return [{**row,'TodayPosition':0,'PositionDate':'2'} for row in details()]
        self.now += 86_400_000_000_000
        with patch.object(self.transport,'connect',side_effect=next_day), \
                patch.object(self.transport,'query_position_details',side_effect=yesterday):
            host = self.build_pure(settings,remaining=200)
            self.assertEqual(host.service.position_manager.position('hybrid-rb','rb_main'),2)
            self.assertEqual(host.account.ledger.state().trading_day,'20261014')
            self.assertEqual(host.account.ledger.snapshot(self.rb).long_yesterday,2)
            self.assertEqual(host.account.driver.order_budget['submitted'],0)
            host.account.stop(); host.service.stop()
        self.assertEqual(path.with_name(path.name+'.before-rollover-g1.json').read_bytes(),before)
        saved = JsonStateStore(path).load().payload
        self.assertEqual(orders_for_day(saved,'20261014'),0)
        self.assertEqual(orders_for_day(saved,'20261013'),1)

    def test_account_identity_change_is_rejected_before_connecting(self):
        settings,_,_,_ = self.migrated()
        before = Path(settings['account_state_file']).read_bytes()
        settings['account']['max_session_orders'] = 201
        with patch('bomber.framework.trader.runtime.live.channels.ctp.build_simnow_transport') as connect:
            with self.assertRaisesRegex(ValueError,'身份、额度'):
                account_service.build_host(settings,resume=True,session_order_budget=201)
            connect.assert_not_called()
        self.assertEqual(Path(settings['account_state_file']).read_bytes(),before)

    def test_remote_client_only_requests_execution_declaration_when_account_allows_it(self):
        settings,_,_,registry = self.migrated()
        configuration = deepcopy(registry['ports']['hybrid-rb'])
        i = self.rb
        from bomber.framework.trader.execution.registry import limits_from_wire
        from bomber.framework.trader.execution.shared import SharedRiskBudget
        changed = deepcopy(configuration)
        changed['routes']['rb_main'] = 'rb2705.SHFE'
        connection = Mock(sid='hybrid-rb')
        connection.call.return_value = {'state':{'configuration':configuration}}
        service = RemoteAccountService(connection,changed,{'client_id':'account','account_id':'fixture'})
        with self.assertRaisesRegex(ValueError,'不一致'):
            service.strategy_client('hybrid-rb',routes={'rb_main':i},budget=SharedRiskBudget(**configuration['budget']),
                instrument_limits={i:limits_from_wire(configuration['limits'][str(i)])})
        connection.call.assert_not_called()
        service.allow_reconfigure = True
        service.instrument_specs = {str(i):registry['instruments'][str(i)]}
        service.strategy_client('hybrid-rb',routes={'rb_main':i},budget=SharedRiskBudget(**configuration['budget']),
            instrument_limits={i:limits_from_wire(configuration['limits'][str(i)])})
        self.assertEqual(connection.call.call_args.args[0],'configure')

    def test_account_scheduler_uses_only_its_saved_calendar_and_remaining_budget(self):
        from threading import Event
        from bomber.framework.calendars.sessions import TradingWindow
        settings,_,_,registry = self.migrated(hold=True)
        config_file = self.root/'public-account-config.json'
        config_file.write_text(json.dumps(settings))
        calendar = account.load_account_calendar(registry)
        moment = datetime(2026,10,13,9,1,tzinfo=calendar.zone)
        window = calendar.window(moment)
        self.assertTrue(calendar.is_trading_day(moment.date()))
        class Continuous:
            def __init__(self,cal,factory,**kwargs): self.factory = factory
            def run(self): return self.factory(window,Event()).run()
        with patch('bomber.framework.cli.load_environment'), \
                patch('bomber.framework.trader.runtime.live.continuous.ContinuousPositionRuntime',Continuous), \
                patch.object(account,'run_window',return_value={'status':'completed'}) as run:
            account.main(['--config',str(config_file),'--connect','--enable-orders','--confirm-simnow'])
        current,service_window,event,remaining,discovery = run.call_args.args
        self.assertEqual(remaining,199)
        self.assertEqual((service_window.end-window.end).total_seconds(),30)
        self.assertEqual(discovery.resolve(),(Path(settings['account_state_file']).parent/'service/current.json').resolve())
        self.assertNotIn('strategies',current)

    def test_real_pure_account_server_refreshes_route_trades_and_restores_without_strategy_files(self):
        import threading
        import time
        from bomber.framework.trader.contracts import ExecutionRequest
        from bomber.framework.trader.execution.ipc import UnixExecutionConnection
        from bomber.framework.trader.execution.registry import limits_from_wire
        from bomber.framework.trader.execution.shared import SharedRiskBudget
        from bomber.framework.trader.runtime.live import account as server
        settings,_,_,registry = self.migrated()
        stopped,errors = threading.Event(),[]
        credentials_dir = self.root/'pure-credentials'
        args = NS(account_only=True,socket=self.root/'pure.sock',credentials_dir=credentials_dir,
            resume=True,remaining_orders=200,activate_at=None)
        original_build,original_send = account_service.build_host,self.transport.send_order
        def build(config,**kwargs):
            host = original_build(config,**kwargs)
            host.service.clock_ns = lambda:self.now
            return host
        def send(fields):
            original_send(fields)
            self.transport.fill(fields['OrderRef'])
        def control():
            try:
                server.serve_account(args,settings,stopped)
            except BaseException as error:
                errors.append(error)
        with patch.object(server,'load_environment'), \
                patch('bomber.framework.trader.runtime.live.channels.ctp.build_simnow_transport',return_value=self.transport), \
                patch.object(account_service,'build_host',side_effect=build), \
                patch.object(self.transport,'send_order',side_effect=send), \
                patch('scripts.integration.shared_account.run.build_host',side_effect=AssertionError('不得走策略工厂账户装配')):
            thread = threading.Thread(target=control); thread.start()
            try:
                path = credentials_dir/'hybrid-rb.json'
                until = time.monotonic()+5
                while not path.exists():
                    if errors: raise errors[0]
                    if time.monotonic() >= until: self.fail('账户未发布执行凭据')
                    time.sleep(.01)
                credentials = json.loads(path.read_text())
                self.assertTrue(credentials['allow_reconfigure'])
                self.assertFalse(credentials['check_reference'])
                connection = UnixExecutionConnection(args.socket,'hybrid-rb',credentials['key'],timeout=5)
                state = connection.call('describe',{})['state']
                remote = RemoteAccountService(connection,credentials['configuration'],state)
                remote.allow_reconfigure = True
                i = fixtures.hybrid_fixture.InstrumentId.from_str('rb2705.SHFE')
                remote.instrument_specs = {str(i):registry['instruments']['rb2701.SHFE']}
                row = registry['ports']['hybrid-rb']
                port = remote.strategy_client('hybrid-rb',routes={'rb_main':i},budget=SharedRiskBudget(**row['budget']),
                    instrument_limits={i:limits_from_wire(row['limits']['rb2701.SHFE'])})
                port.ready_check = lambda:True
                port.start(); port.drain_events()
                remote.prices.update(i,100,self.now)
                port.submit_targets(ExecutionRequest('hybrid-rb',1,'account',self.now,{i:Decimal(2)},'DIRECT',
                    logical_targets={'rb_main':Decimal(2)}, metadata={'position_attribution':[
                        dict(strategy_id='hybrid-rb',target_key='rb_main',instrument_id=str(i),quantity='2')]}))
                port.drain_events()
                self.assertEqual(port.position_manager.position('hybrid-rb','rb_main'),2)
                port.stop()
            finally:
                stopped.set(); thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors,errors)
        saved = JsonStateStore(settings['account_state_file']).load().payload
        self.assertEqual(saved['component_states']['execution_registry']['ports']['hybrid-rb']['routes'],
            {'rb_main':'rb2705.SHFE'})
        self.assertEqual(orders_for_day(saved,'20261013'),1)
        restored = self.build_pure(settings)
        self.assertEqual(restored.service.position_manager.position('hybrid-rb','rb_main'),2)
        restored.account.stop(); restored.service.stop()


class AccountJoinMigrationTests(unittest.TestCase):
    def test_signal_mismatch_fails_file_check_and_apply_before_connecting_without_changing_files(self):
        from tests import test_option_workers as worker_fixtures
        f = worker_fixtures.WorkerRegistrationTests('runTest')
        f.setUp(); self.addCleanup(f.doCleanups)
        root = f.f.root
        settings = read_account_config(ROOT/'demos/shared_account/account.json')
        settings.update(trading_day='20261013', account_state_file=str(root/'public-account.json'))
        settings['account'] = deepcopy(f.current['account'])
        original = deepcopy(f.base_config)
        for row in original['strategies']:
            row['state_file'] = str(root/(row['strategy_id']+'-signal.json'))
            JsonStateStore(row['state_file']).save(dict(version=1, identity=f.previous[row['strategy_id']],
                signal={'preserved_batch':row['strategy_id']}), expected_generation=0)
        rows = [*original['strategies'], dict(strategy_id=f.sid, kind='option_vega', argv=[],
            state_file=str(root/'option-worker-signal.json'))]
        signal_path = root/'option-signal.json'
        signal = deepcopy(f.signal)
        signal['revision'] = 2
        JsonStateStore(signal_path).save(signal, expected_generation=0)
        combined = {**settings, 'account_state_file':str(root/'base.json'), 'strategies':rows}
        paths = [root/'base.json', root/'option.json', signal_path,
            *(Path(row['state_file']) for row in rows[:-1])]
        before = {path:path.read_bytes() for path in paths}
        config_path = root/'account-config.json'; config_path.write_text(json.dumps(settings))
        args = ['--account-config',str(config_path), '--source-config','explicit-legacy.json',
            '--trading-day','20261013', '--option-checkpoint',str(root/'option.json'),
            '--option-signal',str(signal_path)]
        with patch('scripts.integration.shared_account.run_workers.load_config',
                return_value=(combined, {'base_config':'explicit-original.json'})), \
                patch('scripts.integration.shared_account.config.read_config', return_value=original), \
                patch('scripts.integration.shared_account.run.build_host') as builder:
            for flags in ([], ['--connect','--apply']):
                with self.subTest(flags=flags), self.assertRaisesRegex(ValueError,
                        '账户revision=1, 信号revision=2') as caught:
                    migrate_account.main(args+flags)
                self.assertIn(str(signal_path), str(caught.exception))
            builder.assert_not_called()
        self.assertTrue(all(path.read_bytes() == content for path,content in before.items()))
        self.assertFalse(Path(settings['account_state_file']).exists())

    def test_first_migration_merges_existing_hybrid_and_option_ownership_without_touching_sources(self):
        from tests import test_option_workers as worker_fixtures
        f = worker_fixtures.WorkerRegistrationTests('runTest')
        f.setUp(); self.addCleanup(f.doCleanups)
        root = f.f.root
        settings = read_account_config(ROOT/'demos/shared_account/account.json')
        settings.update(trading_day='20261013',account_state_file=str(root/'public-account.json'))
        settings['account'] = deepcopy(f.current['account'])
        f.current['environment'] = 'realtime'
        original = deepcopy(f.base_config)
        rows = []
        for row in original['strategies']:
            row['state_file'] = str(root/(row['strategy_id']+'-signal.json'))
            JsonStateStore(row['state_file']).save(dict(version=1,identity=f.previous[row['strategy_id']],
                signal={'preserved_batch':row['strategy_id']}),expected_generation=0)
            rows.append(deepcopy(row))
        rows.append(dict(strategy_id=f.sid,kind='option_vega',argv=[],state_file=str(root/'option-worker-signal.json')))
        base_path, option_path, signal_path = root/'base.json',root/'option.json',root/'option-signal.json'
        base_store = JsonStateStore(base_path)
        base_saved = base_store.load()
        base_saved.payload['component_states']['session_identity']['identity']['environment'] = 'realtime'
        base_store.save(base_saved.payload,expected_generation=base_saved.generation)
        JsonStateStore(signal_path).save(f.signal,expected_generation=0)
        combined = {**settings,'account_state_file':str(base_path),'strategies':rows}
        source_files = {path:path.read_bytes() for path in [base_path,option_path,signal_path,
            *(Path(row['state_file']) for row in rows[:-1])]}
        account_file = root/'account-config.json'; account_file.write_text(json.dumps(settings))
        def build(config,**kwargs):
            self.assertTrue(kwargs['read_only'])
            f.new_account.manager.repository.path = Path(config['account_state_file'])
            fake_session = NS(references=NS(calendar=f.f.cal))
            return NS(account=f.new_account,service=f.new_service,resources=f.new_account.resources,
                strategies={sid:NS(session=fake_session) for sid in f.current['strategies']})
        args = ['--account-config',str(account_file),'--source-config','explicit-legacy.json',
            '--trading-day','20261013','--option-checkpoint',str(option_path),'--option-signal',str(signal_path),
            '--connect','--apply']
        with patch('scripts.integration.shared_account.run_workers.load_config',return_value=(combined,{'base_config':'explicit-original.json'})), \
                patch('scripts.integration.shared_account.config.read_config',return_value=original), \
                patch('scripts.integration.shared_account.config.load_environment'), \
                patch('scripts.integration.shared_account.run.build_host',side_effect=build), redirect_stdout(StringIO()):
            migrate_account.main(args)
        self.assertTrue(all(path.read_bytes() == content for path,content in source_files.items()))
        payload = JsonStateStore(settings['account_state_file']).load().payload
        positions = {(row['strategy_id'],row['target_key']):row['quantity'] for row in payload['positions']['strategy_positions']}
        self.assertEqual(positions[('hybrid-rb','rb_main')],'2')
        self.assertEqual(positions[(f.sid,str(f.f.p))],'-1')
        self.assertEqual(orders_for_day(payload,'20261013'),2)
        self.assertEqual(set(payload['component_states']['execution_registry']['ports']),set(f.current['strategies']))
        self.assertFalse(f.new_transport.sent)


if __name__ == '__main__':
    unittest.main()
