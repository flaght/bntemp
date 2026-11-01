"""测试策略本身的业务逻辑；单元测试仅隔离框架提交接口。"""

import csv
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

backtest = importlib.import_module("demos.11_cta_by_pos.run_backtest")
comparison = importlib.import_module("demos.11_cta_by_pos.compare_results")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def strategy_module(monkeypatch):
    """加载真实策略文件，用记录器替代 StrategyTemplate 的提交边界。"""
    class TargetUpdateMode(str, Enum):
        REPLACE = "REPLACE"

    class SubmissionCapture:
        def __init__(self, strategy_id):
            self.strategy_id = strategy_id
            self.submissions = []

        def set_targets(self, targets, ts_event, **kwargs):
            self.submissions.append(SimpleNamespace(targets=dict(targets), ts_event=ts_event, **kwargs))

    contracts = ModuleType("bomber.framework.trader.contracts")
    contracts.TargetUpdateMode = TargetUpdateMode
    template = ModuleType("bomber.framework.trader.template")
    template.StrategyTemplate = SubmissionCapture
    monkeypatch.setitem(sys.modules, contracts.__name__, contracts)
    monkeypatch.setitem(sys.modules, template.__name__, template)
    spec = importlib.util.spec_from_file_location("cta_by_pos_unit_strategy",
        ROOT / "demos/11_cta_by_pos/strategy.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def ns(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1_000_000_000)


def make_strategy(tmp_path, module, *rows, signal_time="09:40", **kwargs):
    path = tmp_path / "positions.csv"
    path.write_text("timestamp,instrument_id,instrument_type,target_qty\n" + "\n".join(rows) + "\n")
    return module.CtaByPosStrategy("test", path, module.CtaByPosConfig(signal_time), **kwargs)


def feed(strategy, timestamp):
    count = len(strategy.submissions)
    strategy.on_bar("bars", SimpleNamespace(ts_event=timestamp))
    return strategy.submissions[-1] if len(strategy.submissions) > count else None


def test_emits_at_signal_bar_and_once_for_the_day(tmp_path, strategy_module):
    strategy = make_strategy(tmp_path, strategy_module,
        "2026-05-06 09:40:00,IF2606,FUTURE,2", "2026-05-06 09:40:00,IH2606,FUTURE,0")
    assert feed(strategy, ns("2026-05-06 09:39:00")) is None
    stamp = ns("2026-05-06 09:40:00")
    target = feed(strategy, stamp)
    assert target.ts_event == stamp
    assert target.targets == {"IF2606": Decimal(2), "IH2606": Decimal(0)}
    assert target.update_mode.value == "REPLACE"
    assert feed(strategy, stamp) is None
    assert feed(strategy, ns("2026-05-06 09:40:01")) is None
    assert feed(strategy, ns("2026-05-06 09:41:00")) is None


def test_unchanged_daily_targets_are_still_submitted(tmp_path, strategy_module):
    strategy = make_strategy(tmp_path, strategy_module,
        "2026-05-06 09:40:00,IF2606,FUTURE,2", "2026-05-07 09:40:00,IF2606,FUTURE,2")
    first = feed(strategy, ns("2026-05-06 09:40:00"))
    second = feed(strategy, ns("2026-05-07 09:40:00"))
    assert first.targets == second.targets == {"IF2606": Decimal(2)}


def test_missing_day_or_missed_signal_time_does_not_submit(tmp_path, strategy_module):
    strategy = make_strategy(tmp_path, strategy_module, "2026-05-06 09:40:00,IF2606,FUTURE,2")
    assert feed(strategy, ns("2026-05-06 09:39:00")) is None
    assert feed(strategy, ns("2026-05-06 09:41:00")) is None
    assert feed(strategy, ns("2026-05-07 09:40:00")) is None


def test_daily_aggregation_last_row_wins_and_row_time_is_not_a_schedule(tmp_path, strategy_module):
    strategy = make_strategy(tmp_path, strategy_module,
        "2026-05-06 09:35:00,IF2606,FUTURE,2", "2026-05-06 14:00:00,IF2606,FUTURE,-3")
    assert feed(strategy, ns("2026-05-06 09:40:00")).targets == {"IF2606": Decimal(-3)}


def test_expired_nonzero_filtered_but_zero_retained(tmp_path, strategy_module):
    strategy = make_strategy(tmp_path, strategy_module, "2026-05-18 09:40:00,IF2605,FUTURE,2",
        "2026-05-18 09:40:00,IH2605,FUTURE,0", "2026-05-18 09:40:00,IC2606,FUTURE,-1")
    target = feed(strategy, ns("2026-05-18 09:40:00"))
    assert target.targets == {"IH2605": Decimal(0), "IC2606": Decimal(-1)}
    assert target.metadata["skipped_expired"] == ("IF2605",)


def test_all_expired_targets_do_not_emit_an_empty_portfolio(tmp_path, strategy_module):
    strategy = make_strategy(tmp_path, strategy_module, "2026-05-18 09:40:00,IF2605,FUTURE,2")
    assert feed(strategy, ns("2026-05-18 09:40:00")) is None


def test_contract_expiry_boundary(tmp_path, strategy_module):
    strategy = make_strategy(tmp_path, strategy_module,
        "2026-05-15 15:00:00,IF2605,FUTURE,2", signal_time="15:00")
    assert not strategy._is_contract_expired("IF2605", ns("2026-05-15 15:00:00"))
    assert strategy._is_contract_expired("IF2605", ns("2026-05-15 15:00:01"))
    assert feed(strategy, ns("2026-05-15 15:00:00")).targets == {"IF2605": 2}


def test_requested_dates_select_the_target_universe(tmp_path, strategy_module):
    strategy = make_strategy(tmp_path, strategy_module,
        "2018-02-13 09:40:00,IF1803,FUTURE,2", "2026-05-06 09:40:00,IF2606,FUTURE,-1",
        start_day=date(2026, 5, 6), end_day=date(2026, 5, 6))
    assert strategy.get_insts() == ["IF2606"]
    assert strategy.target_dates == (date(2026, 5, 6),)
    assert strategy.max_target_quantity == Decimal(1)


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "0.5"])
def test_invalid_futures_quantity(tmp_path, strategy_module, quantity):
    with pytest.raises(ValueError, match="手数"):
        make_strategy(tmp_path, strategy_module, f"2026-05-06 09:40:00,IF2606,FUTURE,{quantity}")


