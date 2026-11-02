"""只读比较两份已导出的回测结果，归因场景与实际结果分别保存。"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import html
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]
NEW = BASE / "kd/d14a7231-1535-443b-b354-1405ab8f795b"
OLD = BASE / "lxg/output_20261102_145127"
OUT = Path(__file__).resolve().parent
INITIAL = 40_000_000.0
END = pd.Timestamp("2026-07-09")
CUTOFF = END + pd.Timedelta(days=1)


def read_csv(directory, name):
    return pd.read_csv(directory / name)


def read_fills(directory):
    frame = read_csv(directory, "fills_output.csv")
    frame["timestamp"] = pd.to_datetime(frame.timestamp)
    frame["day"] = frame.timestamp.dt.strftime("%Y-%m-%d")
    frame["kind"] = np.where(frame.instrument_id.str.startswith("MO"), "OPTION", "FUTURE")
    frame["multiplier"] = np.where(frame.kind.eq("OPTION"), 100, 200)
    frame["direction"] = np.where(frame.side.eq("BUY"), 1, -1)
    frame["signed_qty"] = frame.qty * frame.direction
    frame["gross_flow"] = -frame.signed_qty * frame.price * frame.multiplier
    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def summary_by_kind(frame):
    return frame.groupby("kind").agg(rows=("qty", "size"), quantity=("qty", "sum"),
        commission=("commission", "sum")).to_dict("index")


def stats(equity):
    values = np.r_[INITIAL, equity.to_numpy(dtype=float)]
    return dict(ending_equity=float(values[-1]), profit=float(values[-1] - INITIAL),
        total_return=float(values[-1] / INITIAL - 1),
        maximum_drawdown=float((values / np.maximum.accumulate(values) - 1).min()))


def pnl_ledger(frame):
    """加权平均成本，仅用于重建持仓及盈亏；期权权利金现金另行计算。"""
    states = {}
    realized = 0.0
    fees = 0.0
    for row in frame.itertuples():
        delta = float(row.qty) * (1 if row.side == "BUY" else -1)
        price = float(row.price)
        quantity, average = states.get(row.instrument_id, (0.0, 0.0))
        multiplier = 100 if row.instrument_id.startswith("MO") else 200
        if quantity * delta >= 0:
            average = (abs(quantity) * average + abs(delta) * price) / (abs(quantity) + abs(delta))
        else:
            realized += min(abs(quantity), abs(delta)) * (price - average) * np.sign(quantity) * multiplier
            if abs(delta) > abs(quantity):
                average = price
            elif abs(delta) == abs(quantity):
                average = 0.0
        states[row.instrument_id] = quantity + delta, average
        fees += float(row.commission)
    premium = sum(-quantity * average * 100 for instrument, (quantity, average) in states.items()
                  if instrument.startswith("MO"))
    return dict(realized_gross=realized, fees=fees, remaining_option_premium=premium,
        option_premium_cash_before_fees=INITIAL + realized + premium,
        option_premium_cash_after_fees=INITIAL + realized + premium - fees), states


def dump_csv(frame, name, index=False):
    frame.to_csv(OUT / name, index=index, encoding="utf-8-sig", float_format="%.8f")


def json_default(value):
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, (pd.Timestamp, Path)):
        return str(value)
    raise TypeError(type(value).__name__)


def table(frame, money=()):
    frame = frame.copy()
    for column in money:
        if column in frame:
            frame[column] = frame[column].map(lambda value: f"{value:,.2f}" if pd.notna(value) else "—")
    return frame.to_html(index=False, border=0, classes="data")


def line_chart(frame, columns):
    width, height, left, top, right, bottom = 1040, 360, 76, 24, 24, 48
    y_values = np.concatenate([frame[column].values for column, _, _ in columns]) / 1e6
    low, high = np.floor(y_values.min() * 5) / 5, np.ceil(y_values.max() * 5) / 5
    low, high = low - 0.05, high + 0.05
    x = lambda index: left + index / max(len(frame) - 1, 1) * (width - left - right)
    y = lambda value: top + (high - value) / (high - low) * (height - top - bottom)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="统一口径归因场景净值曲线">']
    for tick in np.linspace(low, high, 5):
        yy = y(tick)
        parts.append(f'<line x1="{left}" y1="{yy:.2f}" x2="{width-right}" y2="{yy:.2f}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{left-12}" y="{yy+4:.2f}" text-anchor="end" fill="#475569" font-size="12">{tick:.2f}</text>')
    for index in np.linspace(0, len(frame) - 1, 7).astype(int):
        parts.append(f'<text x="{x(index):.2f}" y="{height-15}" text-anchor="middle" fill="#475569" font-size="12">{frame.index[index].strftime("%m-%d")}</text>')
    for column, label, color in columns:
        points = " ".join(f"{x(index):.2f},{y(value/1e6):.2f}" for index, value in enumerate(frame[column]))
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>')
    parts.append('<text x="14" y="16" fill="#475569" font-size="12">百万元</text></svg>')
    legend = " ".join(f'<span class="legend" style="color:{color}">{html.escape(label)}</span>' for _, label, color in columns)
    return "".join(parts) + f'<div>{legend}</div>'


def main():
    sources = sorted([*NEW.glob("*.csv"), *NEW.glob("*.json"), *OLD.glob("*.csv"), OLD / "metrics.txt"])
    fingerprints = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    new = read_fills(NEW)
    old_full = read_fills(OLD)
    old = old_full.loc[old_full.timestamp.lt(CUTOFF)].copy()
    summary = json.loads((NEW / "summary.json").read_text())
    orders = read_csv(NEW, "orders.csv")
    positions = read_csv(NEW, "positions.csv")
    commissions = read_csv(NEW, "commissions.csv")
    settlements = read_csv(NEW, "settlements.csv")
    account = read_csv(NEW, "account.csv")
    old_curve = read_csv(OLD, "net_value.csv")
    old_curve["date"] = pd.to_datetime(old_curve.date)
    old_curve = old_curve.set_index("date").equity
    old_positions = read_csv(OLD, "positions_output.csv")
    old_positions["timestamp"] = pd.to_datetime(old_positions.timestamp)
    risk = read_csv(OLD, "risk_metrics.csv")
    old_metrics_text = (OLD / "metrics.txt").read_text()
    assert "initial_capital_cny: 40000000" in old_metrics_text
    assert float(account.total.iloc[0]) == INITIAL

    # 由实际异常订单和费用记录定位未完整处理的到期平仓。
    expiration_orders = orders[orders.client_order_id.str.startswith("EXPIRATION-LEG-")].copy()
    hidden = commissions[commissions.client_order_id.str.startswith("EXPIRATION-LEG-")].copy()
    order_index = orders.set_index("client_order_id")
    hidden["timestamp"] = pd.to_datetime(hidden.client_order_id.map(order_index.ts_init), unit="ns", utc=True).dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    hidden["side"] = "SELL"
    hidden = hidden.rename(columns={"client_order_id": "order_id", "quantity": "qty",
        "fill_price": "price", "actual_commission": "commission"})
    hidden["kind"] = "FUTURE"
    hidden["multiplier"] = 200
    hidden["direction"] = -1
    hidden["signed_qty"] = -hidden.qty
    hidden["day"] = hidden.timestamp.dt.strftime("%Y-%m-%d")
    hidden["gross_flow"] = hidden.qty * hidden.price * 200
    actual_journal = pd.concat([new, hidden[new.columns]], ignore_index=True).sort_values("timestamp", kind="stable")

    positions["native_signed"] = positions.quantity * positions.side.map({"LONG": 1, "SHORT": -1, "FLAT": 0})
    visible_qty = new.groupby("instrument_id").signed_qty.sum()
    old_qty = old.groupby("instrument_id").signed_qty.sum()
    native_qty = positions.groupby("instrument_id").native_signed.sum()
    hidden_qty = hidden.groupby("instrument_id").signed_qty.sum()
    identities = sorted(set(visible_qty.index) | set(old_qty.index) | set(native_qty.index))
    reconciliation = pd.DataFrame({"instrument_id": identities}).set_index("instrument_id")
    reconciliation["old_net_0709"] = old_qty.reindex(identities, fill_value=0)
    reconciliation["new_visible_net"] = visible_qty.reindex(identities, fill_value=0)
    reconciliation["framework_net"] = [float(summary["framework_positions"].get(instrument.removesuffix(".CFFEX"), 0)) for instrument in identities]
    reconciliation["strategy_net"] = [float(summary["strategy_positions"].get(instrument.removesuffix(".CFFEX"), 0)) for instrument in identities]
    reconciliation["native_net"] = native_qty.reindex(identities, fill_value=0)
    reconciliation["hidden_expiry_delta"] = hidden_qty.reindex(identities, fill_value=0)
    reconciliation["native_minus_framework"] = reconciliation.native_net - reconciliation.framework_net
    np.testing.assert_allclose(reconciliation.new_visible_net, reconciliation.framework_net, atol=1e-9)
    np.testing.assert_allclose(reconciliation.new_visible_net + reconciliation.hidden_expiry_delta,
        reconciliation.native_net, atol=1e-9)
    expiry_ids = set(expiration_orders.instrument_id)
    expiry_reconciliation = reconciliation.loc[sorted(expiry_ids)].copy()
    dump_csv(reconciliation.reset_index(), "positions_reconciliation.csv")
    dump_csv(expiry_reconciliation.reset_index(), "expiry_positions.csv")
    dump_csv(expiration_orders, "initialized_expiry_orders.csv")
    dump_csv(hidden[["timestamp", "instrument_id", "qty", "price", "commission", "order_id"]], "hidden_expiry_fills.csv")

    # 归因场景：补入旧版实际完成的5笔期货到期平仓，不混入隐藏的部分成交。
    # 这不是原生重跑结果，只用于隔离遗漏到期成交之后的价格及费用差。
    added = []
    for row in expiration_orders.itertuples():
        day = (pd.Timestamp(row.ts_init, unit="ns", tz="UTC").tz_convert("Asia/Shanghai") - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        selection = old[(old.day.eq(day)) & old.instrument_id.eq(row.instrument_id) & old.side.eq("SELL")
            & old.qty.eq(row.quantity) & old.timestamp.dt.strftime("%H:%M:%S").eq("14:59:00")]
        assert len(selection) == 1, (day, row.instrument_id)
        copied = selection.iloc[0].to_dict()
        copied["commission"] = float((Decimal(str(copied["qty"])) * Decimal(str(copied["price"]))
            * Decimal(200) * Decimal("0.000023")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        copied["order_id"] = "ATTRIBUTION-ONLY-" + str(copied["order_id"])
        added.append(copied)
    additions = pd.DataFrame(added)
    scenario = pd.concat([new, additions], ignore_index=True).sort_values("timestamp", kind="stable")
    dump_csv(additions, "scenario_added_future_closes.csv")

    # 旧版净值在到期日用终值替代实际期权平仓成交价。独立重放这项变换。
    settlements["day"] = pd.to_datetime(settlements.ts_event, unit="ns", utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    terminal = settlements.set_index(["instrument_id", "day"]).price.to_dict()
    normalized_old = old.copy()
    normalized_old["export_price"] = normalized_old.price
    normalized_old["price"] = [terminal.get((row.instrument_id, row.day), row.price) for row in normalized_old.itertuples()]
    normalized_old["gross_flow"] = -normalized_old.signed_qty * normalized_old.price * normalized_old.multiplier
    expiry_adjustments = normalized_old[normalized_old.price.ne(normalized_old.export_price)].copy()
    expiry_adjustments["reported_equity_adjustment"] = -expiry_adjustments.signed_qty * (expiry_adjustments.price - expiry_adjustments.export_price) * 100
    dump_csv(expiry_adjustments[["timestamp", "instrument_id", "qty", "side", "export_price", "price", "reported_equity_adjustment"]], "old_terminal_price_adjustments.csv")

    keys = ["day", "instrument_id", "side"]
    def aggregate(frame):
        return frame.groupby(keys, as_index=False).agg(qty=("qty", "sum"), gross_flow=("gross_flow", "sum"), commission=("commission", "sum"),
            first_time=("timestamp", "min"), last_time=("timestamp", "max"))
    compared = aggregate(scenario).merge(aggregate(normalized_old), on=keys, how="outer", suffixes=("_new", "_old"), indicator=True)
    assert compared._merge.eq("both").all()
    np.testing.assert_allclose(compared.qty_new, compared.qty_old, atol=1e-9)
    compared["price_effect"] = compared.gross_flow_new - compared.gross_flow_old
    compared["fee_effect"] = compared.commission_old - compared.commission_new
    compared["net_effect"] = compared.price_effect + compared.fee_effect
    dump_csv(compared.drop(columns="_merge"), "daily_contract_price_effects.csv")

    # 实际成交的配对明细，按同日同合约同方向同数量顺序配对，保留拆单未匹配项。
    pair_keys = ["day", "instrument_id", "side", "qty"]
    for frame in (new, old):
        frame["occurrence"] = frame.groupby(pair_keys).cumcount()
    paired = new.merge(old, on=[*pair_keys, "occurrence"], how="outer", suffixes=("_new", "_old"), indicator=True)
    paired["delay_minutes_new_minus_old"] = (paired.timestamp_new - paired.timestamp_old).dt.total_seconds() / 60
    paired["price_effect"] = -paired.direction_new * paired.qty * (paired.price_new - paired.price_old) * paired.multiplier_new
    dump_csv(paired, "fill_pairs.csv")
    may29 = paired[(paired.day.eq("2026-05-29")) & paired.instrument_id.eq("IM2606.CFFEX") & paired.delay_minutes_new_minus_old.eq(-4)].copy()
    assert len(may29) == 3
    dump_csv(may29[["instrument_id", "side", "qty", "timestamp_old", "timestamp_new", "price_old", "price_new", "price_effect"]], "may29_price_differences.csv")

    # 同日同合约同方向的买卖量一致，故每个日终的持仓与估值项相同，
    # 两套归因净值之差可由现金流差独立确定，不需要猜测新版缺失的分钟行情。
    index = old_curve.loc[:END].index
    daily = pd.DataFrame(index=index)
    daily.index.name = "date"
    daily["old_reported_gross_nav"] = old_curve.reindex(index)
    price_daily = compared.groupby("day").price_effect.sum()
    new_fee_daily = scenario.groupby("day").commission.sum()
    old_fee_daily = old.groupby("day").commission.sum()
    lookup = index.strftime("%Y-%m-%d")
    daily["price_effect"] = price_daily.reindex(lookup, fill_value=0).values
    daily["scenario_new_gross_nav"] = daily.old_reported_gross_nav + daily.price_effect.cumsum()
    daily["old_listed_fees_cumulative"] = old_fee_daily.reindex(lookup, fill_value=0).cumsum().values
    daily["scenario_new_fees_cumulative"] = new_fee_daily.reindex(lookup, fill_value=0).cumsum().values
    daily["old_nav_after_listed_fees"] = daily.old_reported_gross_nav - daily.old_listed_fees_cumulative
    daily["scenario_new_net_nav"] = daily.scenario_new_gross_nav - daily.scenario_new_fees_cumulative
    daily["scenario_net_minus_old_net"] = daily.scenario_new_net_nav - daily.old_nav_after_listed_fees
    account_times = pd.to_datetime(account.iloc[:, 0], utc=True).dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    cash_daily = pd.Series(account.total.values, index=account_times).resample("D").last().ffill()
    daily["actual_new_cash_not_nav"] = cash_daily.reindex(index).ffill()
    dump_csv(daily, "daily_attribution_nav.csv", index=True)
    months = daily.groupby(daily.index.to_period("M")).agg(old_gross_nav=("old_reported_gross_nav", "last"),
        scenario_new_gross_nav=("scenario_new_gross_nav", "last"), old_after_fees=("old_nav_after_listed_fees", "last"),
        scenario_new_net_nav=("scenario_new_net_nav", "last"), price_effect=("price_effect", "sum"))
    dump_csv(months, "monthly_attribution.csv", index=True)

    # 独立验证旧版共同截止日的净值：逐笔盈亏 + 同日持仓价格 + 终值调整，不扣费用。
    old_ledger, old_state = pnl_ledger(normalized_old)
    snapshots = old_positions[(old_positions.timestamp.lt(CUTOFF)) & old_positions.instrument_id.ne("__DAY_END__")
        & old_positions.price.gt(0)].sort_values("timestamp", kind="stable").groupby("instrument_id").tail(1).set_index("instrument_id")
    old_unrealized = 0.0
    for instrument, (quantity, average) in old_state.items():
        if quantity:
            assert snapshots.loc[instrument, "timestamp"].date() == END.date()
            old_unrealized += quantity * (snapshots.loc[instrument, "price"] - average) * (100 if instrument.startswith("MO") else 200)
    reconstructed_old = INITIAL + old_ledger["realized_gross"] + old_unrealized
    assert abs(reconstructed_old - float(old_curve.loc[END])) < 0.02

    # 期权权利金使现金余额不同于净值。重建现金流以验证账户数字，不当作绩效指标。
    native_ledger, native_state = pnl_ledger(actual_journal)
    actual_cash = float(account.total.iloc[-1])
    assert abs(native_ledger["option_premium_cash_after_fees"] - actual_cash) < 0.05
    old_full_ledger, _ = pnl_ledger(old_full)
    old_reported_profit = float(re.search(r"Total Profit:\s*([\d,.]+)", old_metrics_text).group(1).replace(",", ""))
    assert abs(old_full_ledger["option_premium_cash_before_fees"] - INITIAL - old_reported_profit) < 0.02
    listed_new_fees = float(new.commission.sum())
    actual_new_fees = float(commissions.actual_commission.sum())
    assert abs(actual_new_fees - listed_new_fees - float(hidden.commission.sum())) < 0.001
    assert set(commissions.loc[commissions.instrument_id.str.startswith("IM"), "rate"]) == {0.000023}
    old_futures = old[old.kind.eq("FUTURE")]
    np.testing.assert_allclose(old_futures.commission / (old_futures.qty * old_futures.price * 200), 0.0003, atol=1e-12)

    price_effect = float(compared.price_effect.sum())
    fee_effect = float(compared.fee_effect.sum())
    delayed_effect = float(may29.price_effect.sum())
    scenarios = {column: stats(daily[column]) for column in ("old_reported_gross_nav", "scenario_new_gross_nav", "old_nav_after_listed_fees", "scenario_new_net_nav")}
    findings = dict(
        common_period=[str(index.min().date()), str(END.date())], initial_capital=INITIAL,
        new_run=str(NEW), old_run=str(OLD),
        actual=dict(new_fills=summary_by_kind(new), old_fills_common=summary_by_kind(old), old_fills_full=summary_by_kind(old_full),
            new_fill_rows=len(new), old_fill_rows_common=len(old), old_fill_rows_full=len(old_full),
            old_fills_after_cutoff=int(len(old_full)-len(old)),
            new_cash_ending=actual_cash, new_cash_change=actual_cash-INITIAL,
            new_cash_is_not_net_asset_value=True,
            old_metrics_reported_profit=old_reported_profit, old_metrics_profit_matches_cash_before_fees=True,
            old_net_value_common=float(old_curve.loc[END]), old_net_value_last=float(old_curve.iloc[-1]),
            old_last_net_value_date=str(old_curve.index[-1].date()),
            old_metrics_profit_minus_last_curve_profit=old_reported_profit-float(old_curve.iloc[-1]-INITIAL),
            new_fees_in_fills=listed_new_fees, new_actual_fees_in_commissions=actual_new_fees,
            hidden_expiry_commission=float(hidden.commission.sum()),
            expired_future_framework_quantity=float(expiry_reconciliation.framework_net.sum()),
            expired_future_native_quantity=float(expiry_reconciliation.native_net.sum()),
            hidden_expiry_quantity=float(hidden.qty.sum()), initialized_expiry_orders=len(expiration_orders),
            settlement_count=len(settlements), old_report_errors_absent=True, new_report_errors=summary["report_errors"],
            old_risk_first_day=str(risk.date.min()), old_risk_last_day=str(risk.date.max()), old_risk_rows=len(risk),
            old_risk_zero_exposure_rows=int(risk.eod_exposure.eq(0).sum())),
        attribution=dict(is_native_rerun=False,
            assumptions=["共同截止日为2026-07-09，排除旧版2026-07-10新增8笔成交及2026-07-17结算尾部",
                "将新版缺少的5笔期货到期平仓按旧版实际价格及手数补入，移除原生隐藏部分成交的影响",
                "旧版到期日的期权平仓价格按新版导出的官方终值替换，可将旧版费前净值复算至分",
                "每日买卖量对齐后日终持仓一致，使用旧版净值的日终估值项",
                "新版补入平仓按现有0.23bp手续费计算，旧版费用采用成交文件列示值"],
            matching_day_instrument_side_groups=len(compared), all_group_quantities_equal=True,
            gross_price_effect=price_effect, ordinary_future_price_effect=float(compared.loc[compared.instrument_id.str.startswith("IM"), "price_effect"].sum()),
            ordinary_option_price_effect=float(compared.loc[compared.instrument_id.str.startswith("MO"), "price_effect"].sum()),
            may29_three_future_trade_effect=delayed_effect, other_trade_effect=price_effect-delayed_effect,
            old_terminal_price_adjustment=float(expiry_adjustments.reported_equity_adjustment.sum()),
            old_fees_common=float(old.commission.sum()), new_fees_with_added_closes=float(scenario.commission.sum()),
            old_option_fees_common=float(old.loc[old.kind.eq("OPTION"), "commission"].sum()),
            new_option_fees_common=float(new.loc[new.kind.eq("OPTION"), "commission"].sum()),
            fee_advantage=fee_effect, net_difference=price_effect+fee_effect, scenarios=scenarios),
        reconciliation=dict(old_nav_reconstructed=reconstructed_old, old_nav_residual=reconstructed_old-float(old_curve.loc[END]),
            new_cash_reconstructed=native_ledger["option_premium_cash_after_fees"], new_cash_residual=native_ledger["option_premium_cash_after_fees"]-actual_cash,
            old_metrics_cash_reconstructed=old_full_ledger["option_premium_cash_before_fees"],
            native_position_pnl_and_cash_difference_explained_by_open_option_premium=native_ledger["remaining_option_premium"]),
        limitations=["归因场景不是修复后的撮合重跑；不能将其作为新版有效业绩",
            "缺少服务器原始行情及旧版运行配置原件，无法最终确认5月29日旧报价复用的来源",
            "缺少两次positions.csv输入原件，无法做输入文件内容哈希对比",
            "旧版最后一日净值和metrics.txt总收益为不同口径，不能混用"])
    (OUT / "analysis_summary.json").write_text(json.dumps(findings, ensure_ascii=False, indent=2,
        default=json_default, allow_nan=False) + "\n")
    (OUT / "source_sha256.json").write_text(json.dumps(fingerprints, ensure_ascii=False, indent=2) + "\n")

    # 报告保留实际报表异常与归因假设，不把假设计算标成已修复的回测结果。
    expiry_view = expiry_reconciliation.reset_index().rename(columns={"instrument_id":"合约", "old_net_0709":"旧版期末仓位",
        "framework_net":"新版framework仓位", "native_net":"新版原生仓位", "hidden_expiry_delta":"隐藏到期成交变化"})
    expiry_view = expiry_view[["合约", "旧版期末仓位", "新版framework仓位", "新版原生仓位", "隐藏到期成交变化"]]
    scenario_view = pd.DataFrame([dict(口径=label, 期末权益=value["ending_equity"], 盈亏=value["profit"],
        收益率=f'{value["total_return"]:.4%}', 最大回撤=f'{value["maximum_drawdown"]:.4%}') for label, value in (
            ("旧版费前净值（原报表，共同日期）",scenarios["old_reported_gross_nav"]),
            ("新版补齐5笔到期平仓后费前（归因场景）",scenarios["scenario_new_gross_nav"]),
            ("旧版按成交文件列示费用扣减后",scenarios["old_nav_after_listed_fees"]),
            ("新版补齐平仓并扣费用（归因场景）",scenarios["scenario_new_net_nav"]))])
    trade_view = may29[["instrument_id", "side", "qty", "timestamp_new", "timestamp_old", "price_new", "price_old", "price_effect"]].rename(
        columns={"instrument_id":"合约", "side":"方向", "qty":"手数", "timestamp_new":"新版时间", "timestamp_old":"旧版时间", "price_new":"新版价格", "price_old":"旧版价格", "price_effect":"新版减旧版盈亏影响"})
    gross_chart = line_chart(daily, [("old_reported_gross_nav","旧版费前净值","#1d4ed8"),
        ("scenario_new_gross_nav","新版补齐到期平仓后的费前归因场景","#c2410c")])
    net_chart = line_chart(daily, [("old_nav_after_listed_fees","旧版扣列示费用后","#1d4ed8"),
        ("scenario_new_net_nav","新版补齐平仓并扣费用的归因场景","#c2410c")])
    source_list = "".join(f'<li>{html.escape(str(path.relative_to(BASE)))}</li>' for path in sources)
    assumption_list = "".join(f'<li>{html.escape(item)}</li>' for item in findings["attribution"]["assumptions"])
    limits = "".join(f'<li>{html.escape(item)}</li>' for item in findings["limitations"])
    report = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>目标持仓策略结果对比</title><style>body{{font:15px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#172033;max-width:1180px;margin:40px auto;padding:0 24px;background:#fff}}h1{{font-size:28px;font-weight:650}}h2{{font-size:21px;margin-top:36px}}p,li{{max-width:1050px}}.critical{{border-left:4px solid #b91c1c;padding:12px 18px;background:#fff5f5}}.note{{color:#526176}}.data{{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}}.data th{{background:#eaf0f7;text-align:center;padding:10px;border-bottom:1px solid #cbd5e1}}.data td{{text-align:right;padding:8px 10px;border-bottom:1px solid #e2e8f0}}.data td:first-child{{text-align:left}}.scroll{{overflow-x:auto}}.legend{{display:inline-block;margin:0 20px 12px 0}}svg{{width:100%;height:auto}}code{{background:#edf2f7;padding:1px 4px}}.small{{font-size:12px}}a{{color:#1d4ed8}}</style>
<h1>目标持仓策略结果对比</h1><p class="note">共同区间：2026-01-05 至 2026-07-09；初始资金：40,000,000 元。原始结果文件只读。</p>
<div class="critical">新版存在期货到期订单与持仓对账错误，不能把当前输出作为有效业绩。旧版净值为费前口径，且实际交易多一天。直接比较两份报表的收益数字会产生误判。</div>
<h2>主要结论</h2><ul><li>共同区间新版961条成交、旧版964条。按同日、同合约、同方向聚合并补齐5笔到期期货平仓后，716组买卖量全部一致。</li>
<li>新版原生持仓仍有181手已到期期货，framework记录271手；90手差额正好对应5笔有手续费记录、却没有正常成交记录的自动到期平仓。</li>
<li>对齐费前口径、共同日期，并按旧版实际到期平仓价补齐新版漏平后，新版价格影响为 {price_effect:,.2f} 元。其中5月29日IM2606三笔贡献 {delayed_effect:,.2f} 元，其余合计 {price_effect-delayed_effect:,.2f} 元。</li>
<li>旧版成交文件期货手续费为3bp，新版为0.23bp。旧版净值未扣列示费用；统一扣费后费用配置差异会成为主要影响。</li></ul>
<h2>实际报表及统计边界</h2><p>新版账户末余额为 {actual_cash:,.2f} 元。这是现金，包含尚未到期期权收取的权利金，不能当净值。旧版metrics.txt总收益为2,315,774.19元，可由旧版成交重建为未扣费的现金变化；其net_value.csv末日为2026-07-17，净值为{old_curve.iloc[-1]:,.2f}元，两者相差126,801.68元。</p>
<p>旧版2026-07-10多8笔成交，已从共同区间排除。旧版风险表从{risk.date.min()}开始，共{len(risk)}个日期，其中{int(risk.eod_exposure.eq(0).sum())}行日终敞口为零，与净值起止范围不同。</p>
<h2>期货到期错误的实证</h2><div class="scroll">{table(expiry_view)}</div>
<p>5笔 <code>EXPIRATION-LEG-*</code> 订单始终为 <code>INITIALIZED</code>、<code>filled_qty=0</code>。费用表却记录了90手成交及{hidden.commission.sum():,.2f}元手续费，原生持仓也减少90手。余下181手没有完成到期了结。<code>report_errors=[]</code>没有覆盖这些未归属的原生订单。</p>
<p>导出成交列示手续费为{listed_new_fees:,.2f}元，原生费用表为{actual_new_fees:,.2f}元，差额正是上述{hidden.commission.sum():,.2f}元。因此仅靠新版fills_output.csv无法完整重建原生账户。</p>
<h2>成交时点与价格</h2><p>同日、同合约、同方向、同手数配对后，399笔普通期权交易由旧版13:56移到新版14:00，共1157手；61笔到期期权由旧版14:59市场平仓变成新版16:00官方终值结算，共1023手。两笔普通期权委托在新版各拆成两笔成交，按日聚合后数量不变。</p>
<div class="scroll">{table(trade_view,money=("新版价格","旧版价格","新版减旧版盈亏影响"))}</div>
<p>旧版5月28日日终IM2606价格为8524.4，5月29日全天快照仍为8524.4，三笔成交为8524.4或8524.6。存在缺失行情或旧报价复用嫌疑；源M1未随结果提供，不能仅靠结果文件最终确认原因。</p>
<h2>统一口径后的归因场景</h2><p class="critical">下面是分析用场景，不是修复后原生重跑：将新版漏掉的5笔期货到期平仓按旧版实际价格补齐，并采用共同日终估值。不能把场景数字作为新版正式业绩。</p><ul>{assumption_list}</ul>
<div class="scroll">{table(scenario_view,money=("期末权益","盈亏"))}</div>
<p>费前成交价格影响：期货 {findings['attribution']['ordinary_future_price_effect']:,.2f} 元，普通期权 {findings['attribution']['ordinary_option_price_effect']:,.2f} 元，合计 {price_effect:,.2f} 元。旧版已按官方终值替换61笔期权平仓价格，合计调整{findings['attribution']['old_terminal_price_adjustment']:,.2f}元，故不重复算作时序价格差。</p>
{gross_chart}<p>旧版共同区间列示费用{old.commission.sum():,.2f}元；新版补齐到期平仓后的场景费用{scenario.commission.sum():,.2f}元，费用优势{fee_effect:,.2f}元。期权普通交易都按0.15元/手，但旧版到期平仓另扣153.45元，当前新版现金结算费设为0；其余费用差主要来自期货3bp与0.23bp的配置。连同费前价格影响，场景净差为{price_effect+fee_effect:,.2f}元，属于费用配置与执行价格的合并差异。</p>{net_chart}
<h2>下一步优先级</h2><ol><li>修复期货到期生命周期、订单状态和归属回报，新增期货到期验收，再重跑新版。</li><li>两边使用相同目标文件、结束日期、手续费配置及费前/费后口径。</li><li>核对5月29日旧版IM2606的M1源行情与报价更新时间，再选择正确数据基准。</li><li>统一期权撮合规则：各自合约下一根Bar、成交价来源及到期结算，不用时间标签近似宣称一致。</li></ol>
<h2>验证与限制</h2><p>旧版7月9日费前净值独立复算误差：{findings['reconciliation']['old_nav_residual']:.6f}元；新版现金余额（包含隐藏到期成交、扣费并按期权权利金处理）复算误差：{findings['reconciliation']['new_cash_residual']:.6f}元。输入文件SHA256在运行前后保持一致。</p><ul>{limits}</ul>
<h2>明细</h2><ul><li><a href="expiry_positions.csv">到期期货三方持仓对账</a></li><li><a href="hidden_expiry_fills.csv">隐藏到期成交与费用</a></li><li><a href="daily_contract_price_effects.csv">逐日逐合约价格、费用差</a></li><li><a href="may29_price_differences.csv">5月29日三笔差异</a></li><li><a href="daily_attribution_nav.csv">归因场景日净值及实际现金</a></li><li><a href="analysis_summary.json">结构化分析结果</a></li></ul>
<details><summary>输入文件</summary><ul class="small">{source_list}</ul></details></html>'''
    (OUT / "comparison_report.html").write_text(report)
    for path in sources:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == fingerprints[str(path)], path
    print(json.dumps({"report": str(OUT / "comparison_report.html"), "actual": findings["actual"],
        "attribution": findings["attribution"], "reconciliation": findings["reconciliation"]}, ensure_ascii=False, indent=2, default=json_default))


if __name__ == "__main__":
    main()
