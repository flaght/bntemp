"""NT算法CTP离线闭环；仅NT原生时钟宿主和柜台传输使用替身。"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from tests import test_shared_account as fixture


class ManualNTHost:
    """只替换原生SDK宿主；任务状态、Driver、风控、冻结和持久化均用实际代码。"""
    def __init__(self):
        self.primaries, self.alarms, self.counts = {}, {}, {}

    def primary(self, key, order, attempts=0):
        self.primaries[key], self.counts[key] = order, attempts

    def child(self, key, order):
        self.counts[key] += 1
        return f"{key}-E{self.counts[key]}"

    def alarm(self, key, when, callback):
        self.alarms[key] = when, callback

    def cancel_alarm(self, key):
        self.alarms.pop(key, None)

    def advance(self, now):
        for key, (when, callback) in tuple(self.alarms.items()):
            if when <= now:
                self.alarms.pop(key, None)
                callback()

    def close(self):
        self.alarms.clear()


class NTAlgoCtpTests(unittest.TestCase):
    setUp = fixture.SharedAccountTests.setUp
    native_modules = fixture.SharedAccountTests.native_modules
    native_account = fixture.SharedAccountTests.native_account
    submit = fixture.SharedAccountTests.submit
    request = fixture.SharedAccountTests.request
    client = fixture.SharedAccountTests.client

    def make_account(self, path=None, *, max_attempts=3, start=True,
                     host_factory=ManualNTHost, wait_seconds=10):
        modules = self.native_modules()
        from bomber.framework.trader.execution.nt_algo import build_nt_algo_runtime, NTAlgoConfig
        from bomber.framework.trader.execution.ctp.nt_algo import NTAlgoCtpDriver
        from bomber.framework.trader.runtime.live.channels.ctp_independent import IndependentCtpLifecycle
        runtime = build_nt_algo_runtime('limit_reprice', NTAlgoConfig(wait_seconds, max_attempts),
                                       host_factory=host_factory, clock_ns=lambda: self.now)
        factory = lambda *args, **kwargs: NTAlgoCtpDriver(*args, algo_runtime=runtime, **kwargs)
        self.enterContext(patch.object(modules.driver, 'CtpNativeTraderDriver', factory))
        self.enterContext(patch.object(modules.lifecycle, 'CtpSharedAccountLifecycle', IndependentCtpLifecycle))
        if path is None:
            path = Path(self.enterContext(TemporaryDirectory())) / 'account.json'
        account, service, transport, ports, _ = self.native_account(path, modules, execution_mode='independent')
        connect = transport.connect
        transport.connect = lambda *args: replace(connect(*args), front_id=1, session_id=2)
        raw = transport.raw
        transport.raw = lambda ref: {**raw(ref), 'Direction': transport.active[ref]['Direction'],
                                    'OffsetFlag': transport.active[ref]['CombOffsetFlag']}
        self.enterContext(patch('time.time_ns', side_effect=lambda: self.now))
        self.addCleanup(account.driver.stop)
        if start:
            account.start(resume=False)
            service.start()
            for port in ports:
                port.register_execution_event_handler(lambda event: None)
                port.start()
            account.authorize()
        return account, service, transport, ports, runtime

    def tick(self, service, runtime, seconds=10, price=3007):
        self.now += int(seconds * 1e9)
        service.prices.update(self.instrument, price, self.now)
        runtime._host.advance(self.now)
        service.poll()

    def test_algorithm_factory_selects_runtime_and_rejects_unimplemented_algorithms(self):
        from bomber.framework.trader.execution.nt_algo import build_nt_algo_runtime, NTAlgoConfig
        config = NTAlgoConfig(wait_seconds=5, max_attempts=2)
        runtime = build_nt_algo_runtime('limit_reprice', config, host_factory=ManualNTHost)
        self.assertEqual(runtime.algorithm_name, 'limit_reprice')
        self.assertIs(runtime.config, config)
        self.assertIsNone(runtime._host)  # 装配不提前加载原生SDK或连接柜台。
        self.assertEqual(build_nt_algo_runtime().algorithm_name, 'limit_reprice')
        for name in ('twap', 'combined', '', None):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, '仅支持limit_reprice'):
                build_nt_algo_runtime(name)

    def test_native_strategy_ids_are_valid_distinct_and_keep_original_ownership(self):
        import sys
        from decimal import Decimal
        from types import ModuleType, SimpleNamespace as NS
        from bomber.framework.trader.execution.nt_algo import NativeNTHost, encode_order

        class StrictStrategyId(str):
            def __new__(cls, value):
                if '-' not in value:
                    raise ValueError("value was malformed: did not contain a hyphen '-'")
                return super().__new__(cls, value)

        def strict_limit_order(**values):
            if values.get('exec_algorithm_id') is not None and values.get('exec_spawn_id') is None:
                raise TypeError("'exec_spawn_id' argument was `None`")
            return NS(**values)

        def module(name, **members):
            result = ModuleType(name)
            result.__dict__.update(members)
            return result

        # 保留实际primary构造路径；替身复现SDK的策略ID和算法订单关联约束。
        sdk = {
            'bomber.core.uuid': module('bomber.core.uuid', UUID4=object),
            'bomber.execution.messages': module('bomber.execution.messages', SubmitOrder=NS),
            'bomber.model.identifiers': module('bomber.model.identifiers',
                ClientOrderId=str, StrategyId=StrictStrategyId, InstrumentId=type(self.instrument)),
            'bomber.model.enums': module('bomber.model.enums', OrderSide=self.contracts.OrderSide),
            'bomber.model.objects': module('bomber.model.objects',
                Quantity=NS(from_str=Decimal), Price=NS(from_str=Decimal)),
            'bomber.model.orders': module('bomber.model.orders', LimitOrder=strict_limit_order),
        }
        primaries, commands = [], []
        host = NativeNTHost.__new__(NativeNTHost)
        host.trader_id, host.clock = 'ALGO-001', NS(timestamp_ns=lambda: 1)
        host.cache = NS(add_order=primaries.append)
        host.algorithm = NS(id='LimitRepriceAlgorithm', execute=commands.append)
        source_ids = ('one', 'one-001', 'hybrid-RB-001', 'NTAlgo-one')
        with patch.dict(sys.modules, sdk):
            for index, source_id in enumerate(source_ids):
                order = self.contracts.OrderIntent(source_id, 'account', self.instrument,
                    'BUY', 2, order_type='LIMIT', price=3000, position_effect='OPEN')
                host.primary(f'NTALG-{index}', order)
                self.assertEqual(primaries[-1].strategy_id, f'NTAlgo-{source_id}')
                self.assertEqual(primaries[-1].exec_spawn_id, primaries[-1].client_order_id)
                self.assertEqual(commands[-1].strategy_id, primaries[-1].strategy_id)
                self.assertEqual(encode_order(order)['strategy_id'], source_id)
        self.assertEqual(len({order.strategy_id for order in primaries}), len(source_ids))

    def test_timeout_cancel_then_remaining_order_uses_original_risk_and_attribution(self):
        account, service, transport, (one, _), runtime = self.make_account()
        self.submit(one, 2)
        first = transport.sent[-1]['OrderRef']
        transport.fill(first, 1)
        self.tick(service, runtime, 9)
        self.assertFalse(transport.canceled)
        self.tick(service, runtime, 1)
        self.assertEqual(transport.canceled, [first])
        self.assertEqual(len(transport.sent), 1)  # 回报回调中不补单。
        service.poll()
        second = transport.sent[-1]
        self.assertEqual(second['VolumeTotalOriginal'], 1)
        self.assertEqual(second['LimitPrice'], 3007)
        self.assertNotEqual(second['OrderRef'], first)
        self.assertEqual(account.driver.order_budget['submitted'], 2)
        transport.fill(second['OrderRef'])
        self.assertEqual(service.position_manager.position('one', 'main'), 2)
        self.assertFalse(runtime.snapshot_state()['tasks'])
        account._validate_account()

    def test_cancel_ack_with_late_trade_does_not_send_duplicate_quantity(self):
        account, service, transport, (one, _), runtime = self.make_account()
        transport.cancel_order = lambda ref: transport.canceled.append(ref)
        self.submit(one, 2)
        first = transport.sent[-1]['OrderRef']
        self.tick(service, runtime)
        transport.on_order({**transport.raw(first), 'OrderStatus': '5', 'VolumeTraded': 1})
        service.poll()
        self.assertEqual(len(transport.sent), 1)
        transport.fill(first, 1)
        transport.active.pop(first)
        service.poll()
        self.assertEqual(transport.sent[-1]['VolumeTotalOriginal'], 1)
        transport.fill(transport.sent[-1]['OrderRef'])
        self.assertEqual(service.position_manager.position('one', 'main'), 2)

    def test_fill_after_timeout_cancel_request_finishes_task_without_reordering(self):
        account, service, transport, (one, _), runtime = self.make_account()
        # 撤单在柜台处理中，原订单仍可能全部成交。
        transport.cancel_order = lambda ref: transport.canceled.append(ref)
        self.submit(one, 2)
        first = transport.sent[-1]['OrderRef']
        self.tick(service, runtime)
        self.assertEqual(transport.canceled, [first])
        transport.fill(first)
        service.poll()
        self.tick(service, runtime)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(transport.canceled, [first])
        self.assertFalse(runtime.snapshot_state()['tasks'])
        self.assertFalse(runtime.snapshot_state()['stops'])
        self.assertEqual(service.position_manager.position('one', 'main'), 2)
        self.assertFalse(service.position_manager.attribution.working_quantity(
            service.client_id, self.instrument, 'one', 'main'))
        self.assertNotIn('one', service._suspended)
        account._validate_account()

    def test_changed_target_stops_old_task_even_between_actual_orders(self):
        _, service, transport, (one, _), runtime = self.make_account()
        self.submit(one, 2)
        self.tick(service, runtime)
        self.assertEqual(len(transport.sent), 1)
        self.submit(one, -1, revision=2)
        self.assertEqual(transport.sent[-1]['Direction'], '1')
        service.poll()
        self.assertEqual(len(transport.sent), 2)
        self.assertEqual(len(runtime.snapshot_state()['tasks']), 1)
        transport.fill(transport.sent[-1]['OrderRef'])
        self.assertEqual(service.position_manager.position('one', 'main'), -1)

    def test_max_attempts_stops_after_last_cancel(self):
        _, service, transport, (one, _), runtime = self.make_account(max_attempts=2)
        self.submit(one, 2)
        self.tick(service, runtime)
        service.poll()
        self.tick(service, runtime)
        service.poll()
        self.assertEqual(len(transport.sent), 2)
        self.assertEqual(len(transport.canceled), 2)
        self.assertFalse(runtime.snapshot_state()['tasks'])
        self.assertIn('one', service._suspended)
        service.continue_targets('one', self.instrument, self.now)
        self.assertEqual(len(transport.sent), 2)

    def test_missing_fresh_quote_waits_and_does_not_send_blindly(self):
        _, service, transport, (one, _), runtime = self.make_account()
        self.submit(one, 2)
        self.tick(service, runtime)
        self.now += 121_000_000_000
        service.poll()
        self.assertEqual(len(transport.sent), 1)
        service.prices.update(self.instrument, 3010, self.now)
        service.poll()
        self.assertEqual(transport.sent[-1]['LimitPrice'], 3010)
        transport.fill(transport.sent[-1]['OrderRef'])

    def test_replacement_obeys_original_risk_limit(self):
        _, service, transport, (one, _), runtime = self.make_account()
        self.submit(one, 2)
        self.tick(service, runtime)
        service.client.risk_manager.check = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('risk blocked'))
        with self.assertRaisesRegex(ValueError, 'risk blocked'):
            service.poll()
        self.assertEqual(len(transport.sent), 1)

    def test_replacement_rechecks_demo_authorization(self):
        _, service, transport, (one, _), runtime = self.make_account()
        self.submit(one, 2)
        self.tick(service, runtime)
        service.client._demo_environment_check = lambda: False
        with self.assertRaises(PermissionError):
            service.poll()
        self.assertEqual(len(transport.sent), 1)
        self.assertFalse(service.client.is_armed)

    def test_replacement_respects_new_reduce_only_health_guard(self):
        _, service, transport, (one, _), runtime = self.make_account()
        self.submit(one, 2)
        self.tick(service, runtime)
        one.reduce_only_check = lambda: True
        service.poll()
        self.assertEqual(len(transport.sent), 1)
        self.assertFalse(runtime.snapshot_state()['tasks'])

    def test_replacement_rechecks_latest_source_health_mode(self):
        _, service, transport, (one, _), runtime = self.make_account()
        self.submit(one, 2)
        self.tick(service, runtime)
        service._sources['one']['metadata']['market_health_mode'] = 'REDUCE_ONLY'
        with self.assertRaises(self.risk.RiskRejected):
            service.poll()
        self.assertEqual(len(transport.sent), 1)

    def test_recovery_rejects_actual_order_without_matching_task(self):
        account, _, _, (one, _), runtime = self.make_account()
        self.submit(one, 2)
        task = next(iter(runtime._tasks.values()))
        task['active'] = None
        with self.assertRaisesRegex(RuntimeError, '缺少对应的NT算法任务'):
            account.driver.validate_algorithm_state()

    def test_fill_racing_timeout_does_not_cancel_or_resubmit_completed_task(self):
        _, service, transport, (one, _), runtime = self.make_account()
        self.submit(one, 2)
        first = transport.sent[-1]['OrderRef']
        self.now += 10_000_000_000
        runtime._host.advance(self.now)
        transport.fill(first)
        service.poll()
        self.assertFalse(transport.canceled)
        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(service.position_manager.position('one', 'main'), 2)

    def test_native_primary_failure_does_not_leave_unrecoverable_empty_task(self):
        _, service, transport, (one, _), runtime = self.make_account()
        runtime._host.primary = lambda *args: (_ for _ in ()).throw(RuntimeError('native API failure'))
        with self.assertRaisesRegex(RuntimeError, 'native API failure'):
            self.submit(one, 2)
        self.assertFalse(runtime.snapshot_state()['tasks'])
        self.assertFalse(transport.sent)

    def test_real_order_and_algo_progress_saved_in_same_checkpoint(self):
        import json
        account, service, transport, (one, _), runtime = self.make_account()
        self.submit(one, 2)
        transport.fill(transport.sent[-1]['OrderRef'], 1)
        payload = json.loads(account.manager.repository.path.read_text())['payload']
        task = next(iter(payload['component_states']['nt_algo']['tasks'].values()))
        self.assertEqual(task['filled'], '1')
        self.assertEqual(task['active'], payload['ctp_drivers']['account']['orders'][0]['client_order_id'])
        self.assertEqual(len(payload['positions']['attribution']['orders']), 1)

    def test_same_day_restart_reconciles_actual_order_and_keeps_task_progress(self):
        path = Path(self.enterContext(TemporaryDirectory())) / 'account.json'
        account, service, transport, (one, _), runtime = self.make_account(path)
        self.submit(one, 2)
        first = transport.sent[-1]['OrderRef']
        transport.fill(first, 1)
        task_id = next(iter(runtime.snapshot_state()['tasks']))
        account.driver.stop()
        account2, service2, transport2, ports2, runtime2 = self.make_account(path, start=False)
        transport2.active = {k: dict(v) for k, v in transport.active.items()}
        transport2.gross = dict(transport.gross)
        account2.start(resume=True)
        self.assertEqual(runtime2.snapshot_state()['tasks'][task_id]['filled'], '1')
        self.assertEqual(runtime2.snapshot_state()['tasks'][task_id]['active'],
                         f'CTP-9999-demo-20261013-{first}')
        self.assertFalse(transport2.sent)
        self.assertNotEqual(path.read_bytes(), b'')

    def test_switching_driver_with_active_task_is_rejected(self):
        account, _, _, (one, _), runtime = self.make_account()
        self.submit(one, 2)
        import json
        payload = json.loads(account.manager.repository.path.read_text())['payload']
        original = account.driver.algo_runtime
        del account.driver.algo_runtime
        try:
            with self.assertRaisesRegex(ValueError, '禁止切换'):
                account._adapt_algorithm_checkpoint(payload)
        finally:
            account.driver.algo_runtime = original

    def test_cross_day_pending_retry_is_rejected_before_payload_migration(self):
        path = Path(self.enterContext(TemporaryDirectory())) / 'account.json'
        account, service, transport, (one, _), runtime = self.make_account(path)
        self.submit(one, 2)
        self.tick(service, runtime)  # 已撤销实际单，但任务仍等待补单。
        self.assertFalse(transport.active)
        self.assertTrue(runtime.snapshot_state()['tasks'])
        account.driver.stop()
        account2, _, _, _, _ = self.make_account(path, start=False)
        account2.expected_day = '20261014'
        transformed = []
        with self.assertRaisesRegex(ValueError, '跨交易日'):
            account2.start(resume=True, payload_transform=lambda p: transformed.append(p) or p)
        self.assertFalse(transformed)

    def test_close_retry_only_closes_remaining_owned_position(self):
        account, service, transport, (one, _), runtime = self.make_account()
        self.submit(one, 2)
        transport.fill(transport.sent[-1]['OrderRef'])
        self.submit(one, 0, revision=2)
        first = transport.sent[-1]['OrderRef']
        transport.fill(first, 1)
        self.tick(service, runtime)
        service.poll()
        second = transport.sent[-1]
        self.assertNotEqual(second['CombOffsetFlag'], '0')
        self.assertEqual(second['VolumeTotalOriginal'], 1)
        transport.fill(second['OrderRef'])
        self.assertEqual(service.position_manager.position('one', 'main'), 0)
        account._validate_account()

    def test_stale_original_task_deadline_does_not_reopen(self):
        _, service, transport, (one, _), runtime = self.make_account()
        request = self.request(one.client_id, strategy='one', quantity=2, timestamp=self.now)
        one.submit_targets(replace(request, deadline_ns=self.now + 5_000_000_000))
        self.tick(service, runtime, seconds=6)
        service.poll()
        self.assertEqual(len(transport.sent), 1)
        self.assertFalse(transport.active)
        self.assertFalse(runtime.snapshot_state()['tasks'])

    def test_idle_driver_switch_preserves_positions_and_other_checkpoint_components(self):
        import json
        account, _, transport, (one, _), runtime = self.make_account()
        self.submit(one, 2)
        transport.fill(transport.sent[-1]['OrderRef'])
        payload = json.loads(account.manager.repository.path.read_text())['payload']
        expected = payload['positions']
        original = account.driver.algo_runtime
        del account.driver.algo_runtime
        try:
            result = account._adapt_algorithm_checkpoint(payload)
        finally:
            account.driver.algo_runtime = original
        self.assertNotIn('nt_algo', result['component_states'])
        self.assertEqual(result['positions'], expected)
        result = account._adapt_algorithm_checkpoint(result)
        self.assertEqual(result['component_states']['nt_algo'], runtime.snapshot_state())


class NTAlgoConfigurationTests(unittest.TestCase):
    def test_configuration_validates_without_loading_native_nt(self):
        import json
        from scripts.integration.shared_account.config import resolve_config
        path = Path(__file__).resolve().parents[1] / 'demos/10_hybrid_transformer/config/shared_rb_p.json'
        config = json.loads(path.read_text())
        config.update(execution_driver='nt_algo_ctp', nt_algo={'wait_seconds': 10, 'max_attempts': 3})
        self.assertEqual(resolve_config(config, path)['execution_driver'], 'nt_algo_ctp')
        for policy in ({'wait_seconds': float('nan')}, {'max_attempts': True}, {'unknown': 1}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                resolve_config({**config, 'nt_algo': policy}, path)
        with self.assertRaisesRegex(ValueError, 'independent'):
            resolve_config({**config, 'execution_mode': 'netted'}, path)

    def test_algorithm_selection_is_independent_of_driver_and_defaults_for_legacy_config(self):
        import json
        from scripts.integration.shared_account.config import resolve_config
        path = Path(__file__).resolve().parents[1] / 'demos/10_hybrid_transformer/config/shared_rb_p.json'
        original = json.loads(path.read_text())
        legacy = {**original, 'execution_driver': 'nt_algo_ctp'}
        self.assertEqual(resolve_config(legacy, path)['execution_algorithm'], 'limit_reprice')
        self.assertNotIn('execution_algorithm', legacy)
        self.assertEqual(resolve_config({**legacy, 'execution_algorithm': 'limit_reprice'}, path)
                         ['execution_algorithm'], 'limit_reprice')
        for name in ('twap', 'combined', '', None):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'execution_algorithm'):
                resolve_config({**legacy, 'execution_algorithm': name}, path)
        with self.assertRaisesRegex(ValueError, 'nt_algo_ctp'):
            resolve_config({**original, 'execution_driver': 'ctp',
                            'execution_algorithm': 'limit_reprice'}, path)
        self.assertNotIn('execution_algorithm', resolve_config(original, path))

    def check_direct(self, args):
        import json
        from contextlib import redirect_stdout
        from io import StringIO
        from types import SimpleNamespace
        from demos import direct_ctp
        original_import = direct_ctp.import_module
        def entry(name):
            if name == 'demos.10_hybrid_transformer.reconcile_direct_positions':
                return SimpleNamespace(parse_position=lambda value: value)
            return original_import(name)
        output = StringIO()
        with patch.object(direct_ctp, 'import_module', side_effect=entry), redirect_stdout(output):
            direct_ctp.main('hybrid_transformer', args)
        return json.loads(output.getvalue())

    def test_direct_cli_reports_selected_algorithm_and_rejects_driver_mismatch(self):
        from contextlib import redirect_stderr
        from io import StringIO
        for args in (['--execution-driver', 'nt_algo_ctp'],
                ['--execution-driver', 'nt_algo_ctp', '--execution-algorithm', 'limit_reprice']):
            with self.subTest(args=args):
                result = self.check_direct(args)
                self.assertEqual(result['execution_driver'], 'nt_algo_ctp')
                self.assertEqual(result['execution_algorithm'], 'limit_reprice')
                self.assertFalse(result['orders_enabled'])
        self.assertIsNone(self.check_direct([])['execution_algorithm'])
        for args in (['--execution-algorithm', 'limit_reprice'],
                ['--execution-driver', 'nt_algo_ctp', '--execution-algorithm', 'twap']):
            with self.subTest(args=args), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
                self.check_direct(args)
            self.assertEqual(raised.exception.code, 2)

    def test_launch_yaml_applies_algorithm_and_cli_can_override_its_value(self):
        from contextlib import redirect_stderr
        from io import StringIO
        import yaml
        from bomber.framework.cli import load_config
        root = Path(__file__).resolve().parents[1]
        profile = load_config(root / 'demos/10_hybrid_transformer/config/strategy.yaml', 'direct_nt_algo')
        self.assertEqual(profile['execution_algorithm'], 'limit_reprice')
        # 使用真实启动配置，但校验期间显式关闭连接/报单，入口会在建连前返回。
        profile.update(connect=False, enable_orders=False, confirm_simnow=False)
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'launch.yaml'
            args = ['--config-file', str(path), '--config-id', 'check']
            path.write_text(yaml.safe_dump({'experiment_setups': {'check': profile}}))
            self.assertEqual(self.check_direct(args)['execution_algorithm'], 'limit_reprice')
            profile['execution_algorithm'] = 'twap'
            path.write_text(yaml.safe_dump({'experiment_setups': {'check': profile}}))
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                self.check_direct(args)
            result = self.check_direct(args + ['--execution-algorithm', 'limit_reprice'])
            self.assertEqual(result['execution_algorithm'], 'limit_reprice')


if __name__ == '__main__':
    unittest.main()