def test_backward_time_rejected(tmp_path, strategy_module):
    strategy = make_strategy(tmp_path, strategy_module, "2026-05-06 09:40:00,IF2606,FUTURE,2")
    feed(strategy, ns("2026-05-06 09:40:00"))
    with pytest.raises(ValueError, match="倒退"):
        feed(strategy, ns("2026-05-06 09:39:00"))


def test_original_positions_remain_the_daily_business_targets(strategy_module):
    path = ROOT / "3rd/strategies/kd_cta_by_pos/positions.csv"
    expected = {}
    with path.open() as stream:
        for row in csv.DictReader(stream):
            day = datetime.fromisoformat(row["timestamp"]).date()
            expected.setdefault(day, {})[row["instrument_id"]] = Decimal(row["target_qty"])
    strategy = strategy_module.CtaByPosStrategy("test", path)
    for day, targets in sorted(expected.items()):
        assert feed(strategy, ns(f"{day} 09:40:00")).targets == targets


def test_comparison_export_uses_actual_framework_fee(tmp_path):
    import pandas as pd
    fills = pd.DataFrame([{"ts_event": pd.Timestamp("2026-05-06T01:40:00Z"),
        "instrument_id": "IF2606.CFFEX", "order_side": "BUY", "last_qty": "2",
        "last_px": "4000.0", "commission": "72.00 CNY"}], index=["framework-order"])
    path = tmp_path / "fills_output.csv"
    backtest.export_comparison_fills(fills, path)
    exported = comparison.read_fills(path)
    assert exported[0][0].strftime("%H:%M") == "09:40"
    assert exported[0][3:] == (Decimal(2), Decimal(4000), Decimal(72))


def test_cli_uses_framework_inputs_and_fee_configuration():
    args = backtest.parse_args(["--start-day", "2026-05-06", "--end-day", "2026-05-07",
                               "--data-root", "/data/ctp"])
    assert args.start_day == date(2026, 5, 6)
    assert args.commission_source == "metadata"
    assert args.data_root == Path("/data/ctp")
    assert args.execution_timing == "next_market"
    assert args.execution_delay_bars == 1
    native = backtest.parse_args(["--start-day", "2026-05-06", "--end-day", "2026-05-07",
                                 "--execution-timing", "native"])
    assert (native.execution_timing, native.execution_delay_bars) == ("native", 0)
    assert backtest.parse_args(["--start-day", "2026-05-06", "--end-day", "2026-05-07",
                               "--execution-delay-bars", "2"]).execution_delay_bars == 2


@pytest.mark.parametrize("options", [("--execution-delay-bars", "0"),
    ("--execution-delay-bars", "-1"), ("--execution-timing", "native", "--execution-delay-bars", "2")])
def test_cli_rejects_conflicting_execution_delay(options):
    with pytest.raises(SystemExit):
        backtest.parse_args(["--start-day", "2026-05-06", "--end-day", "2026-05-07", *options])


def require_bomber_engine():
    if importlib.util.find_spec("bomber.backtest") is None:
        pytest.skip("未安装 Bomber 编译内核")
    pytest.importorskip("bomber.backtest.engine")


