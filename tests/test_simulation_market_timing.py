"""真实网关/Backend的时序回归；SDK宿主替身只记录提交，不模拟原生撮合。"""

from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PREFIX = "bomber.framework.trader.execution"


class Bar:
    def __init__(self, instrument_id, ts_event, price=100):
        # BarType在真实SDK中可哈希。
        self.bar_type = _BarType(instrument_id)
        self.ts_event = ts_event
        self.price = price


class _BarType:
    def __init__(self, instrument_id, period="1-MINUTE"):
        self.instrument_id = instrument_id
        self.period = period

    def __hash__(self):
        return hash((self.instrument_id, self.period))

    def __eq__(self, other):
        return (isinstance(other, _BarType) and self.instrument_id == other.instrument_id
                and self.period == other.period)


class QuoteTick:
    def __init__(self, instrument_id, ts_event, price=100):
        self.instrument_id, self.ts_event, self.price = instrument_id, ts_event, price


class TradeTick(QuoteTick):
    pass


class Host:
    """记录订单在哪个原生数据回调提交；不生成虚构成交。"""
    def __init__(self, config):
        self.now = 0
        self.serial = 0
        self.submissions = []
        self.subscriptions = []
        self.clock = NS(timestamp_ns=lambda: self.now)
        instrument = NS(make_qty=lambda qty: qty, make_price=lambda price: price)
        self.cache = NS(instrument=lambda key: instrument, order=lambda key: None)
        self.order_factory = NS(market=self.make_order, limit=self.make_order)

    def make_order(self, **kwargs):
        self.serial += 1
        return NS(client_order_id=f"O-{self.serial}", **kwargs)

    def submit_order(self, order):
        self.submissions.append((self.now, order))

    def subscribe_bars(self, key):
        self.subscriptions.append((Bar, key))

    def subscribe_quote_ticks(self, key):
        self.subscriptions.append((QuoteTick, key))

    def subscribe_trade_ticks(self, key):
        self.subscriptions.append((TradeTick, key))

    def on_bar(self, event):
        pass

    def on_quote_tick(self, event):
        pass

    def on_trade_tick(self, event):
        pass


def module(name, **attributes):
    result = ModuleType(name)
    result.__dict__.update(attributes)
    return result


def load(name, relative_path):
    spec = spec_from_file_location(name, ROOT / relative_path)
    result = module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


@contextmanager
def components():
    """加载实际Python契约、网关和Backend，仅替换无法本机导入的SDK边界。"""
    stubs = {
        "bomber.framework.market.basic.base": module("base", InstrumentId=str),
        "bomber.model.data": module("data", Bar=Bar, QuoteTick=QuoteTick, TradeTick=TradeTick),
        "bomber.model.enums": module("enums", OrderSide=NS(BUY="BUY", SELL="SELL")),
        "bomber.model.events": module("events", **{name: object for name in (
            "OrderAccepted", "OrderCanceled", "OrderDenied", "OrderExpired",
            "OrderFilled", "OrderRejected")}),
        "bomber.model.identifiers": module("identifiers", ClientOrderId=str),
        "bomber.trading.config": module("config", StrategyConfig=NS),
        "bomber.trading.strategy": module("strategy", Strategy=Host),
        "bomber.backtest.config": module("config", BacktestEngineConfig=NS),
        "bomber.backtest.engine": module("engine", BacktestEngine=object),
        PREFIX + ".ports": module("ports", VenueSimulationProfilePort=object),
    }
    with patch.dict(sys.modules, stubs):
        contracts = load(PREFIX + ".contracts", "bomber/framework/trader/execution/contracts.py")
        gateway = load(PREFIX + ".simulation.gateway", "bomber/framework/trader/execution/simulation/gateway.py")
        backend = load(PREFIX + ".simulation.backend", "bomber/framework/trader/execution/simulation/backend.py")
        state = load(PREFIX + ".order_state", "bomber/framework/trader/execution/order_state.py")
        yield NS(contracts=contracts, gateway=gateway, backend=backend, state=state)


