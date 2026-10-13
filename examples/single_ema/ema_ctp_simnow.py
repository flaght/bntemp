#!/usr/bin/env python3
"""SimNow EMA受控报单：限时验收或单交易日持续运行。"""
from __future__ import annotations

import argparse
import os
import time
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from bomber.framework.market.basic.base import DataType, InstrumentId, InstrumentMeta
from bomber.framework.market.stream import TradeTickBarFeed
from bomber.framework.market.stream.ctp import CtpLiveDataFeed, CtpMdConfig
from bomber.framework.trader import (ControlledLiveExecutionClient, DataBinding, ExecutionRoute,
                      MarketReferencePriceStore, NautilusLiveExecutionBackend,
                      PositionManager, PreTradeRiskManager, RiskLimits, RuntimeMode,
                      UnifiedStrategyRunner)
from bomber.framework.trader.execution.ctp import (CtpLimitPlanner, CtpExecutionAccounting,
                                    CtpNativeTraderDriver, CtpPositionLedger,
                                    CtpTdApiTransport)
from examples.single_ema.strategies import EmaCrossConfig, EmaCrossTargetStrategy

load_dotenv(Path(__file__).resolve().parents[2] / '.env')


def required(name):
    value = os.getenv(name)
    if not value:
        raise SystemExit(f'缺少环境变量: {name}')
    return value


def replay_session_ready(driver, feed, expected_day, *, now_ns=None):
    """回放模式仅在MD与TD交易日一致且原始行情持续到达时授权。"""
    received_ns = feed.latest_receive_monotonic_ns
    age_ns = None
    if received_ns is not None:
        age_ns = (time.monotonic_ns() if now_ns is None else now_ns) - received_ns
    return bool(
        driver.is_simnow_session
        and driver.trading_day == expected_day
        and feed.latest_trading_day == expected_day
        and age_ns is not None
        and 0 <= age_ns <= 10_000_000_000
    )


def occupied_positions(transport):
    return {
        key: amounts for key, amounts in transport.query_gross_positions().items()
        if any(amounts)
    }


def build_runner(args):
    if not args.enable_orders or not args.confirm_simnow:
        raise SystemExit('SimNow订单入口须同时指定 --enable-orders --confirm-simnow；只看行情请用 ema_ctp_live.py')
    if required('CTP_BROKER_ID') != '9999':
        raise SystemExit('SimNow报单仅允许BrokerID=9999')
    investor = required('CTP_ACCOUNT_ID')
    symbol = args.symbol or required('CTP_SYMBOL')
    if args.exchange.upper() not in {'SHFE', 'INE'}:
        raise SystemExit('首版SimNow EMA订单只支持SHFE/INE明确平今平昨规则')
    instrument = InstrumentId.from_str(f'{symbol}.{args.exchange.upper()}')
    multiplier = Decimal(args.multiplier)
    tick_size = Decimal(args.price_increment)
    if (args.fast <= 0 or args.slow <= args.fast or Decimal(args.quantity) <= 0
            or Decimal(args.quantity) != Decimal(args.quantity).to_integral_value()
            or multiplier <= 0 or tick_size <= 0 or args.limit_offset_ticks < 0):
        raise SystemExit('EMA周期、手数、合约乘数、价格步长或限价偏移无效')
    front = required('CTP_TD_ADDRESS')
    md_front = required('CTP_MD_ADDRESS')
    transport = CtpTdApiTransport(
        client_id='ctp-simnow', account_id=investor,
        front=front, broker_id='9999', investor_id=investor,
        password=required('CTP_PASSWORD'),
        app_id=os.getenv('CTP_APP_ID', ''), auth_code=os.getenv('CTP_AUTH_CODE', ''),
        flow_path=os.getenv('CTP_TD_FLOW_PATH', '/tmp/bomber-ctp-ema-td'),
        production_mode=os.getenv('CTP_PRODUCTION_MODE', 'true').lower()
        in {'1', 'true', 'yes', 'on'},
        timeout_seconds=args.query_timeout,
    )
    holder = {}
    driver = CtpNativeTraderDriver(
        'ctp-simnow', investor, transport, enable_simnow_orders=True,
        max_session_orders=args.max_session_orders,
        disconnect_handler=lambda reason: holder['client'].mark_disconnected(reason),
    )
    backend = NautilusLiveExecutionBackend('ctp-simnow', driver)
    ledger = CtpPositionLedger()
    positions = PositionManager()
    prices = MarketReferencePriceStore()
    planner = CtpLimitPlanner(ledger, positions, prices, tick_size, args.limit_offset_ticks)
    risk = PreTradeRiskManager(
        'ctp-simnow', positions, prices,
        instrument_limits={instrument: RiskLimits(
            max_order_quantity=Decimal(args.max_order_quantity),
            max_abs_position=Decimal(args.max_position),
            max_order_notional=Decimal(args.max_order_notional),
            max_abs_position_notional=Decimal(args.max_position_notional),
            max_market_age_ns=120 * 1_000_000_000,
            contract_multiplier=multiplier,
        )},
    )
    upstream = CtpLiveDataFeed(CtpMdConfig(
        front=md_front, broker_id='9999', user_id=investor,
        password=required('CTP_PASSWORD'),
        flow_path=os.getenv('CTP_MD_FLOW_PATH', '/tmp/bomber-ctp-ema-md'),
        production_mode=os.getenv('CTP_PRODUCTION_MODE', 'true').lower()
        in {'1', 'true', 'yes', 'on'}))
    client = ControlledLiveExecutionClient(
        'ctp-simnow', planner, backend, positions, risk, account_id=investor,
        demo_environment_check=lambda: replay_session_ready(
            driver, upstream,
            args.expected_replay_trading_day if args.simnow_replay else driver.trading_day,
        ),
        max_request_wall_age_ns=(None if args.simnow_replay else 120 * 1_000_000_000),
    )
    holder['client'] = client
    accounting = CtpExecutionAccounting(ledger, {instrument: multiplier})
    backend.register_report_handler(accounting.on_report)
    feed = TradeTickBarFeed('CTP_EMA_1M', upstream)
    feed.register_instrument(InstrumentMeta(
        instrument_id=instrument, price_precision=args.price_precision,
        size_precision=0, price_increment=tick_size, multiplier=multiplier,
        currency='CNY', exchange=args.exchange.upper()))
    strategy = EmaCrossTargetStrategy('ema-ctp-simnow', EmaCrossConfig(
        fast_period=args.fast, slow_period=args.slow,
        long_quantity=Decimal(args.quantity), short_quantity=-Decimal(args.quantity),
        skip_single_price=False, repeat_target_each_bar=True))
    runner = UnifiedStrategyRunner(RuntimeMode.LIVE, position_manager=positions)
    runner.add_market_observer(prices)
    runner.add_data_feed('ctp-bars', feed)
    runner.add_execution_client(client)
    runner.add_strategy(
        strategy,
        data_bindings=(DataBinding('primary_bar', 'ctp-bars', instrument,
                                   DataType.BAR, '1-MINUTE'),),
        execution_routes=(ExecutionRoute('position', client.client_id, instrument),),
    )
    return runner, strategy, client, driver, transport, ledger, upstream, instrument


