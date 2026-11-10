"""原生入口进程边界及远程信号恢复；行情、模型和TD使用离线替身。"""
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from importlib import import_module
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, RLock
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from tests import run_hybrid_live_offline as fixture
from bomber.framework.calendars.sessions import TradingWindow
from bomber.framework.cli import load_config as load_setup
from bomber.framework.trader.persistence import JsonStateStore
from bomber.framework.trader.runtime.live.remote import RemoteStrategyRuntime, run_remote_group, wait_for_account
from bomber.framework.trader.runtime.live import remote as native
from bomber.framework.trader.runtime.live.account import read_account_config
from demos.shared_account import account

ROOT = Path(__file__).resolve().parents[1]
vega = import_module('demos.08_option_vega.run_live')
hybrid = import_module('demos.10_hybrid_transformer.run_shared')
ZONE = ZoneInfo('Asia/Shanghai')


def current_configuration():
    from bomber.framework.trader.runtime.live.remote import read_strategy_rows
    settings = read_account_config(ROOT/'demos/shared_account/account.json')
    rows = read_strategy_rows(ROOT/'demos/10_hybrid_transformer/config/instances_rb_p.json',
        path_arguments=hybrid.PATH_ARGUMENTS)
    return {**settings,'strategies':rows}


class NativeEntryTests(unittest.TestCase):
    def test_both_native_entries_validate_their_own_config_without_starting_processes_or_td(self):
        for entry, path, config_id, count in (
                (vega, 'demos/08_option_vega/config/strategy.yaml', 'workers_option', '一个'),
                (hybrid, 'demos/10_hybrid_transformer/config/strategy.yaml', 'workers_hybrid', '2个')):
            with self.subTest(config_id=config_id), patch('subprocess.Popen') as popen, \
                    patch('bomber.framework.cli.load_environment') as env, \
                    redirect_stdout(StringIO()) as output:
                setup = load_setup(ROOT/path, config_id)
                setup.update(connect=False, enable_orders=False, confirm_simnow=False)
                with TemporaryDirectory() as directory:
                    import yaml
                    launch = Path(directory)/'strategy.yaml'
                    if entry is vega:
                        setup['live_config_file'] = str(ROOT/path)
                    launch.write_text(yaml.safe_dump({'experiment_setups': {'offline': setup}}))
                    entry.main(['--config-file', str(launch), '--config-id', 'offline'])
                self.assertIn(count, output.getvalue())
                popen.assert_not_called()
                env.assert_not_called()

    def test_vega_native_entry_runs_one_instance_in_current_process(self):
        now = datetime.now(ZONE)
        window = TradingWindow(now.strftime('%Y%m%d'), now-timedelta(minutes=1), now+timedelta(minutes=3), 'test')
        seen, stopped = [], Event()
        class Continuous:
            def __init__(self, calendar, factory, **kwargs): self.factory = factory
            def run(self): return self.factory(window, stopped).run()
        def make(current, row, credentials, **kwargs):
            self.assertEqual(kwargs['stop_at'], window.end)
            self.assertEqual(row['strategy_id'], 'option-vega-live')
            return NS(strategy_id=row['strategy_id'], run=lambda stop: seen.append(os.getpid()))
        with patch('bomber.framework.cli.load_environment'), \
                patch('bomber.framework.dataprep.session_inputs.load_session_inputs', return_value=NS()), \
                patch.object(vega, 'validate_calendar'), \
                patch.object(native, 'window_credentials', return_value={'option-vega-live': Path('/tmp/fixture.json')}), \
                patch.object(native, 'resume_available', return_value=True), \
                patch.object(vega, 'build_shared_runtime', side_effect=make), \
                patch('bomber.framework.trader.runtime.live.continuous.ContinuousPositionRuntime', Continuous), \
                patch('subprocess.Popen', side_effect=AssertionError('不得创建子进程')):
            result = vega.main(['--config-file','demos/08_option_vega/config/strategy.yaml',
                '--config-id','workers_option'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(seen, [os.getpid()])

    def test_hybrid_native_entry_runs_rb_and_p_concurrently_in_current_process(self):
        config = current_configuration()
        now = datetime.now(ZONE)
        window = TradingWindow(now.strftime('%Y%m%d'), now-timedelta(minutes=1), now+timedelta(minutes=3), 'test')
        barrier, seen, stopped = Barrier(2), [], Event()
        class Runtime:
            def __init__(self, sid): self.strategy_id = sid
            def run(self, stop):
                seen.append((self.strategy_id, os.getpid()))
                barrier.wait(timeout=5)
        class Continuous:
            def __init__(self, calendar, factory, **kwargs): self.factory = factory
            def run(self): return self.factory(window, stopped).run()
        def make(current, row, credentials, **kwargs):
            self.assertEqual(kwargs['stop_at'], window.end-timedelta(seconds=30))
            return Runtime(row['strategy_id'])
        paths = {row['strategy_id']: Path('/tmp')/(row['strategy_id']+'.json') for row in config['strategies']}
        with patch('bomber.framework.cli.load_environment'), \
                patch.object(hybrid, 'load_calendar', return_value=NS()), \
                patch.object(native, 'window_credentials', return_value=paths), \
                patch.object(native, 'resume_available', return_value=True), \
                patch.object(hybrid, 'build_shared_runtime', side_effect=make), \
                patch('bomber.framework.trader.runtime.live.continuous.ContinuousPositionRuntime', Continuous), \
                patch('subprocess.Popen', side_effect=AssertionError('不得创建子进程')):
            result = hybrid.main(['--config-file','demos/10_hybrid_transformer/config/strategy.yaml',
                '--config-id','workers_hybrid'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(set(seen), {('hybrid-transformer-live', os.getpid()), ('hybrid-p-live', os.getpid())})

    def test_legacy_account_json_and_duplicate_instances_are_rejected(self):
        config = current_configuration()
        row = config['strategies'][0]
        with self.assertRaisesRegex(ValueError,'账户JSON'):
            native.load_account_connection(ROOT/'scripts/integration/shared_account/legacy_workers.json',[row])
        with self.assertRaisesRegex(ValueError,'重复'):
            native.load_account_connection(ROOT/'demos/shared_account/account.json',[row,row])

    def test_hybrid_entry_rejects_removed_account_host_layout(self):
        from contextlib import redirect_stderr
        with redirect_stderr(StringIO()) as output, self.assertRaises(SystemExit):
            hybrid.main(['--config-file','demos/10_hybrid_transformer/config/strategy.yaml',
                '--config-id','workers_hybrid','--process-layout','same-process'])
        self.assertIn('unrecognized arguments',output.getvalue())

    def test_account_entry_runs_server_in_current_process_and_removes_its_discovery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(ZONE)
            window = TradingWindow(now.strftime('%Y%m%d'), now, now+timedelta(minutes=5), 'test')
            config = dict(report_dir=str(root), trading_day=window.trading_day,account_env_file='fixture.env')
            discovery = root/'current.json'
            def serve(args, current, stop):
                self.assertEqual(os.getpid(), pid)
                self.assertEqual(args.stop_at, window.end)
                self.assertEqual(args.remaining_orders, 197)
                discovery.write_text(json.dumps({'config': str(args.discovery_config)}))
            pid = os.getpid()
            with patch('bomber.framework.trader.runtime.live.account.serve_account', side_effect=serve) as server, \
                    patch('subprocess.Popen', side_effect=AssertionError('不得创建子进程')):
                result = account.run_window(config, window, Event(), 197, discovery)
            server.assert_called_once()
            self.assertFalse(discovery.exists())
            self.assertEqual(result['status'], 'completed')

    def test_vega_entry_preserves_identity_and_declares_only_real_execution_contracts(self):
        config = read_account_config(ROOT/'demos/shared_account/account.json')
        with TemporaryDirectory() as directory:
            row = dict(strategy_id='option-vega-live',kind='option_vega',state_file=str(Path(directory)/'signal.json'),
                argv=['--config-file',str(ROOT/'demos/08_option_vega/config/strategy.yaml'),'--config-id','live_simnow'])
            i = fixture.InstrumentId.from_str('MO2611-C-6900.CFFEX')
            future = fixture.InstrumentId.from_str('IM2611.CFFEX')
            refs = NS(instrument_ids={'option':i,'future':future},
                contracts={'option':NS(tick='.2',multiplier=100)},
                futures={'future':{'minChgPriceNum':'.2','contMultNum':200}})
            service,driver = NS(),NS()
            inputs = NS(references=refs,ctp_upstream=None)
            session = NS()
            live = import_module('demos.08_option_vega.live')
            with patch.object(vega,'prepare_inputs',return_value=inputs), \
                    patch.object(live,'assemble',return_value=session) as assemble, \
                    patch('bomber.framework.trader.runtime.live.channels.ctp.required',return_value='fixture'):
                runtime = vega.build_shared_runtime(config,row,Path(directory)/'credentials.json',resume=False)
                actual,identity = runtime.prepare(service,driver,NS())
            self.assertIs(actual,session)
            self.assertEqual(identity,dict(strategy_id=row['strategy_id'],kind=row['kind'],argv=row['argv'],
                trading_day=config['trading_day'],environment='realtime',replay_md_trading_day=None,
                execution_mode='independent'))
            self.assertEqual(set(service.instrument_specs),{str(i),str(future)})
            self.assertEqual(assemble.call_args.kwargs['strategy_id'],row['strategy_id'])
            self.assertIs(assemble.call_args.kwargs['shared_service'],service)
            self.assertTrue(callable(session.restore_signal))


class RemoteRuntimeTests(unittest.TestCase):
    def test_two_instances_restore_separate_signals_cursors_and_late_fills_before_saving(self):
        with TemporaryDirectory() as directory:
            root, stop, barrier, sessions, calls = Path(directory), Event(), Barrier(2), {}, []
            def runtime(sid, quantity):
                identity = {'strategy_id': sid, 'trading_day': '20261110'}
                store = JsonStateStore(root/(sid+'.json'))
                store.save(dict(version=1, identity=identity, revision=1, cursor=1,
                    fills_received=quantity, signal={'batch':sid}), expected_generation=0)
                credentials = root/(sid+'-credentials.json')
                credentials.write_text(json.dumps(dict(strategy_id=sid, socket=str(root/'exec.sock'),
                    key='fixture', configuration={})))
                def prepare(service, driver, context):
                    state = dict(source_revision=2, ack=0, positions={sid:quantity})
                    port = NS(state=state, cursor=0, received_events=[], price_sync={})
                    port.set_cursor = lambda cursor: setattr(port, 'cursor', cursor)
                    port.drain_events = lambda: setattr(port, 'cursor', 2)
                    strategy = NS(fills_received=0, restore_target_revision=lambda rev: calls.append((sid,'revision',rev)))
                    signal = {}
                    def restore(saved): signal.update(saved)
                    def begin():
                        calls.append((sid,'begin',os.getpid()))
                        barrier.wait(timeout=5)
                        stop.set()
                    def close():
                        port.cursor = 3
                        strategy.fills_received += 1
                        calls.append((sid,'late_fill',port.cursor))
                    port.begin_stop = close
                    runner = NS(_submit_lock=RLock(), failure=None, fixed_spec='fixed', accept_bars=False,
                        start=lambda: None, stop=lambda: calls.append((sid,'stopped',port.cursor)),
                        session_ready=lambda: True, begin_bars=begin)
                    session = NS(client=port, runner=runner, strategy=strategy, restore_signal=restore,
                        signal_checkpoint=NS(snapshot_state=lambda: dict(signal)),
                        references=NS(spec='fixed', refresh=lambda: None))
                    sessions[sid] = session
                    return session, identity
                return RemoteStrategyRuntime(sid, store.path, credentials, '20261110', identity,
                    prepare, Mock(), Mock(), resume=True, seconds=.01)
            def describe(path, sid, key):
                return NS(call=lambda *_: {'state': dict(trading_day='20261110', source_revision=2, ack=0)})
            with patch('bomber.framework.trader.execution.ipc.UnixExecutionConnection', side_effect=describe), \
                    patch('bomber.framework.trader.execution.ipc_client.RemoteAccountService'), \
                    patch('bomber.framework.trader.execution.ipc_client.RemoteTraderSession'), \
                    patch('subprocess.Popen', side_effect=AssertionError('不得创建子进程')):
                run_remote_group([runtime('rb',2), runtime('p',4)], stop)
            for sid, quantity in [('rb',2),('p',4)]:
                saved = JsonStateStore(root/(sid+'.json')).load().payload
                self.assertEqual((saved['revision'], saved['cursor'], saved['fills_received']), (2,3,quantity+1))
                self.assertEqual(saved['signal'], {'batch':sid})
                self.assertIn((sid,'begin',os.getpid()), calls)
                self.assertLess(calls.index((sid,'late_fill',3)), calls.index((sid,'stopped',3)))

    def test_group_failure_requests_other_instance_to_finish_saving(self):
        entered, finished = Event(), Event()
        def first(stop):
            entered.set()
            self.assertTrue(stop.wait(5))
            finished.set()
        def second(stop):
            self.assertTrue(entered.wait(5))
            raise RuntimeError('fixture failure')
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            run_remote_group([NS(strategy_id='rb',run=first),NS(strategy_id='p',run=second)], Event())
        self.assertTrue(finished.is_set())

    def test_wrong_account_day_or_window_does_not_get_attached(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(ZONE)
            window = TradingWindow(now.strftime('%Y%m%d'), now, now+timedelta(minutes=3), 'test')
            socket = root/'exec.sock'; socket.touch()
            discovery = root/'current.json'
            discovery.write_text(json.dumps(dict(socket=str(socket), trading_day='19990101',
                start=window.start.isoformat(), end=window.end.isoformat())))
            validate = Mock()
            with self.assertRaisesRegex(RuntimeError, '未找到'):
                wait_for_account(discovery, window, Event(), validate, timeout=0)
            validate.assert_not_called()


class NativeHybridIpcTests(unittest.TestCase):
    from tests.test_hybrid_shared_layout import HybridSharedLayoutTests as Fixture
    setUp = Fixture.setUp
    make_account = Fixture.make_account
    session = Fixture.session
    bar = Fixture.bar

    def test_hybrid_entry_assembles_real_runner_with_original_checkpoint_identity(self):
        from contextlib import ExitStack
        from bomber.framework.trader.runtime.live.contracts import LiveRunContext
        from scripts.integration.shared_account.factories import shared_strategy_identity
        declaration = self.session('RB')
        market = self.root/'market-config.json'
        market.write_text(json.dumps(dict(source='dolphindb',table='fixture',timestamp_label='start',volume_mode='per-bar')))
        row = dict(strategy_id='hybrid-rb',kind='hybrid_transformer',state_file=str(self.root/'rb-signal.json'),argv=[
            '--product','RB','--release-path',str(self.release),'--directions-file',str(self.directions),
            '--signal-config',str(self.signals),'--market-source','dolphindb','--market-config',str(market),
            '--max-contracts','2','--entry-contracts','2','--report-dir',str(self.root/'reports'),'--no-session-overrides'])
        config = dict(trading_day='20261013',environment='realtime',seconds=60,account={'client_id':'account'})
        inputs = NS(references=declaration.references,upstream=fixture.Upstream('fixture'),calendar=self.cal)
        with ExitStack() as resources, \
                patch.object(import_module('demos.10_hybrid_transformer.shared'),'previous_source_day',return_value='20261012'), \
                patch.object(fixture.live,'prepare_inputs',return_value=inputs), \
                patch.object(fixture.adapter,'MizarWorkflowAdapter',fixture.Predictor), \
                patch('bomber.framework.trader.runtime.live.channels.ctp.required',return_value='fixture'):
            runtime = hybrid.build_shared_runtime(config,row,self.root/'credentials.json',resume=False)
            session,identity = runtime.prepare(self.service,self.account.driver,
                LiveRunContext(resources=resources,trading_day=datetime(2026,10,13).date()))
            self.assertEqual(identity,shared_strategy_identity(row,config,session))
            self.assertEqual(session.strategy.strategy_id,'hybrid-rb')
            self.assertEqual(session.strategy.config.max_contracts,2)
            self.assertEqual(set(self.service.instrument_specs),{str(self.rb)})
            self.assertIs(session.runner.shared_signal,session.signal_checkpoint)

    def test_real_rb_p_runners_trade_and_save_through_one_account_in_same_process(self):
        import threading
        import time
        from bomber.framework.trader.execution.ipc import AccountExecutionProtocol, UnixExecutionServer
        rb, p = self.session('RB'), self.session('P')
        protocol = AccountExecutionProtocol(self.service, keys={'hybrid-rb':'rb', 'hybrid-p':'p'}, trading_day='20261013')
        self.account.start(resume=False); self.service.start(); self.account.authorize()
        self.addCleanup(self.account.driver.stop)
        directory = TemporaryDirectory(dir='/tmp'); self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        server = UnixExecutionServer(root/'exec.sock', protocol); self.addCleanup(server.close)
        running, stop, filled, failures, sessions = Event(), Event(), set(), [], {}
        prepared = Barrier(2)
        running.set()
        def control():
            try:
                while running.is_set():
                    server.poll(); protocol.poll()
                    for ref in list(self.transport.active): self.transport.fill(ref)
                    time.sleep(.002)
            except BaseException as error:
                failures.append(error); stop.set()
        thread = threading.Thread(target=control)
        runtimes = []
        for product, declaration in [('RB',rb), ('P',p)]:
            sid = 'hybrid-'+product.lower()
            credentials = root/(sid+'-credentials.json')
            credentials.write_text(json.dumps(dict(strategy_id=sid, socket=str(server.path),
                key=product.lower(), configuration=declaration.client.configuration(),
                reference=getattr(declaration, 'session_identity', None))))
            def prepare(service, driver, context, product=product, sid=sid):
                session = self.session(product, service=service, driver=driver)
                sessions[sid] = session
                session.references.refresh = lambda: None
                session.runner.session_ready = lambda: session.client.is_reconciled
                def begin():
                    session.runner.accept_bars = True
                    prepared.wait(timeout=5)
                    # 使用订阅/预热完成后的分钟，保留Runner的首次完整Bar闸门。
                    self.bar(session, '2026-10-13 09:04', 1 if product == 'RB' else .3)
                session.runner.begin_bars = begin
                return session, {'strategy_id':sid}
            from bomber.framework.trader.runtime.live.remote import poll_strategy_session
            def poll(session, save, product=product, sid=sid):
                poll_strategy_session(session, save)
                key = product.lower()+'_main'
                quantity = 2 if product == 'RB' else 1
                if session.client.position_manager.position(sid,key) == quantity:
                    filled.add(sid)
                    if len(filled) == 2: stop.set()
            runtimes.append(RemoteStrategyRuntime(sid, root/(sid+'.json'), credentials, '20261013',
                {'strategy_id':sid}, prepare, Mock(), Mock(), seconds=5,
                restore_fills_received=False, poll_session=poll))
        thread.start()
        try:
            with patch('subprocess.Popen', side_effect=AssertionError('不得创建子进程')):
                run_remote_group(runtimes, stop)
            self.assertFalse(failures, failures)
            self.assertEqual(filled, {'hybrid-rb','hybrid-p'})
            self.assertEqual(self.transport.starts, 1)
            self.assertEqual(len(self.transport.sent), 2)
            for sid in filled:
                saved = JsonStateStore(root/(sid+'.json')).load().payload
                self.assertGreater(saved['cursor'], 0)
                self.assertIn('signal', saved)
                self.assertEqual(len(sessions[sid].strategy.holdings.active), 1)
        finally:
            running.clear(); thread.join(2)


if __name__ == '__main__':
    unittest.main()