class EngineHost:
    def __init__(self):
        self.gateway = None
        self.started = False

    def add_strategy(self, gateway):
        self.gateway = gateway

    def add_data(self, events):
        self.events = events

    def run(self, streaming):
        if not self.started:
            self.started = True
            self.gateway.on_start()
        for event in self.events:
            self.gateway.now = event.ts_event
            if isinstance(event, Bar):
                self.gateway.on_bar(event)
            elif isinstance(event, TradeTick):
                self.gateway.on_trade_tick(event)
            else:
                self.gateway.on_quote_tick(event)

    def clear_data(self):
        self.events = []

    def end(self):
        self.gateway.on_stop()

    def get_result(self):
        return NS(total_orders=len(self.gateway.submissions))


class SimulationMarketTimingTests(unittest.TestCase):
    def setUp(self):
        context = components()
        self.sdk = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.engine = EngineHost()
        self.backend = self.sdk.backend.NautilusSimExecutionBackend("sim", engine=self.engine,
                                                                  execution_timing="next_market")

    def order(self, instrument="IF2603.CFFEX", request_ts=100, strategy="cta"):
        contracts = self.sdk.contracts
        intent = contracts.OrderIntent(strategy_id=strategy, backend_id="sim",
            instrument_id=instrument, side=contracts.OrderSide.SELL, quantity=1,
            metadata={"request_ts": request_ts})
        self.backend.submit_order(intent)
        return intent

    def event(self, instrument, timestamp, kind=Bar):
        self.backend.process_market_event(kind(instrument, timestamp))

    def test_other_contracts_and_same_timestamp_cannot_release_orders(self):
        self.event("IC2603.CFFEX", 100)
        self.order()
        self.order("IH2603.CFFEX")
        self.event("IF2603.CFFEX", 100)
        self.event("IH2603.CFFEX", 100)
        self.event("IC2603.CFFEX", 101)
        self.assertEqual(self.engine.gateway.submissions, [])
        self.event("IF2603.CFFEX", 101)
        self.assertEqual([(ts, order.instrument_id) for ts, order in self.engine.gateway.submissions],
                         [(101, "IF2603.CFFEX")])
        self.event("IH2603.CFFEX", 102)
        self.assertEqual(self.engine.gateway.submissions[-1][0], 102)

    def test_wait_two_own_bars_ignores_other_contracts_and_duplicate_timestamps(self):
        self.backend = self.sdk.backend.NautilusSimExecutionBackend("sim", engine=self.engine,
            execution_timing="next_market", execution_delay_bars=2)
        self.event("IC2603.CFFEX", 100)
        self.order()
        self.event("IF2603.CFFEX", 100)
        self.event("IC2603.CFFEX", 101)
        self.event("IF2603.CFFEX", 101)  # 第一根。
        self.event("IF2603.CFFEX", 101)  # 重复事件不能算作第二根。
        self.event("IH2603.CFFEX", 102)
        self.assertEqual(self.engine.gateway.submissions, [])
        self.event("IF2603.CFFEX", 103)  # 缺少102，第二根实际到达时才放行。
        self.assertEqual(len(self.engine.gateway.submissions), 1)
        self.assertEqual(self.engine.gateway.submissions[0][0], 103)

    def test_each_order_counts_bars_from_its_own_submission_time(self):
        self.backend = self.sdk.backend.NautilusSimExecutionBackend("sim", engine=self.engine,
            execution_timing="next_market", execution_delay_bars=2)
        self.event("IF2603.CFFEX", 100)
        self.order(strategy="a")
        self.event("IF2603.CFFEX", 101)
        self.order(request_ts=101, strategy="b")
        self.event("IF2603.CFFEX", 102)
        self.assertEqual(len(self.engine.gateway.submissions), 1)
        self.event("IF2603.CFFEX", 103)
        self.assertEqual([ts for ts, _ in self.engine.gateway.submissions], [102, 103])

    def test_end_before_required_bar_count_cancels_unsubmitted_intent(self):
        self.backend = self.sdk.backend.NautilusSimExecutionBackend("sim", engine=self.engine,
            execution_timing="next_market", execution_delay_bars=3)
        self.event("IF2603.CFFEX", 100)
        self.order()
        self.event("IF2603.CFFEX", 101)
        self.event("IF2603.CFFEX", 102)
        self.backend.finish()
        self.assertEqual(self.engine.gateway.submissions, [])
        self.assertEqual(len(self.backend.reports), 1)
        self.assertIs(self.backend.reports[0].report_type,
                      self.sdk.contracts.ExecutionReportType.CANCELED)

    def test_same_timestamp_ordering_does_not_change_release(self):
        for order in (("IC", "IF", "IH"), ("IH", "IF", "IC")):
            with self.subTest(order=order), components() as sdk:
                engine = EngineHost()
                backend = sdk.backend.NautilusSimExecutionBackend("sim", engine=engine,
                                                                 execution_timing="next_market")
                backend.process_market_event(Bar(order[0], 100))
                backend.submit_order(sdk.contracts.OrderIntent(strategy_id="cta", backend_id="sim",
                    instrument_id="IF", side="SELL", quantity=1, metadata={"request_ts": 100}))
                for symbol in order[1:]:
                    backend.process_market_event(Bar(symbol, 100))
                self.assertEqual(engine.gateway.submissions, [])
                backend.process_market_event(Bar("IF", 101))
                self.assertEqual(len(engine.gateway.submissions), 1)
                self.assertEqual(engine.gateway.submissions[0][0], 101)

    def test_clock_and_signal_time_both_bound_release(self):
        self.event("IF2603.CFFEX", 200)
        self.order(request_ts=100)  # 延迟信号不能回用提交时已有的行情。
        self.event("IF2603.CFFEX", 200)
        self.assertEqual(self.engine.gateway.submissions, [])
        self.event("IF2603.CFFEX", 201)
        self.order(request_ts=300)
        self.event("IF2603.CFFEX", 300)
        self.assertEqual(len(self.engine.gateway.submissions), 1)
        self.event("IF2603.CFFEX", 301)
        self.assertEqual(len(self.engine.gateway.submissions), 2)

    def test_prestart_order_waits_for_strictly_later_own_event(self):
        self.order()
        self.event("IF2603.CFFEX", 100)
        self.assertEqual(self.engine.gateway.submissions, [])
        self.event("IF2603.CFFEX", 101)
        self.assertEqual(len(self.engine.gateway.submissions), 1)

    def test_quote_and_trade_events_enforce_the_same_time_boundary(self):
        for timestamp, kind in ((100, QuoteTick), (200, TradeTick)):
            with self.subTest(kind=kind):
                self.event("IF2603.CFFEX", timestamp, kind)
                self.order(request_ts=timestamp)
                self.event("IF2603.CFFEX", timestamp, kind)
                count = len(self.engine.gateway.submissions)
                self.event("IF2603.CFFEX", timestamp + 1, kind)
                self.assertEqual(len(self.engine.gateway.submissions), count + 1)

    def test_end_cancels_waiting_intents_without_native_submission(self):
        self.event("IC2603.CFFEX", 100)
        self.order()
        self.backend.finish()
        self.assertEqual(self.engine.gateway.submissions, [])
        self.assertEqual(len(self.backend.reports), 1)
        report = self.backend.reports[0]
        self.assertIs(report.report_type, self.sdk.contracts.ExecutionReportType.CANCELED)
        self.assertEqual(report.order_quantity, 1)
        self.assertEqual(report.filled_quantity, 0)
        states = self.sdk.state.OrderReportStateMachine("sim")
        update = states.apply(report)
        self.assertEqual((update.fill_delta, update.release_delta), (0, 1))
        self.assertFalse(states.apply(report).applied)

    def test_strategy_cancel_releases_only_its_waiting_intents(self):
        self.event("IC2603.CFFEX", 100)
        self.order(strategy="a")
        self.order(strategy="b")
        self.backend.cancel_strategy("a")
        self.event("IF2603.CFFEX", 101)
        self.assertEqual(len(self.backend.reports), 1)
        self.assertEqual(self.backend.reports[0].metadata["strategy_id"], "a")
        self.assertEqual(len(self.engine.gateway.submissions), 1)

    def test_option_expiry_cancel_keeps_other_contract_pending(self):
        self.event("IC2603.CFFEX", 100)
        self.order("IF2603.CFFEX")
        self.order("IH2603.CFFEX")
        self.engine.gateway.cancel_pending_orders(instrument_id="IF2603.CFFEX", reason="expired")
        self.event("IH2603.CFFEX", 101)
        self.assertEqual(len(self.backend.reports), 1)
        self.assertEqual(str(self.backend.reports[0].instrument_id), "IF2603.CFFEX")
        self.assertEqual(len(self.engine.gateway.submissions), 1)
        self.assertEqual(self.engine.gateway.submissions[0][1].instrument_id, "IH2603.CFFEX")

    def test_last_trading_cutoff_uses_current_engine_clock_during_callbacks(self):
        self.event("IF2603.CFFEX", 100)
        self.backend._option_last_trade_times["IF2603.CFFEX"] = 150
        self.engine.gateway.now = 150  # 原生定时器先推进时钟，尚未返回 market_event。
        with self.assertRaisesRegex(RuntimeError, "past last trading time"):
            self.order()

    def test_native_is_the_backend_default_and_does_not_defer_submission(self):
        backend = self.sdk.backend.NautilusSimExecutionBackend("sim", engine=self.engine)
        backend.process_market_event(Bar("IC2603.CFFEX", 100))
        backend.submit_order(self.sdk.contracts.OrderIntent(strategy_id="cta", backend_id="sim",
            instrument_id="IF2603.CFFEX", side="SELL", quantity=1, metadata={"request_ts": 100}))
        self.assertEqual(backend.execution_timing, "native")
        self.assertEqual(backend.execution_delay_bars, 0)
        self.assertEqual(len(self.engine.gateway.submissions), 1)
        self.assertEqual(self.engine.gateway.submissions[0][0], 100)
        self.assertEqual(self.engine.gateway.subscriptions, [])

    def test_invalid_timing_is_rejected(self):
        with self.assertRaises(ValueError):
            self.sdk.backend.NautilusSimExecutionBackend("sim", engine=self.engine,
                                                        execution_timing="next_open")

    def test_delay_validation_rejects_invalid_counts_and_mode_combinations(self):
        for mode, delay in (("native", 1), ("next_market", 0), ("next_market", -1),
                            ("next_market", True), ("next_market", 1.5)):
            with self.subTest(mode=mode, delay=delay), self.assertRaises(ValueError):
                self.sdk.backend.NautilusSimExecutionBackend("sim", engine=self.engine,
                    execution_timing=mode, execution_delay_bars=delay)

    def test_multiple_bar_delay_rejects_tick_streams(self):
        backend = self.sdk.backend.NautilusSimExecutionBackend("sim", engine=self.engine,
            execution_timing="next_market", execution_delay_bars=2)
        with self.assertRaisesRegex(ValueError, "只支持Bar"):
            backend.process_market_event(QuoteTick("IF2603.CFFEX", 100))

    def test_multiple_bar_delay_rejects_mixed_bar_periods_for_one_instrument(self):
        backend = self.sdk.backend.NautilusSimExecutionBackend("sim", engine=self.engine,
            execution_timing="next_market", execution_delay_bars=2)
        backend.process_market_event(Bar("IF2603.CFFEX", 100))
        event = Bar("IF2603.CFFEX", 105)
        event.bar_type = _BarType("IF2603.CFFEX", "5-MINUTE")
        with self.assertRaisesRegex(ValueError, "一种Bar周期"):
            backend.process_market_event(event)

    def test_next_market_cannot_silently_fall_back_in_batch_mode(self):
        with self.assertRaisesRegex(RuntimeError, "UnifiedHistoricalRuntime"):
            self.backend.run()


if __name__ == "__main__":
    unittest.main()
