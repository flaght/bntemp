"""按具体订单去重撤单；使用真实执行账本和离线柜台替身。"""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests import test_shared_account as fixture


class OrderCancellationTests(unittest.TestCase):
    setUp = fixture.SharedAccountTests.setUp
    native_modules = fixture.SharedAccountTests.native_modules
    native_account = fixture.SharedAccountTests.native_account
    submit = fixture.SharedAccountTests.submit
    request = fixture.SharedAccountTests.request
    client = fixture.SharedAccountTests.client

    def make_account(self, *, mode="independent", extra_instruments=()):
        directory = self.enterContext(TemporaryDirectory())
        account, service, transport, ports, _ = self.native_account(
            Path(directory) / "account.json", self.native_modules(),
            execution_mode=mode, extra_instruments=extra_instruments,
        )
        self.addCleanup(account.driver.stop)
        account.start(resume=False)
        service.start()
        for port in ports:
            port.register_execution_event_handler(lambda event: None)
            port.start()
        account.authorize()
        return account, service, transport, ports

    def test_empty_cancel_does_not_block_canceling_later_order(self):
        _, _, transport, (one, _) = self.make_account()
        one.cancel_strategy("one")
        self.submit(one, 1)
        first = transport.sent[-1]["OrderRef"]
        self.submit(one, -1, revision=2)
        self.assertEqual(transport.canceled, [first])
        self.assertNotIn(first, transport.active)

    def test_next_order_does_not_inherit_previous_cancel_marker(self):
        _, _, transport, (one, _) = self.make_account()
        self.submit(one, 1)
        first = transport.sent[-1]["OrderRef"]
        self.submit(one, -1, revision=2)
        second = transport.sent[-1]["OrderRef"]
        self.assertNotEqual(first, second)
        self.submit(one, 0, revision=3)
        self.assertEqual(transport.canceled, [first, second])
        self.assertFalse(transport.active)

    def test_pending_cancel_is_sent_once_and_next_order_can_be_canceled(self):
        _, service, transport, (one, _) = self.make_account()
        confirm = transport.cancel_order
        requested = []
        transport.cancel_order = requested.append
        self.submit(one, 1)
        first = transport.sent[-1]["OrderRef"]
        one.cancel_strategy("one")
        one.cancel_strategy("one")
        self.submit(one, -1, revision=2)
        self.assertEqual(requested, [first])
        self.assertEqual(len(transport.sent), 1)
        confirm(first)
        service.continue_targets("one", self.instrument, self.now)
        second = transport.sent[-1]["OrderRef"]
        one.cancel_strategy("one")
        one.cancel_strategy("one")
        self.assertEqual(requested, [first, second])
        confirm(second)
        self.assertFalse(transport.active)

    def test_new_order_on_another_instrument_does_not_recancel_pending_order(self):
        extra = type(self.instrument).from_str("HC.SHFE")
        directory = self.enterContext(TemporaryDirectory())
        account, service, transport, _, _ = self.native_account(
            Path(directory) / "account.json", self.native_modules(),
            scoped=True, execution_mode="independent", extra_instruments=(extra,),
        )
        self.addCleanup(account.driver.stop)
        from bomber.framework.trader.execution.shared import SharedRiskBudget

        pair = service.strategy_client(
            "pair", routes={"a": self.instrument, "b": extra},
            budget=SharedRiskBudget(5, 50000),
        )
        account.start(resume=False)
        service.start()
        pair.start()
        account.authorize()
        service.prices.update(extra, 3000, self.now)

        def submit_pair(a, b, revision):
            request = self.request(service.client_id, strategy="pair", timestamp=self.now, revision=revision)
            pair.submit_targets(replace(request, targets={self.instrument: a, extra: b}, metadata={
                "position_attribution": tuple(
                    dict(strategy_id="pair", target_key=key, instrument_id=str(instrument), quantity=str(quantity))
                    for key, instrument, quantity in (("a", self.instrument, a), ("b", extra, b))
                ),
            }))

        confirm = transport.cancel_order
        requested = []
        transport.cancel_order = requested.append
        submit_pair(1, 0, 1)
        first = transport.sent[-1]["OrderRef"]
        pair.cancel_strategy("pair")
        submit_pair(1, 1, 2)
        second = transport.sent[-1]["OrderRef"]
        self.assertNotEqual(first, second)
        pair.cancel_strategy("pair")
        pair.cancel_strategy("pair")
        self.assertEqual(requested, [first, second])
        confirm(first)
        confirm(second)

    def test_shared_order_is_canceled_once_across_allocation_owners(self):
        account, _, transport, (one, two) = self.make_account(mode="netted")
        original = account.client.planner
        account.client.planner = type("Paused", (), {"plan": lambda *_: ()})()
        self.submit(one, 2)
        account.client.planner = original
        self.submit(two, 3)
        first = transport.sent[-1]["OrderRef"]
        self.assertEqual(transport.sent[-1]["VolumeTotalOriginal"], 5)
        transport.fill(first, 2)  # one已全成交，仍须能撤掉涉及它的共享母单。
        confirm = transport.cancel_order
        requested = []
        transport.cancel_order = requested.append
        one.cancel_strategy("one")
        two.cancel_strategy("two")
        one.cancel_strategy("one")
        self.assertEqual(requested, [first])
        confirm(first)

    def test_cancel_final_waits_for_late_trade_before_replanning_remainder(self):
        account, service, transport, (one, _) = self.make_account()
        transport.cancel_order = lambda ref: None
        self.submit(one, 2)
        first = transport.sent[-1]["OrderRef"]
        one.cancel_strategy("one")
        transport.on_order({**transport.raw(first), "OrderStatus": "5", "VolumeTraded": 1})
        service.continue_targets("one", self.instrument, self.now)
        self.assertEqual(len(transport.sent), 1)
        self.assertIn(first, account.driver._orders)
        transport.fill(first, 1)
        transport.active.pop(first)
        service.continue_targets("one", self.instrument, self.now)
        self.assertEqual(transport.sent[-1]["VolumeTotalOriginal"], 1)
        transport.fill(transport.sent[-1]["OrderRef"])
        self.assertEqual(service.position_manager.position("one", "main"), 2)

    def test_poll_does_not_follow_order_after_timeout_or_price_move(self):
        _, service, transport, (one, _) = self.make_account()
        self.submit(one, 2)
        first = transport.sent[-1]["OrderRef"]
        self.now += 60_000_000_000
        service.prices.update(self.instrument, 3030, self.now)
        service.poll()
        self.assertIn(first, transport.active)
        self.assertFalse(transport.canceled)
        self.assertEqual(len(transport.sent), 1)


if __name__ == "__main__":
    unittest.main()
