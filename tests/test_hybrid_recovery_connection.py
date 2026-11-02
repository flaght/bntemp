"""预检关闭后的恢复查询：真实Lifecycle/Driver/Transport，仅SDK为无网络假API。"""
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest

from tests import test_hybrid_sizing_restore as sizing_fixture
from tests import test_hybrid_continuous as fixture
from tests.run_p4_ctp_td_transport import FakeTdApi
from bomber.framework.trader.execution.ctp.td_transport import CtpTdApiTransport
from bomber.framework.trader.execution.ctp.query_errors import CtpQueryRejected
from bomber.framework.trader.runtime.live.channels.ctp_recovery import validate_ctp_account

live=fixture.live


class RecoveryConnectionTests(unittest.TestCase):
    def setUp(self):
        self.f=sizing_fixture.SizingRestoreTests('runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def exercise(self, *, manual=False, reject_positions=False, wrong_day=False, day='20261028'):
        if manual:
            self.f.seed_nonflat()
        apis=[]
        class Api(FakeTdApi):
            def __init__(api):
                super().__init__()
                api.number=len(apis)
                apis.append(api)
            def reqUserLogin(api,data,reqid):
                api.request_names.append('login')
                actual_day='20261029' if wrong_day and api.number==1 else day
                api.onRspUserLogin(dict(BrokerID='9999',UserID='fixture',TradingDay=actual_day,MaxOrderRef='17'),{},reqid,True)
                return 0
            def reqSettlementInfoConfirm(api,data,reqid):
                api.request_names.append('settlement')
                api.onRspSettlementInfoConfirm(dict(BrokerID='9999',InvestorID='fixture'),{},reqid,True)
                return 0
            def reqQryTradingAccount(api,data,reqid):
                api.request_names.append('account')
                api.onRspQryTradingAccount(dict(BrokerID='9999',AccountID='fixture',CurrencyID='CNY',
                    Balance=100000,Available=80000,CurrMargin=0),{},reqid,True)
                return 0
            def reqQryInvestorPosition(api,data,reqid):
                api.request_names.append('positions')
                if reject_positions and api.number==1:
                    return -2
                api.onRspQryInvestorPosition({}, {}, reqid, True)
                return 0
            def reqQryOrder(api,data,reqid):
                api.request_names.append('orders')
                api.onRspQryOrder({}, {}, reqid, True)
                return 0
            def reqOrderInsert(api,*args):
                raise AssertionError('恢复测试禁止发单')
            def reqOrderAction(api,*args):
                raise AssertionError('恢复测试禁止撤单')
        transport=CtpTdApiTransport(client_id=live.CLIENT_ID,account_id='fixture',investor_id='fixture',
            broker_id='9999',front='tcp://fake:1234',password='fake',app_id='fake',auth_code='fake',
            td_api_base=Api,flow_path=str(self.f.f.root/'td'),timeout_seconds=.05)
        lifecycle=live.HybridSessionLifecycle(transport,md_front=None,orders=True,resume=True,
            state_file=self.f.f.path,expected_trading_day=day,account_reconciliation='off')
        lifecycle.confirm_manual_flat=manual
        lifecycle.window=NS() if day!='20261028' else None
        self.addCleanup(lifecycle.driver.stop)
        market=live.MarketSourceConfig()  # 与原检查点保持同一行情身份；TD核验独立于MD通道。
        reference_session,_,_=self.f.f.session(day)
        with ExitStack() as resources, redirect_stdout(StringIO()):
            context=lifecycle.prepare(resources)
            self.assertTrue(transport._ready)
            lifecycle.finish_preparation(context)
            self.assertFalse(transport._ready)
            self.assertTrue(apis[0].exited)
            with patch.object(fixture.adapter,'MizarWorkflowAdapter',fixture.Predictor):
                session=live.assemble(NS(references=reference_session.references,calendar=self.f.f.cal,
                    upstream=fixture.Upstream('recovery-connection')),lifecycle,
                    product='RB',release_path=self.f.f.release,directions_file=self.f.f.directions,
                    signal_config=self.f.f.signal,market_config=market,mode='simnow',
                    max_contracts=2,entry_contracts=2,state_file=self.f.f.path,resume=True,
                    execution_policy=fixture.ExecutionPolicy('off','fills'))
            self.assertFalse(transport._ready)
            original=self.f.f.path.read_bytes()
            stage=lifecycle.driver.stage_recovery
            def stage_while_disconnected(checkpoint):
                self.assertFalse(transport._ready)
                self.assertIsNone(transport._api)
                stage(checkpoint)
            if reject_positions or wrong_day:
                expected=CtpQueryRejected if reject_positions else RuntimeError
                with self.assertRaises(expected):
                    lifecycle.restore_session(session,context)
                self.assertEqual(self.f.f.path.read_bytes(),original)
                self.assertFalse(transport._ready)
                self.assertTrue(all(api.exited for api in apis))
                self.assertEqual(len(apis),2)  # 一次明确的查询失败，不自动重连重试。
                self.assertFalse(list(self.f.f.root.glob('*.before-rollover-*')))
                return
            with patch.object(lifecycle.driver,'stage_recovery',side_effect=stage_while_disconnected):
                lifecycle.restore_session(session,context)
            self.assertEqual(self.f.f.path.read_bytes(),original)
            self.assertEqual(len(apis),2)
            self.assertEqual(apis[1].request_names[:4],['create','auth','login','settlement'])
            self.assertIn('positions',apis[1].request_names)
            self.assertTrue(apis[1].exited)
            self.assertFalse(transport._ready)
            lifecycle.validate_restored_session(session,day)
            session.client.start()
            try:
                self.assertTrue(transport._ready)
                self.assertFalse(session.client.is_armed)
                validate_ctp_account(session,transport,session.recovery_binding)
                session.manager.commit_restore()
            finally:
                session.client.stop()
            self.assertEqual(len(apis),3)  # 预检、恢复只读核验、正式执行会话。
            self.assertTrue(all(api.exited for api in apis))
            backup=next(self.f.f.root.glob('*.before-rollover-*.json'))
            self.assertEqual(backup.read_bytes(),original)
            self.assertFalse(session.strategy.holdings.active)
            self.assertEqual((session.strategy.config.max_contracts,session.strategy.config.max_entry_contracts),(2,2))

    def test_resize_reconnects_for_readonly_query_after_preflight_was_closed(self):
        self.exercise()

    def test_manual_flat_reconnects_then_stages_and_starts_final_session(self):
        self.exercise(manual=True)

    def test_manual_flat_rollover_reuses_readonly_connection_and_stages_disconnected(self):
        self.exercise(manual=True,day='20261029')

    def test_rejected_query_closes_connection_and_preserves_checkpoint(self):
        self.exercise(manual=True,reject_positions=True)

    def test_changed_td_is_not_accepted_for_readonly_flat_verification(self):
        self.exercise(manual=True,wrong_day=True)


if __name__=='__main__':
    unittest.main()
