"""按显式来源日及固定合约复权；不解释或覆盖主力角色。"""
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import re
import time as clock_time
from zoneinfo import ZoneInfo

from .base import HistoryError
from ..sources import ReferenceSourceFactory, ReferenceQuery, DataSourcePurpose


@dataclass(frozen=True)
class FactorAdjustmentResult:
    bars: tuple
    excluded: tuple[tuple[str, int], ...]
    missing_factors: tuple[dict, ...]


class ReferenceFactorAdjuster:
    def __init__(self, config, source_days, *, backend="dolphindb", factory=ReferenceSourceFactory,
                 timezone="Asia/Shanghai"):
        self.source_days = dict(source_days)
        if any(type(day) is not date or type(source) is not date or source >= day
                for day, source in self.source_days.items()):
            raise HistoryError("须显式映射交易日期到更早的因子来源日，不推断或跨日沿用")
        self.zone = ZoneInfo(timezone)
        self.config, self.backend, self.factory = config, backend, factory
        self.last_audit = []

    def _day(self, bar):
        return datetime.fromtimestamp(bar.ts_event // 1000000000, self.zone).date()

    def _declared_source_day(self, day):
        if day not in self.source_days:
            raise HistoryError(f"{day}未声明因子来源日，不能沿用其他日期因子")
        return self.source_days[day]

    def _resolve_source_day(self, source, day, product, declared):
        return declared

    def _availability(self, source_day):
        return (int(datetime.combine(source_day + timedelta(days=1), time(), self.zone).timestamp()) * 10**9,
                "declared_source_day_end")

    def _factor_batch(self, source, query):
        return source.adjustment_factors(query)

    def __call__(self, bars):
        return self._adjust(bars, allow_missing=False).bars

    def adjust_available(self, bars):
        """仅豁免成功查询得到零行的因子；保留每根被排除分钟及原因。"""
        return self._adjust(bars, allow_missing=True)

    def _adjust(self, bars, *, allow_missing):
        self.last_audit = []
        bars = tuple(bars)
        required = {}
        for bar in bars:
            if bar.adjusted_close is not None:
                continue
            day = self._day(bar)
            match = re.fullmatch(r"([A-Za-z]+)\d+\.[A-Z0-9]+", bar.instrument_id)
            if match is None or bar.close is None:
                raise HistoryError("历史复权须为固定真实合约且包含原始收盘价")
            symbol = bar.instrument_id.rsplit(".", 1)[0].upper()
            key = (day, bar.instrument_id)
            required[key] = (match[1].upper(), symbol, self._declared_source_day(day))
        factors, audits, missing_keys = {}, [], {}
        if required:
            source = self.factory.create_for(DataSourcePurpose.LIVE_REFERENCE, self.backend, self.config)
            try:
                source.open()
                for key, (product, symbol, source_day) in sorted(required.items()):
                    source_day = self._resolve_source_day(source, key[0], product, source_day)
                    batch = self._factor_batch(source, ReferenceQuery(products=(product,), symbols=(symbol,),
                        start_date=source_day, end_date=source_day))
                    if not batch.rows and allow_missing:
                        missing_keys[key] = {"trading_day": str(key[0]), "instrument": key[1],
                            "source_day": str(source_day), "reason": "missing_historical_factor",
                            "source": batch.source, "fingerprint": batch.fingerprint}
                        continue
                    if len(batch.rows) != 1:
                        raise HistoryError(f"{source_day}/{symbol}须有唯一累计因子，实际{len(batch.rows)}行")
                    row = batch.rows[0]
                    if (row["symbol"] != symbol or row["code"] != product or row["trade_date"] != source_day):
                        raise HistoryError("因子日期/合约/品种与请求不一致，禁止换合约或回退")
                    factor = Decimal(str(row["pcr_cumfactor"]))
                    if not factor.is_finite() or factor <= 0:
                        raise HistoryError("历史累计因子须为正且有限")
                    available, policy = self._availability(source_day)
                    if row.get("available_ns") is not None:
                        published = Decimal(str(row["available_ns"]))
                        if not published.is_finite() or published < 0 or published != published.to_integral_value():
                            raise HistoryError("因子available_ns须为非负整数纳秒")
                        available = max(available, int(published))
                        policy = "explicit_and_" + policy
                    factors[key] = (factor, available)
                    audits.append({"trading_day": str(key[0]), "instrument": key[1],
                        "source_day": str(source_day), "factor": str(factor),
                        "available_ns": available, "availability_policy": policy,
                        "source": batch.source, "fingerprint": batch.fingerprint,
                        "version_evidence": "current_read_not_historical_revision_asof"})
            finally:
                source.close()
        adjusted, excluded, excluded_by_day = [], [], {}
        for bar in bars:
            if bar.adjusted_close is not None:
                adjusted.append(bar)
                continue
            day = self._day(bar)
            if (day, bar.instrument_id) in missing_keys:
                excluded.append((bar.instrument_id, bar.ts_event))
                excluded_by_day.setdefault((day, bar.instrument_id), []).append(bar.ts_event)
                continue
            factor, available = factors[(day, bar.instrument_id)]
            if available > bar.ts_event:
                raise HistoryError("因子在该分钟尚不可用，禁止提前复权")
            adjusted.append(replace(bar, adjusted_close=bar.close * factor, cumulative_factor=factor))
        self.last_audit = audits
        missing_audit = []
        for (day, instrument), evidence in sorted(missing_keys.items()):
            stamps = excluded_by_day[(day, instrument)]
            missing_audit.append({**evidence, "bars": len(stamps),
                "first_ns": min(stamps), "last_ns": max(stamps)})
        return FactorAdjustmentResult(tuple(adjusted), tuple(excluded), tuple(missing_audit))


class SessionReferenceFactorAdjuster(ReferenceFactorAdjuster):
    """Live指标预热：按会话交易日选择角色来源日，不重放历史交易。

    无历史发布时间时仅声明当前读取的历史因子版本；真实available_ns仍门控。
    不将该模式作为历史回测的可见时间证据。
    """
    def __init__(self, config, calendar, *, factor_date_basis="source", max_source_age_days=14, **kwargs):
        super().__init__(config, {}, **kwargs)
        if factor_date_basis not in {"source", "trading"}:
            raise HistoryError("历史因子日期口径须为source或trading")
        self.calendar, self.factor_date_basis = calendar, factor_date_basis
        self.max_source_age_days = max_source_age_days

    def _adjust(self, bars, *, allow_missing):
        result = super()._adjust(bars, allow_missing=allow_missing)
        observed = clock_time.time_ns()
        for audit in self.last_audit:
            audit.update(observed_at_ns=observed, factor_date_basis=self.factor_date_basis,
                         purpose="live_indicator_warmup_not_historical_replay")
        for audit in result.missing_factors:
            audit.update(observed_at_ns=observed, factor_date_basis=self.factor_date_basis,
                         purpose="live_indicator_warmup_not_historical_replay")
        return result

    def _factor_batch(self, source, query):
        first, second = source.adjustment_factors(query), source.adjustment_factors(query)
        if first.fingerprint != second.fingerprint:
            raise HistoryError("历史累计因子读取期间变化，拒绝复权")
        return second

    def _day(self, bar):
        from bomber.framework.calendars.sessions import ns_datetime
        window = self.calendar.window(ns_datetime(bar.ts_event, self.zone))
        if window is None:
            raise HistoryError("历史分钟不属于已声明交易会话，不能推断因子交易日")
        return datetime.strptime(window.trading_day, "%Y%m%d").date()

    def _declared_source_day(self, day):
        return None

    def _resolve_source_day(self, source, day, product, declared):
        if self.factor_date_basis == "trading":
            return day
        from ..references import parse_role_assignments
        query = ReferenceQuery(products=(product,), end_date=day - timedelta(days=1))
        first, second = source.contract_structure(query), source.contract_structure(query)
        if first.fingerprint != second.fingerprint:
            raise HistoryError("历史角色资料读取期间变化，拒绝选择因子来源日")
        roles = parse_role_assignments(second.frame().rename(columns={"date": "trade_date"}),
            (product,), ("main",), source=second.source)
        rows = roles.loc[roles.code.eq(product) & roles.source_day.lt(day)]
        if rows.empty:
            raise HistoryError(f"{day}/{product}缺少交易日前角色资料，不能选择因子来源日")
        selected = rows.iloc[-1].source_day
        if (day - selected).days > self.max_source_age_days:
            raise HistoryError("历史角色来源日过旧，拒绝复权")
        return selected

    def _availability(self, source_day):
        return 0, "current_read_indicator_warmup_only"