def test_standard_framework_replay(tmp_path):
    require_bomber_engine()
    pytest.importorskip("pyarrow")
    import pandas as pd
    path = tmp_path / "positions.csv"
    path.write_text("timestamp,instrument_id,instrument_type,target_qty\n"
                    "2026-05-06 09:40:00,IF2606,FUTURE,2\n")
    basic = tmp_path / "fut_basic.feather"
    pd.DataFrame([{"symbol": "IF2606", "code": "IF", "exchangeCD": "CCFX",
        "contMultNum": 300, "minChgPriceNum": 0.2, "listDate": "2026-01-01",
        "lastTradeDate": "2026-06-19", "trade_fee_per_contract": 1, "trade_fee_rate": 0}]).to_feather(basic)
    bars_dir = tmp_path / "bars"
    bars_dir.mkdir()
    pd.DataFrame({"datetime": pd.date_range("2026-05-06 09:39", periods=5, freq="min"),
                  "open": 4000, "high": 4001, "low": 3999, "close": 4000, "volume": 100}).to_feather(
        bars_dir / "IF2606_20260506.feather")
    output = backtest.run_case(positions_path=path, start_day=date(2026, 5, 6),
        end_day=date(2026, 5, 6), bars_dir=bars_dir, fut_basic=basic,
        report_dir=tmp_path / "report", tearsheet=False, require_fills=True, log_level="ERROR")
    fills = comparison.read_fills(output / "fills_output.csv")
    assert sum(row[3] if row[2] == "BUY" else -row[3] for row in fills) == Decimal(2)
    assert (output / "commissions.csv").is_file()


@pytest.mark.parametrize("missing_ih_next_bar", [False, True])
@pytest.mark.parametrize("delay_bars", [1, 2])
def test_multiple_contracts_wait_for_their_own_later_bar(tmp_path, missing_ih_next_bar, delay_bars):
    """真实主链：IC先触发信号，IF/IH同时间戳的Bar不可成交；价格取新Bar。"""
    require_bomber_engine()
    pytest.importorskip("pyarrow")
    import pandas as pd
    targets = {"IC2606": 0, "IF2606": -1, "IH2606": -1, "IM2606": 0}
    path = tmp_path / "positions.csv"
    path.write_text("timestamp,instrument_id,instrument_type,target_qty\n" + "".join(
        f"2026-05-06 09:40:00,{symbol},FUTURE,{qty}\n" for symbol, qty in targets.items()))
    basic = tmp_path / "fut_basic.feather"
    pd.DataFrame([{"symbol": symbol, "code": symbol[:2], "exchangeCD": "CCFX",
        "contMultNum": 200 if symbol[:2] in {"IC", "IM"} else 300,
        "minChgPriceNum": 0.2, "listDate": "2026-01-01", "lastTradeDate": "2026-06-19",
        "trade_fee_per_contract": 1, "trade_fee_rate": 0} for symbol in targets]).to_feather(basic)
    bars_dir = tmp_path / "bars"
    bars_dir.mkdir()
    for symbol, base in zip(targets, (6000, 4000, 3000, 7000)):
        rows = []
        for offset in range(5):
            if symbol == "IH2606" and offset == 2 and missing_ih_next_bar:
                continue
            price = base + offset * 10
            rows.append({"datetime": pd.Timestamp("2026-05-06 09:39") + pd.Timedelta(minutes=offset),
                "open": price, "high": price + 5, "low": price - 5,
                "close": price + 2, "volume": 100})
        pd.DataFrame(rows).to_feather(bars_dir / f"{symbol}_20260506.feather")
    output = backtest.run_case(positions_path=path, start_day=date(2026, 5, 6),
        end_day=date(2026, 5, 6), bars_dir=bars_dir, fut_basic=basic,
        execution_delay_bars=delay_bars,
        report_dir=tmp_path / "report", tearsheet=False, require_fills=True, log_level="ERROR")
    fills = comparison.read_fills(output / "fills_output.csv")
    assert len(fills) == 2
    by_instrument = {row[1]: row for row in fills}
    if_offset = 1 + delay_bars
    ih_offset = if_offset + int(missing_ih_next_bar)
    for instrument, offset, base in (("IF2606.CFFEX", if_offset, 4000),
                                     ("IH2606.CFFEX", ih_offset, 3000)):
        stamp, _, side, qty, fill_price, _ = by_instrument[instrument]
        expected = pd.Timestamp("2026-05-06 09:39") + pd.Timedelta(minutes=offset)
        assert stamp.strftime("%H:%M:%S") == expected.strftime("%H:%M:%S")
        assert (side, qty, fill_price) == ("SELL", Decimal(1), Decimal(base + offset * 10 + 2))
    signals = pd.read_csv(output / "signals.csv")
    assert set(signals.ts_event) == {ns("2026-05-06 09:40:00")}
    import json
    summary = json.loads((output / "summary.json").read_text())
    assert summary["execution_timing"] == "next_market"
    assert summary["execution_delay_bars"] == delay_bars
