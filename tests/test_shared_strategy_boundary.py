"""底层只识别共享执行格式，不解释策略名称或扩展数据。"""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from tests import run_hybrid_live_offline as sdk_fixture
from bomber.framework.trader.execution.ipc import AccountExecutionProtocol
from bomber.framework.trader.persistence import JsonStateStore
from bomber.framework.trader.runtime.live.channels.ctp_shared import CtpSharedAccountLifecycle


class RestoreReached(Exception):
    pass


class SharedStrategyBoundaryTests(unittest.TestCase):
    def lifecycle(self, payload, converter, *, preserve_external=False):
        directory=TemporaryDirectory();self.addCleanup(directory.cleanup)
        store=JsonStateStore(Path(directory.name)/'state.json')
        saved=store.save(payload,expected_generation=0)
        before=store.path.read_bytes()
        manager=NS(repository=store,state_components={'session_identity':NS(identity={
            'trading_day':'20261103','existing_position_policy':'preserve_external'})})
        observed=[]
        def restore(*,payload_transform):
            transformed=payload_transform(deepcopy(saved.payload)) if payload_transform else deepcopy(saved.payload)
            observed.append(transformed)
            raise RestoreReached()
        manager.restore=restore
        lifecycle=object.__new__(CtpSharedAccountLifecycle)
        lifecycle.manager=manager
        lifecycle.expected_day='20261103'
        lifecycle.rollover_transform=converter
        lifecycle.preserve_external=preserve_external
        lifecycle._started=False
        return lifecycle,store,before,observed

    def test_arbitrary_legacy_business_format_is_converted_by_application_callback(self):
        old={'component_states':{'custom_application_state':{'positions':[1,2]}}}
        def convert(account,payload):
            payload['component_states']={'shared_account':{},'session_identity':{'identity':{'trading_day':'20261103'}}}
            return payload
        converter=Mock(side_effect=convert)
        lifecycle,store,before,observed=self.lifecycle(old,converter)
        with self.assertRaises(RestoreReached):lifecycle.start(resume=True)
        converter.assert_called_once()
        self.assertEqual(converter.call_args.args[0],lifecycle)
        self.assertEqual(converter.call_args.args[1]['component_states']['shared_account'],{})
        self.assertIn('shared_account',observed[0]['component_states'])
        self.assertEqual(store.path.read_bytes(),before)

    def test_legacy_format_requires_converter_and_does_not_modify_original(self):
        lifecycle,store,before,_=self.lifecycle({'component_states':{'unknown_business':{}}},None)
        with self.assertRaisesRegex(RuntimeError,'旧格式转换'):lifecycle.start(resume=True)
        self.assertEqual(store.path.read_bytes(),before)

    def test_current_shared_format_same_day_does_not_run_business_converter(self):
        converter=Mock(side_effect=AssertionError('unnecessary conversion'))
        current={'component_states':{'shared_account':{},'session_identity':{'identity':{'trading_day':'20261103'}}}}
        lifecycle,store,before,observed=self.lifecycle(current,converter)
        with self.assertRaises(RestoreReached):lifecycle.start(resume=True)
        converter.assert_not_called()
        self.assertEqual(observed,[current])
        self.assertEqual(store.path.read_bytes(),before)

    def test_current_shared_format_cross_day_uses_existing_callback(self):
        original={'component_states':{'shared_account':{},'session_identity':{'identity':{'trading_day':'20261102'}}}}
        converter=Mock(side_effect=lambda account,payload:payload)
        lifecycle,store,before,_=self.lifecycle(original,converter)
        with self.assertRaises(RestoreReached):lifecycle.start(resume=True)
        converter.assert_called_once()
        self.assertEqual(store.path.read_bytes(),before)

    def test_external_baseline_runs_after_generic_application_conversion(self):
        calls=[]
        def convert(account,payload):
            calls.append('application')
            payload['component_states']={'shared_account':{},'session_identity':{
                'identity':{'trading_day':'20261103','strategies':{}}}}
            return payload
        def baseline(account,payload):
            calls.append('baseline')
            self.assertIn('shared_account',payload['component_states'])
            return payload
        lifecycle,store,before,_=self.lifecycle({'component_states':{'custom_business_state':{}}},convert,
            preserve_external=True)
        with patch('bomber.framework.trader.runtime.live.channels.ctp_owned.restore_external_baseline',side_effect=baseline):
            with self.assertRaises(RestoreReached):lifecycle.start(resume=True)
        self.assertEqual(calls,['application','baseline'])
        self.assertEqual(store.path.read_bytes(),before)

    def test_generic_policy_migration_is_visible_to_cross_day_application_validator(self):
        original={'component_states':{'shared_account':{},'session_identity':{'identity':{
            'trading_day':'20261102','strategies':{}}}}}
        def convert(account,payload):
            self.assertEqual(payload['component_states']['session_identity']['identity']['existing_position_policy'],
                             'preserve_external')
            return payload
        lifecycle,store,before,_=self.lifecycle(original,convert,preserve_external=True)
        with patch('bomber.framework.trader.runtime.live.channels.ctp_owned.restore_external_baseline',
                   side_effect=lambda account,payload:payload):
            with self.assertRaises(RestoreReached):lifecycle.start(resume=True)
        self.assertEqual(store.path.read_bytes(),before)

    def test_explicit_restore_transform_takes_precedence_over_configured_converter(self):
        lifecycle,store,before,observed=self.lifecycle({'component_states':{'arbitrary':{}}},
            Mock(side_effect=AssertionError('unexpected converter')))
        explicit=Mock(return_value={'converted':True})
        with self.assertRaises(RestoreReached):lifecycle.start(resume=True,payload_transform=explicit)
        self.assertEqual(observed,[{'converted':True}])
        self.assertEqual(store.path.read_bytes(),before)

    def test_protocol_returns_all_metadata_for_current_source_only_as_independent_copy(self):
        sources={'first':{'revision':4,'metadata':{'application_state':{'batch':[1,2]},'version':'v1'}},
                 'second':{'revision':8,'metadata':{'private_state':'second only'}}}
        service=NS(client_id='account',account_id='fixture',execution_stop=None,
            snapshot_state=lambda:{'sources':sources},client=NS(is_armed=True),
            position_manager=NS(position=lambda *args:0,attribution=NS(working_quantity=lambda *args:0)),
            _ports={'first':NS(is_reconciled=True,configuration=lambda:{},routes={})})
        protocol=object.__new__(AccountExecutionProtocol)
        protocol.service=service;protocol.trading_day='20261103';protocol.journal=NS(ack={'first':0})
        result=protocol._state_locked('first')
        self.assertEqual(result['source_metadata'],sources['first']['metadata'])
        self.assertNotIn('private_state',result['source_metadata'])
        result['source_metadata']['application_state']['batch'].append(99)
        self.assertEqual(sources['first']['metadata']['application_state']['batch'],[1,2])
        sources.pop('first')
        self.assertEqual(protocol._state_locked('first')['source_metadata'],{})


if __name__=='__main__':
    unittest.main()
