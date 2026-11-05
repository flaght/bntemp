"""同日真实成交查询补回报；实际Driver、账本、持久化和共享journal，仅柜台为替身。"""
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from tests import test_shared_account as fixture


class CtpTradeRecoveryTests(unittest.TestCase):
    setUp = fixture.SharedAccountTests.setUp
    native_modules = fixture.SharedAccountTests.native_modules
    native_account = fixture.SharedAccountTests.native_account
    submit = fixture.SharedAccountTests.submit
    request = fixture.SharedAccountTests.request
    client = fixture.SharedAccountTests.client

    def journal(self, service):
        service.event_journal = import_module('bomber.framework.trader.execution.ipc').ExecutionEventJournal(list(service._ports))

    def stopped_with_missing_close(self, root, paired=False):
        if paired:
            self.instrument = fixture.fixtures.Instrument("p2701.DCE")
            self.rb = fixture.fixtures.Instrument("rb2701.SHFE")
        modules = self.native_modules()
        path = root/'account.json'
        options = dict(scoped=True, preserve_external=True, execution_mode='independent',
                       extra_instruments=(self.rb,), generic_close_venues=('DCE',)) if paired else {}
        old, service, broker, (one, _), _ = self.native_account(path, modules, **options)
        if paired:
            budget = import_module('bomber.framework.trader.execution.shared').SharedRiskBudget(2,50000)
            rb_port = service.strategy_client('rb', routes={'main':self.rb}, budget=budget)
            service.prices.update(self.rb,3000,self.now)
        self.journal(service)
        self.addCleanup(old.driver.stop)
        old.start(resume=False); service.start(); one.start(); old.authorize()
        self.submit(one, -2)
        broker.fill(broker.sent[-1]['OrderRef'])
        if paired:
            rb_port.start()
            rb_port.submit_targets(self.request_type('rb',1,'account',self.now,{self.rb:2},'DIRECT',
                metadata={'position_attribution':({'strategy_id':'rb','target_key':'main',
                          'instrument_id':str(self.rb),'quantity':'2'},)}))
            broker.fill(broker.sent[-1]['OrderRef'])
        self.now += 1
        self.submit(one, -1, revision=2)
        fields = broker.sent[-1]
        ref = fields['OrderRef']
        terminal = dict(broker.raw(ref), Direction=fields['Direction'], OrderStatus='0', OrderSubmitStatus='3',
            VolumeTotalOriginal=1, VolumeTraded=1, VolumeTotal=0)
        original = path.read_bytes()
        received = broker.on_trade
        broker.on_trade = lambda raw: None
        broker.fill(ref, 1)  # 柜台成交，原Driver没有收到回报，禁止按仓位差额猜测价格。
        broker.on_trade = received
        self.assertEqual(path.read_bytes(), original)
        old.driver.stop()
        account, target, transport, ports, _ = self.native_account(path, modules, **options)
        if paired:
            target.strategy_client('rb', routes={'main':self.rb}, budget=budget)
            target.prices.update(self.rb,3000,self.now)
        self.journal(target)
        self.addCleanup(account.driver.stop)
        transport.gross = dict(broker.gross)
        transport.queried_order_states = {f'CTP-9999-demo-20261013-{ref}': terminal}
        trade = dict(terminal, TradeID='ACTUAL-CLOSE', OrderSysID=terminal['OrderSysID'],
            TradingDay='20261013', TradeDate='20261013', TradeTime='10:14:59', OffsetFlag=fields['CombOffsetFlag'],
            Volume=1, Price=2999)
        self.trade_queries = 0
        def query():
            self.trade_queries += 1
            return (trade,)
        transport.query_trades = query
        return modules, path, original, account, target, transport, trade

    def test_missing_fill_uses_real_price_preserves_position_and_never_sends_order(self):
        with TemporaryDirectory() as directory:
            modules, path, original, account, service, broker, trade = self.stopped_with_missing_close(Path(directory))
            account.start(resume=True)
            self.assertEqual(service.position_manager.position('one','main'), -1)
            self.assertEqual(account.ledger.snapshot(self.instrument).short_total, 1)
            self.assertFalse(account.driver._orders or broker.sent or broker.canceled)
            recovered = [r for r in account.execution.backend.reports if r.filled_quantity]
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].fill_price, 2999)
            self.assertEqual(recovered[0].metadata['execution_origin'], 'ctp_trade_query_recovery')
            self.assertEqual(recovered[0].metadata['ctp_trade_time'], '10:14:59')
            self.assertEqual(self.trade_queries, 1)
            backups = list(Path(directory).glob('*.before-rollover-*.json'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            saved = account.manager.repository.load()
            self.assertTrue(all(row['status'] in {'FILLED','CANCELED','REJECTED'} for row in saved.payload['orders']['account']))
            journal_size = len(service.event_journal.events['one'])
            account.stop()
            again, target, other, _, _ = self.native_account(path, modules)
            self.journal(target)
            self.addCleanup(again.driver.stop)
            other.gross = dict(broker.gross)
            other.query_trades = lambda: self.fail('已补齐成交不得再次查询或回放')
            again.start(resume=True)
            self.assertEqual(target.position_manager.position('one','main'), -1)
            self.assertEqual(len(target.event_journal.events['one']), journal_size)
            self.assertFalse(other.sent or again.execution.backend.reports)
            again.stop()

    def test_p_close_recovery_keeps_rb_long_two_and_p_short_one_under_external_baseline(self):
        with TemporaryDirectory() as directory:
            _, _, _, account, service, broker, _ = self.stopped_with_missing_close(Path(directory), paired=True)
            account.start(resume=True)
            self.assertEqual(service.position_manager.position('one','main'), -1)
            self.assertEqual(service.position_manager.position('rb','main'), 2)
            self.assertEqual(account.ledger.snapshot(self.instrument).short_total, 1)
            self.assertEqual(account.ledger.snapshot(self.rb).long_total, 2)
            self.assertEqual(broker.gross[str(self.instrument)], (0,1))
            self.assertEqual(broker.gross[str(self.rb)], (2,0))
            self.assertFalse(broker.sent or broker.canceled)
            account.stop()

    def test_missing_trade_details_do_not_change_original_file(self):
        with TemporaryDirectory() as directory:
            _, path, original, account, _, broker, _ = self.stopped_with_missing_close(Path(directory))
            broker.query_trades = lambda: ()
            with self.assertRaisesRegex(RuntimeError, '真实成交明细不足'):
                account.start(resume=True)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(broker.sent)
            self.assertFalse(list(Path(directory).glob('*.before-rollover-*')))

    def test_wrong_trade_identity_price_quantity_or_day_never_guesses_a_fill(self):
        for changed in ({'Price':0}, {'Volume':2}, {'InstrumentID':'OTHER'}, {'OrderSysID':'OTHER'},
                        {'OffsetFlag':'0'}, {'TradingDay':'20261014'}):
            with self.subTest(changed=changed), TemporaryDirectory() as directory:
                _, path, original, account, _, broker, trade = self.stopped_with_missing_close(Path(directory))
                broker.query_trades = lambda: (dict(trade, **changed),)
                with self.assertRaises(RuntimeError): account.start(resume=True)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(broker.sent)

    def test_valid_trade_but_position_mismatch_keeps_original_checkpoint(self):
        with TemporaryDirectory() as directory:
            _, path, original, account, _, broker, _ = self.stopped_with_missing_close(Path(directory))
            broker.gross = {str(self.instrument):(0,2)}
            with self.assertRaises(RuntimeError): account.start(resume=True)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(broker.sent)

    def test_query_and_live_callback_with_same_trade_id_are_applied_once(self):
        with TemporaryDirectory() as directory:
            _, _, _, account, service, broker, trade = self.stopped_with_missing_close(Path(directory))
            def query():
                broker.on_trade(trade)
                return (trade, dict(trade))
            broker.query_trades = query
            account.start(resume=True)
            self.assertEqual(service.position_manager.position('one','main'), -1)
            self.assertEqual(len([r for r in account.execution.backend.reports if r.filled_quantity]), 1)
            self.assertFalse(broker.sent)
            account.stop()

    def test_confirmed_cancel_without_new_fill_does_not_query_or_fabricate_trade(self):
        with TemporaryDirectory() as directory:
            _, _, _, account, service, broker, _ = self.stopped_with_missing_close(Path(directory))
            raw = next(iter(broker.queried_order_states.values()))
            raw.update(OrderStatus='5', VolumeTraded=0, VolumeTotal=1)
            broker.gross = {str(self.instrument):(0,2)}
            broker.query_trades = lambda: self.fail('没有新增成交，不应查询或补造成交')
            account.start(resume=True)
            self.assertEqual(service.position_manager.position('one','main'), -2)
            self.assertFalse([r for r in account.execution.backend.reports if r.filled_quantity])
            self.assertFalse(account.driver._orders)
            account.stop()


if __name__ == '__main__': unittest.main()
