"""08当日零成交重选维护：真实文件校验、锁与备份，不连接行情或柜台。"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo
import json
import unittest

from tests import test_direct_ctp  # 复用本机SDK实体兼容夹具。
from bomber.framework.trader.persistence import JsonStateStore, ConcurrentStateWriteError
from bomber.framework.trader.runtime.live.local import strategy_state_lock

app = import_module('demos.08_option_vega.rearm_entry')
SID = 'option-vega-direct'
NOW = datetime(2026, 11, 26, 11, 30, tzinfo=ZoneInfo('Asia/Shanghai'))


class EntryRearmTests(unittest.TestCase):
    def fixtures(self):
        config = json.loads((app.DEMO/'config/vega_live.json').read_text())
        config['option_slot'] = '11:08'
        parameters = json.loads(json.dumps(asdict(app.OptionVegaConfig(**{
            key: value for key, value in config.items()
            if key in {field.name for field in app.fields(app.OptionVegaConfig)}})), default=str))
        goals = {'MO2612-C-5800': '0', 'MO2612-C-5900': '0', 'MO2612-C-6000': '-4',
                 'MO2612-C-6100': '-4', 'MO2612-C-6200': '-5', 'MO2612-C-6300': '-6'}
        targets = {key+'.CFFEX': value for key, value in {**goals, 'IM2612': '2'}.items()}
        routes = {key: key for key in targets}
        identity = dict(strategy_id=SID, kind='option_vega', trading_day='20261126',
            environment='realtime', routes={'routes': routes}, reference=dict(config=parameters,
            mode='simnow', environment='realtime', td_day='20261126',
            max_notional=config['max_notional'], limit_offset_ticks=config['limit_offset_ticks']))
        business = dict(version=1, option_goals=goals, future_goals={'IM2612': '2'},
            sent_days=['2026-11-25', '2026-11-26'], hedge_sent=['2026-11-26_11:00'],
            pending_scheduled_hedges={}, submissions=6, entry_decisions=4, flatten_requested=False)
        records, orders, events = {}, [], []
        for index in range(12):
            instrument = f'MO2612-C-{5800 + index % 4 * 100}.CFFEX'
            order = f'CTP-20261126-{index}'
            records[str(index)] = dict(strategy_id=SID, day='20261126', client_order_id=order,
                instrument_id=instrument, terminal=True, trades=[])
            orders.append(dict(client_order_id=order, instrument_id=instrument,
                status='CANCELED', filled_quantity='0'))
            events.append(dict(type='OrderUpdateEvent', sequence=index+1,
                event=dict(identity={'client_order_id': order}, status='CANCELED')))
        # 已有仓位来自前一交易日成交；维护不能删除它或把它视为今天新增成交。
        events.append(dict(type='FillEvent', sequence=13, event=dict(
            ts_event=int(NOW.replace(day=25).timestamp())*10**9,
            identity={'client_order_id': 'old-filled'}, quantity='19')))
        signal = dict(version=1, identity=deepcopy(identity), revision=6, cursor=len(events),
            fills_received=0, signal=business, failure=None)
        account = dict(positions=dict(working_quantities=[], attribution=dict(owned=[
            dict(strategy_id=SID, instrument_id=key, quantity=value)
            for key, value in targets.items() if value != '0'])),
            orders={'client': orders}, ctp_drivers={'client': {'trading_day': '20261126', 'orders': []}},
            portfolio={'revision': 6, 'strategy_revisions': {SID: 6},
                'contributions': {SID: [{'key': {'client_id': 'client', 'instrument_id': key},
                'quantity': value} for key, value in targets.items()]}}, order_budget={'submitted': 12, 'limit': 200},
            component_states=dict(
                session_identity={'identity': {'trading_day': '20261126', 'strategies': {SID: identity}}},
                ctp_order_owners={'records': records}, strategy_buckets={'frozen': {}},
                nt_algo={'tasks': {}, 'stops': [], 'config': {'max_attempts': 3}},
                shared_account=dict(pending=False, failed=False, portfolio_revision=6,
                    strategy_revisions={SID: 6}, sources={SID: dict(revision=6,
                    targets=targets, rows=[dict(strategy_id=SID, target_key=key,
                        instrument_id=key, quantity=value) for key, value in targets.items()],
                    metadata={'checkpoint_alignment': {'revision': 6, 'signal': deepcopy(business)}})},
                    shared=dict(execution_mode='independent', execution_stop=None,
                        routes={SID: {'routes': routes}}, bootstrap_signals={SID: deepcopy(business)},
                        event_journal={'events': {SID: events}, 'ack': {SID: len(events)}}))))
        return account, signal, config

    def prepare(self, account, signal, config, **kwargs):
        return app.prepare_rearm(account, signal, config, strategy_id=SID, trading_day='2026-11-26',
            option_slot=kwargs.get('option_slot', '13:40'), now=kwargs.get('now', NOW))

    def files(self, root):
        root = root.resolve()
        account, signal, config = self.fixtures()
        paths = [root/'state.json', root/'signal.json', root/'vega_live.json']
        JsonStateStore(paths[0]).save(account, expected_generation=0)
        JsonStateStore(paths[1]).save(signal, expected_generation=0)
        paths[2].write_text(json.dumps(config))
        return paths

    def operate(self, paths, **kwargs):
        return app.rearm(account_state_file=paths[0], signal_file=paths[1], strategy_config=paths[2],
            strategy_id=SID, trading_day='2026-11-26', option_slot='13:40', now=NOW, **kwargs)

    def test_twelve_terminal_zero_fill_orders_preserve_positions_history_counters_and_cursors(self):
        account, signal, config = self.fixtures()
        original = deepcopy((account, signal, config))
        updated, revised, parameters, evidence = self.prepare(account, signal, config)
        self.assertEqual((account, signal, config), original)
        self.assertEqual(evidence['previous_order_count'], 12)
        self.assertEqual(evidence['orders_submitted'], 0)
        self.assertEqual(revised['signal']['sent_days'], ['2026-11-25'])
        expected_business = {**signal['signal'], 'sent_days': ['2026-11-25']}
        self.assertEqual(revised['signal'], expected_business)
        self.assertEqual(revised['cursor'], signal['cursor'])
        self.assertEqual(revised['revision'], signal['revision'])
        for key in ('positions', 'orders', 'ctp_drivers', 'portfolio', 'order_budget'):
            self.assertEqual(updated[key], account[key])
        for key in ('ctp_order_owners', 'strategy_buckets', 'nt_algo'):
            self.assertEqual(updated['component_states'][key], account['component_states'][key])
        shared = updated['component_states']['shared_account']
        self.assertEqual(shared['shared']['event_journal'], account['component_states']['shared_account']['shared']['event_journal'])
        self.assertEqual(shared['sources'][SID]['metadata']['checkpoint_alignment']['signal'], expected_business)
        self.assertEqual(shared['shared']['bootstrap_signals'][SID], expected_business)
        self.assertEqual(parameters, {**config, 'option_slot': '13:40'})
        current_identity = updated['component_states']['session_identity']['identity']['strategies'][SID]
        self.assertEqual(current_identity, revised['identity'])
        self.assertEqual(current_identity['reference']['config']['option_slot'], '13:40')
        import_module('scripts.integration.shared_account.rollover').validate_strategy_identity(
            current_identity, revised['identity'])

    def test_fills_unsafe_execution_or_remaining_targets_refuse_without_mutation(self):
        def modify(case, name):
            account, signal, config = case
            c = account['component_states']
            shared = c['shared_account']
            if name == 'trade': c['ctp_order_owners']['records']['0']['trades'] = [['20261126', 'CFFEX', 'T1']]
            if name == 'filled': account['orders']['client'][0]['filled_quantity'] = '1'
            if name == 'fill_event': shared['shared']['event_journal']['events'][SID][-1]['event']['ts_event'] = int(NOW.timestamp())*10**9
            if name == 'nonterminal': c['ctp_order_owners']['records']['0']['terminal'] = False
            if name == 'order_state': account['orders']['client'][0]['status'] = 'ACCEPTED'
            if name == 'missing_order': account['orders']['client'].pop(0)
            if name == 'frozen': c['strategy_buckets']['frozen']['O1'] = {}
            if name == 'task': c['nt_algo']['tasks']['T1'] = {}
            if name == 'stop': c['nt_algo']['stops'].append({})
            if name == 'working': account['positions']['working_quantities'].append({'quantity': '1'})
            if name == 'driver_order': account['ctp_drivers']['client']['orders'].append({})
            if name == 'driver_day': account['ctp_drivers']['client']['trading_day'] = '20261125'
            if name == 'pending': shared['pending'] = True
            if name == 'failed': shared['failed'] = True
            if name == 'budget': shared['shared']['execution_stop'] = {'reason': 'order_budget_exhausted'}
            if name == 'cursor': signal['cursor'] -= 1
            if name == 'ack': shared['shared']['event_journal']['ack'][SID] -= 1
            if name == 'revision': signal['revision'] -= 1
            if name == 'goal': signal['signal']['future_goals']['IM2612'] = '3'
            if name == 'source_goal': shared['sources'][SID]['targets']['IM2612.CFFEX'] = '3'
            if name == 'source_row': shared['sources'][SID]['rows'][-1]['quantity'] = '3'
            if name == 'contribution': account['portfolio']['contributions'][SID][-1]['quantity'] = '3'
            if name == 'order_contract': account['orders']['client'][0]['instrument_id'] = 'other.CFFEX'
            if name == 'risk': config['vega_budget'] += 1
            if name == 'notional': config['max_notional'] = '1'
            if name == 'offset': config['limit_offset_ticks'] = 0
            if name == 'flat': signal['signal']['flatten_requested'] = True
            if name == 'not_consumed': signal['signal']['sent_days'].remove('2026-11-26')
            if name == 'identity': signal['identity']['strategy_id'] = 'other'
            if name == 'other_strategy': c['session_identity']['identity']['strategies']['hybrid'] = {}
        for name in ('trade', 'filled', 'fill_event', 'nonterminal', 'order_state', 'missing_order',
                     'frozen', 'task', 'stop', 'working', 'driver_order', 'driver_day', 'pending',
                     'failed', 'budget', 'cursor', 'ack', 'revision', 'goal', 'source_goal', 'source_row',
                     'contribution', 'order_contract', 'risk', 'notional', 'offset', 'flat', 'not_consumed', 'identity', 'other_strategy'):
            case = self.fixtures()
            modify(case, name)
            original = deepcopy(case)
            with self.subTest(name=name), self.assertRaises((ValueError, KeyError)):
                self.prepare(*case)
            self.assertEqual(case, original)

    def test_unused_old_bootstrap_and_alignment_are_preserved(self):
        account, signal, config = self.fixtures()
        source = account['component_states']['shared_account']
        source['sources'][SID]['metadata']['checkpoint_alignment']['revision'] -= 1
        source['shared']['bootstrap_signals'][SID]['submissions'] -= 1
        original = deepcopy(source)
        updated, revised, _, _ = self.prepare(account, signal, config)
        self.assertNotIn('2026-11-26', revised['signal']['sent_days'])
        self.assertEqual(updated['component_states']['shared_account'], original)

    def test_closed_zero_fill_targets_are_rebased_before_reselection(self):
        account, signal, config = self.fixtures()
        instrument = 'MO2612-C-6000.CFFEX'
        signal['signal']['option_goals']['MO2612-C-6000'] = '-8'
        source = account['component_states']['shared_account']
        source['sources'][SID]['targets'][instrument] = '-8'
        for row in source['sources'][SID]['rows']:
            if row['instrument_id'] == instrument:
                row['quantity'] = '-8'
        for row in account['portfolio']['contributions'][SID]:
            if row['key']['instrument_id'] == instrument:
                row['quantity'] = '-8'
        source['sources'][SID]['metadata']['checkpoint_alignment']['signal'] = deepcopy(signal['signal'])
        source['shared']['bootstrap_signals'][SID] = deepcopy(signal['signal'])
        original = deepcopy(account)
        updated, revised, _, evidence = self.prepare(account, signal, config)
        self.assertEqual(revised['signal']['option_goals']['MO2612-C-6000'], '-4')
        self.assertNotIn('2026-11-26', revised['signal']['sent_days'])
        shared = updated['component_states']['shared_account']
        self.assertEqual(shared['sources'][SID]['targets'][instrument], '-4')
        self.assertEqual(shared['sources'][SID]['metadata']['checkpoint_alignment']['signal'], revised['signal'])
        self.assertEqual(shared['shared']['bootstrap_signals'][SID], revised['signal'])
        self.assertEqual(shared['portfolio_revision'], updated['portfolio']['revision'])
        self.assertEqual(shared['strategy_revisions'], updated['portfolio']['strategy_revisions'])
        self.assertEqual(updated['portfolio']['revision'], original['portfolio']['revision'] + 1)
        self.assertEqual(revised['revision'], signal['revision'])
        for name in ('positions', 'orders', 'ctp_drivers', 'order_budget'):
            self.assertEqual(updated[name], original[name])
        self.assertEqual(updated['component_states']['ctp_order_owners'], original['component_states']['ctp_order_owners'])
        self.assertEqual(evidence['target_adjustments']['business'][instrument], {'before': '-8', 'after': '-4'})
        self.assertEqual(set(evidence['target_adjustments']), {'business', 'source', 'source_rows', 'portfolio'})

    def test_real_three_attempt_stop_leaves_business_goal_but_can_rearm_zero_fill(self):
        from tests.test_option_nt_algo import OptionNTAlgoTests
        case = OptionNTAlgoTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        account, service, broker, port, runtime, strategy, _, _ = case.setup_strategy()
        for _ in range(10):
            if len(broker.sent) == 3:
                break
            case.tick(service, runtime)
            port.drain_events()
        strategy.stopping = True
        service.suspend_strategy('one')
        service.poll_stopping()
        port.drain_events()
        account.manager.save()
        payload = account.manager.repository.load().payload
        original = deepcopy(payload)
        business = import_module('demos.08_option_vega.vega_signal').OptionVegaCheckpoint(strategy).snapshot_state()
        self.assertEqual(len(broker.sent), 3)
        self.assertEqual(business['option_goals']['MO2612-C-6000'], '-8')
        self.assertFalse(payload['component_states']['nt_algo']['tasks'])
        self.assertFalse(payload['component_states']['strategy_buckets']['frozen'])
        self.assertEqual(payload['component_states']['shared_account']['sources']['one']['targets']['MO2612-C-6000.CFFEX'], '0')
        today = [row for row in payload['component_states']['ctp_order_owners']['records'].values()
                 if row['strategy_id'] == 'one' and row['day'] == '20261013']
        orders = {row['client_order_id']: row for rows in payload['orders'].values() for row in rows}
        changes = app.align_unfilled_targets(payload, business, strategy_id='one', today=today, orders=orders)
        self.assertEqual(business['option_goals']['MO2612-C-6000'], '0')
        self.assertEqual(changes, {'business': {'MO2612-C-6000.CFFEX': {'before': '-8', 'after': '0'}}})
        self.assertEqual(payload, original)  # 执行层已冻结；此路径仅调整业务目标。

    def test_unproven_target_mismatch_reports_layer_and_quantities(self):
        account, signal, config = self.fixtures()
        signal['signal']['future_goals']['IM2612'] = '3'
        with self.assertRaisesRegex(ValueError, '缺少今天已结束零成交委托的依据') as caught:
            self.prepare(account, signal, config)
        self.assertIn('business', str(caught.exception))
        self.assertIn('IM2612.CFFEX', str(caught.exception))
        self.assertIn('"before": "3"', str(caught.exception))
        self.assertIn('"after": "2"', str(caught.exception))

    def test_future_time_and_current_day_are_required(self):
        for kwargs in ({'now': NOW.replace(hour=14)}, {'now': NOW.replace(day=25)},
                       {'option_slot': '9:58'}, {'option_slot': '25:00'}, {'option_slot': '14:55'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.prepare(*self.fixtures(), **kwargs)

    def test_dry_run_writes_nothing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.files(root)
            original = [path.read_bytes() for path in paths]
            self.assertEqual(self.operate(paths)['status'], 'checked')
            self.assertEqual([path.read_bytes() for path in paths], original)
            self.assertEqual(len(list(root.iterdir())), 3)

    def test_apply_saves_valid_checksums_backups_and_single_day_receipt(self):
        with TemporaryDirectory() as directory:
            paths = self.files(Path(directory))
            original = [path.read_bytes() for path in paths]
            result = self.operate(paths, apply=True)
            self.assertEqual(result['status'], 'applied')
            self.assertEqual([Path(path).read_bytes() for path in result['backups']], original)
            account, signal = (JsonStateStore(path).load() for path in paths[:2])
            self.assertEqual((account.generation, signal.generation), (2, 2))
            self.assertEqual(account.payload['order_budget'], {'submitted': 12, 'limit': 200})
            self.assertEqual(signal.payload['signal']['sent_days'], ['2026-11-25'])
            self.assertEqual(json.loads(paths[2].read_text())['option_slot'], '13:40')
            after = [path.read_bytes() for path in paths]
            with self.assertRaisesRegex(ValueError, '今天已执行'):
                self.operate(paths, apply=True)
            self.assertEqual([path.read_bytes() for path in paths], after)

    def test_running_owner_lock_blocks_even_readonly_maintenance(self):
        with TemporaryDirectory() as directory:
            paths = self.files(Path(directory))
            with strategy_state_lock(str(paths[0])+'.direct-owner'), self.assertRaisesRegex(RuntimeError, '已有进程'):
                self.operate(paths, apply=True)

    def test_second_save_failure_rolls_back_first_save_with_original_bytes(self):
        with TemporaryDirectory() as directory:
            paths = self.files(Path(directory))
            original = [path.read_bytes() for path in paths]
            save = JsonStateStore.save
            def fail_signal(store, *args, **kwargs):
                if store.path == paths[1]:
                    raise OSError('fixture disk failure')
                return save(store, *args, **kwargs)
            with patch.object(JsonStateStore, 'save', new=fail_signal), self.assertRaises(OSError):
                self.operate(paths, apply=True)
            self.assertEqual([path.read_bytes() for path in paths], original)
            self.assertEqual(JsonStateStore(paths[0]).load().generation, 1)

    def test_concurrent_generation_change_is_not_overwritten_by_rollback(self):
        with TemporaryDirectory() as directory:
            paths = self.files(Path(directory))
            original_signal = paths[1].read_bytes()
            save = JsonStateStore.save
            def race(store, payload, **kwargs):
                if store.path == paths[0]:
                    other = deepcopy(store.load().payload)
                    other['external_change'] = True
                    save(store, other, **kwargs)
                return save(store, payload, **kwargs)
            with patch.object(JsonStateStore, 'save', new=race), self.assertRaises(ConcurrentStateWriteError):
                self.operate(paths, apply=True)
            self.assertTrue(JsonStateStore(paths[0]).load().payload['external_change'])
            self.assertEqual(paths[1].read_bytes(), original_signal)


if __name__ == '__main__':
    unittest.main()