def main():
    parser = argparse.ArgumentParser(description='SimNow受控EMA交易')
    parser.add_argument('--symbol')
    parser.add_argument('--exchange', default=os.getenv('CTP_EXCHANGE', 'SHFE'))
    parser.add_argument('--fast', type=int, default=2)
    parser.add_argument('--slow', type=int, default=3)
    parser.add_argument('--quantity', default='1')
    parser.add_argument('--price-precision', type=int, default=0)
    parser.add_argument('--price-increment', default='1')
    parser.add_argument('--multiplier', default='10')
    parser.add_argument('--limit-offset-ticks', type=int, default=1)
    parser.add_argument('--max-order-quantity', default='1')
    parser.add_argument('--max-position', default='1')
    parser.add_argument('--max-order-notional', default='50000')
    parser.add_argument('--max-position-notional', default='50000')
    parser.add_argument('--query-timeout', type=float, default=15)
    parser.add_argument('--timeout', type=float, default=300,
                        help='本次运行秒数；--run-forever 时忽略')
    parser.add_argument('--run-forever', action='store_true',
                        help='本交易日不按固定时间停机；断线、行情停滞、交易日变化或报单上限仍立即停止')
    parser.add_argument('--enable-orders', action='store_true')
    parser.add_argument('--confirm-simnow', action='store_true')
    parser.add_argument('--simnow-replay', action='store_true',
                        help='显式启用第二套环境的历史回放EMA下单验收')
    parser.add_argument('--expected-replay-trading-day',
                        help='必须与第二套环境MD及TD报告的交易日一致，格式YYYYMMDD')
    parser.add_argument('--allow-other-positions', action='store_true',
                        help='只在回放验收时允许非测试合约旧仓，且运行期间须保持不变')
    parser.add_argument('--max-session-orders', type=int, default=4,
                        help='本次运行最多向CTP发送的订单数；限时1至4，持续运行1至100')
    args = parser.parse_args()
    if (not args.run_forever and args.timeout <= 0) or args.query_timeout <= 0:
        parser.error('超时必须大于零')
    order_limit = 100 if args.run_forever else 4
    if not 1 <= args.max_session_orders <= order_limit:
        parser.error(f'本次运行报单上限须为1至{order_limit}笔')
    if args.allow_other_positions and not args.simnow_replay:
        parser.error('--allow-other-positions 仅用于 --simnow-replay')
    if args.simnow_replay:
        day = args.expected_replay_trading_day or ''
        if len(day) != 8 or not day.isdigit():
            parser.error('--simnow-replay 必须提供 --expected-replay-trading-day YYYYMMDD')
        if not args.run_forever and args.timeout > 600:
            parser.error('限时回放验收最多运行600秒；持续运行须显式指定 --run-forever')
    elif args.expected_replay_trading_day:
        parser.error('--expected-replay-trading-day 仅用于 --simnow-replay')
    runner, strategy, client, driver, transport, ledger, upstream, instrument = build_runner(args)
    client.start()
    try:
        orders = driver.reconcile_active_orders()
        if orders.orders:
            raise RuntimeError('SimNow账户已有活动订单，拒绝自动接管')
        occupied = occupied_positions(transport)
        if str(instrument) in occupied:
            raise RuntimeError(f'SimNow测试合约{instrument}已有仓位，拒绝自动接管')
        other_positions = {key: value for key, value in occupied.items() if key != str(instrument)}
        if other_positions and not args.allow_other_positions:
            raise RuntimeError(f'SimNow账户已有其他合约仓位，拒绝自动接管: {other_positions}')
        if args.simnow_replay and driver.trading_day != args.expected_replay_trading_day:
            raise RuntimeError(
                f'CTP交易前置交易日{driver.trading_day}与期望回放日'
                f'{args.expected_replay_trading_day}不一致'
            )
    except BaseException:
        client.stop()
        raise
    print(
        f'SimNow EMA已完成测试合约空仓、资金与活动订单权威查询；'
        f'其他合约基线={other_positions} 回放模式={args.simnow_replay}'
    )
    seen = 0
    next_account_check = time.monotonic() + 10
    next_position_check = time.monotonic() + 10
    try:
        runner.start()
        expected_day = args.expected_replay_trading_day if args.simnow_replay else driver.trading_day
        ready_deadline = time.monotonic() + min(30, args.query_timeout)
        while not replay_session_ready(driver, upstream, expected_day):
            if time.monotonic() >= ready_deadline:
                raise RuntimeError(
                    f'SimNow MD未在等待期内报告交易日{expected_day}的持续行情；拒绝开启下单'
                )
            time.sleep(0.2)
        client.arm_demo(client.DEMO_CONFIRMATION)
        print(
            f'SimNow下单闸门已开启: MD/TD交易日={expected_day} '
            f'最多报单={args.max_session_orders} 持续运行={args.run_forever}'
        )
        deadline = None if args.run_forever else time.monotonic() + args.timeout
        while deadline is None or time.monotonic() < deadline:
            if client.report_errors:
                raise RuntimeError(f'CTP标准执行事件冲突，已闭闸: {client.report_errors}')
            if not driver.is_simnow_session:
                client.mark_disconnected('ctp_session_lost')
                raise RuntimeError('SimNow交易会话已断开')
            if driver.submitted_orders >= args.max_session_orders:
                client.set_risk_mode('HALTED', cancel_active_orders=True)
                print(f'报单达到{args.max_session_orders}笔上限，已闭闸并停止')
                break
            if not replay_session_ready(driver, upstream, expected_day):
                client.mark_disconnected('simnow_md_stale_or_day_mismatch')
                raise RuntimeError('SimNow MD已停滞或与TD交易日不一致')
            if time.monotonic() >= next_account_check:
                client.refresh_account_state()
                next_account_check = time.monotonic() + 10
            if time.monotonic() >= next_position_check:
                gross = occupied_positions(transport)
                actual_other = {key: value for key, value in gross.items() if key != str(instrument)}
                snapshot = ledger.snapshot(instrument)
                expected = (snapshot.long_total, snapshot.short_total)
                actual = gross.get(str(instrument), (Decimal(0), Decimal(0)))
                if actual_other != other_positions or actual != expected:
                    client.mark_disconnected('position_ledger_mismatch')
                    raise RuntimeError(
                        f'SimNow柜台与本地账本不一致: 测试合约柜台={actual} '
                        f'本地={expected} 其他合约柜台={actual_other} 基线={other_positions}'
                    )
                next_position_check = time.monotonic() + 10
            backend_reports = client.backend.reports[seen:]
            for report in backend_reports:
                print(f'CTP执行回报: {report}')
            seen += len(backend_reports)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            client.set_risk_mode('HALTED', cancel_active_orders=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if not driver.reconcile_active_orders().orders:
                    break
                time.sleep(1)
            else:
                print('停机时SimNow柜台仍有活动订单，必须人工核对')
        except Exception as error:
            client.disarm('shutdown_query_failed')
            print(f'停机撤单或查询未完成，必须人工核对: {error}')
        try:
            final_gross = occupied_positions(transport)
            final_other = {key: value for key, value in final_gross.items() if key != str(instrument)}
            print(f'停机柜台总仓: {final_gross} 其他合约仓位未变={final_other == other_positions}')
        except Exception as error:
            print(f'停机仓位查询失败，必须人工核对: {error}')
        backend_reports = client.backend.reports[seen:]
        for report in backend_reports:
            print(f'CTP执行回报: {report}')
        seen += len(backend_reports)
        if client.report_errors:
            print(f'执行回报状态冲突，必须人工对账: {client.report_errors}')
        runner.stop()
    print(f'已停止: bars_seen={strategy.bars_seen}, reports={seen}')


if __name__ == '__main__':
    main()
