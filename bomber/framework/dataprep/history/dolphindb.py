"""DolphinDB历史分钟Provider；可配置字段映射，构造时不加载SDK或连接。"""
from dataclasses import dataclass, field, replace
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import re
from threading import RLock
from .base import HistoryBar, HistoryProvider, HistoryError, HistoryUnavailable, select_bars, MINUTE_NS
from ...dolphindb import DolphinDbConnectionConfig
from ..sources.dolphindb import DolphinDbReferenceConfig, _session_factory
from ...calendars.sessions import SessionCalendar, datetime_ns, ns_datetime


@dataclass(frozen=True)
class DolphinDbHistoryConfig:
    connection: DolphinDbConnectionConfig | DolphinDbReferenceConfig
    database: str
    table: str
    columns: dict = field(default_factory=lambda: {"instrument_id": "instrument_id", "ts_event": "ts_event", "close": "close"})
    time_unit: str = "ns"
    time_label: str = "end_ns"
    instrument_format: str = "full"
    venue: str | None = None
    date_column: str | None = None
    time_column: str | None = None
    source_timezone: str = "UTC"
    date_basis: str = "calendar"
    volume_mode: str = "per-bar"
    # 运行时复用策略的会话日历，不在JSON中另存一份日期规则。
    session_calendar: SessionCalendar | None = field(default=None, repr=False, compare=False)
    time_column_kind: str = "time"

    def __post_init__(self):
        if re.fullmatch(r"dfs://[A-Za-z0-9_/-]+", self.database) is None:
            raise HistoryError("行情数据库须为安全的dfs路径")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.table) is None:
            raise HistoryError("行情表名无效；须提供实际表名")
        allowed = {"instrument_id", "ts_event", "close", "adjusted_close", "open", "high", "low", "volume", "cumulative_factor", "value", "open_interest"}
        columns = dict(self.columns)
        composite = self.date_column is not None or self.time_column is not None
        required = {"instrument_id"} if composite else {"instrument_id", "ts_event"}
        if (not required <= columns.keys()
                or not {"close", "adjusted_close"} & columns.keys() or not columns.keys() <= allowed
                or any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) is None for v in columns.values())):
            raise HistoryError("行情字段映射缺失或无效")
        if composite and (self.date_column is None or self.time_column is None or "ts_event" in columns
                or any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
                    for name in (self.date_column, self.time_column))):
            raise HistoryError("组合时间须同时提供安全date_column/time_column，不能同时映射ts_event")
        if self.source_timezone not in {"UTC", "Asia/Shanghai"}:
            raise HistoryError("历史源时区仅支持UTC或Asia/Shanghai，不能猜测偏移")
        if composite and (self.time_label not in {"start", "end"} or self.time_unit != "ns"):
            raise HistoryError("组合date/time须声明分钟开始或完成标签，统一转为纳秒")
        if self.time_unit not in {"ns", "ms", "s"} or self.time_label not in {"start", "end", "end_ns"}:
            raise HistoryError("须声明时间单位及分钟起点/末尾口径")
        if self.instrument_format not in {"full", "symbol"} or (self.instrument_format == "symbol"
                and (self.venue is None or re.fullmatch(r"[A-Z0-9]+", self.venue) is None)):
            raise HistoryError("symbol格式须明确固定Venue，不能猜测交易所")
        if self.time_column_kind not in {"time", "timestamp"}:
            raise HistoryError("time_column_kind须为time/timestamp")
        if self.time_column_kind == "timestamp" and (not composite or self.date_basis != "trading"):
            raise HistoryError("交易日分区的完整时间列须配置date_column/time_column及date_basis=trading")
        if self.date_basis not in {"calendar", "trading"} or self.volume_mode not in {"per-bar", "cumulative"}:
            raise HistoryError("历史日期口径须为calendar/trading，量额口径须为per-bar/cumulative")
        if self.date_basis == "trading" and (not composite
                or not isinstance(self.session_calendar, SessionCalendar)
                or str(self.session_calendar.zone) != self.source_timezone):
            raise HistoryError("交易日分钟须提供date/time字段及同一时区的会话日历")
        if self.volume_mode == "cumulative" and (self.date_basis != "trading"
                or not {"volume", "value"} <= columns.keys()):
            raise HistoryError("累计量额须同时映射volume/value，并使用交易日会话日历定位重置边界")
        object.__setattr__(self, "columns", columns)


class DolphinDbHistoryProvider(HistoryProvider):
    def __init__(self, config, *, session_factory=None):
        if not isinstance(config, DolphinDbHistoryConfig):
            raise TypeError("须提供DolphinDbHistoryConfig")
        self.config = config
        self._factory = session_factory or _session_factory
        self._session = None
        self._lock = RLock()

    def open(self):
        with self._lock:
            if self._session is not None:
                return
            session = self._factory(enableSSL=self.config.connection.enable_ssl)
            try:
                c = self.config.connection
                if session.connect(c.host, c.port, c.username, c.password, reconnect=False,
                        readTimeout=c.read_timeout_seconds, writeTimeout=c.read_timeout_seconds) is not True:
                    raise HistoryUnavailable("历史库未确认连接成功")
            except Exception:
                session.close()
                raise HistoryUnavailable("历史库连接失败") from None
            self._session = session

    def _script(self, request):
        if re.fullmatch(r"[A-Za-z0-9_.-]+", request.instrument_id) is None:
            raise HistoryError("合约标识无效")
        c = self.config
        symbol = request.instrument_id
        if c.instrument_format == "symbol":
            if not symbol.endswith("." + c.venue):
                raise HistoryError("请求交易所与历史库固定Venue不一致")
            symbol = symbol.rsplit(".", 1)[0]
        if c.date_basis == "trading":
            return self._trading_script(request, symbol)
        if c.date_column is not None:
            end = f"long(nanotimestamp(concatDateTime({c.date_column},{c.time_column})))"
            ordering = f"{c.date_column},{c.time_column}"
        else:
            scale = {"ns": 1, "ms": 1000000, "s": 1000000000}[c.time_unit]
            end = f"long({c.columns['ts_event']})*{scale}"
            ordering = c.columns['ts_event']
        if c.source_timezone == "Asia/Shanghai":
            end = f"({end}-{8 * 3600 * 1000000000}l)"
        if c.time_label == "start":
            end = f"({end}+{MINUTE_NS - 1}l)"
        elif c.time_label == "end":
            end = f"({end}-1l)"
        fields = ",".join(f"{physical} as {logical}" for logical, physical in c.columns.items() if logical != "ts_event")
        partition_filter = ""
        if c.date_column is not None:
            zone = timezone(timedelta(hours=8 if c.source_timezone == "Asia/Shanghai" else 0))
            # 完成标签00:00存于次日分区，框架时间则为前日23:59:59.999999999。
            label_offset = 1 if c.time_label == "end" else 0
            start = datetime.fromtimestamp((request.start_ns + label_offset) // 1000000000, zone).strftime("%Y.%m.%d")
            last = datetime.fromtimestamp((request.end_ns - 1 + label_offset) // 1000000000, zone).strftime("%Y.%m.%d")
            partition_filter = f"{c.date_column}>={start},{c.date_column}<={last},"
        # 只使用已验证名称、整数和合约字符串；读取多一行检测截断，禁止截断后当完整窗口。
        return (f'select top {request.max_rows + 1} {fields},{end} as ts_event '
            f'from loadTable("{c.database}","{c.table}") '
            f'where {partition_filter}{c.columns["instrument_id"]}="{symbol}",'
            f'{end}>={request.start_ns}l,{end}<{request.end_ns}l order by {ordering}')

    def _trading_labels(self, request):
        """交易日+时钟 -> 实际完成戳；周五夜盘、凌晨、例外均沿用SessionCalendar。"""
        calendar = self.config.session_calendar
        owners = set()
        first = (request.start_ns // MINUTE_NS + 1) * MINUTE_NS - 1
        for stamp in range(first, request.end_ns, MINUTE_NS):
            window = calendar.window(ns_datetime(stamp, calendar.zone))
            if window is not None:
                owners.add(window.trading_day)
        labels = {}
        for owner in sorted(owners):
            index = 0
            for window in calendar.windows(owner):
                for start in range(datetime_ns(window.start), datetime_ns(window.end), MINUTE_NS):
                    stamp = start + MINUTE_NS - 1
                    label = start if self.config.time_label == "start" else start + MINUTE_NS
                    key = (owner, ns_datetime(label, calendar.zone).strftime("%H:%M:%S"))
                    if key in labels:
                        raise HistoryError("同一交易日时钟对应多个分钟，历史时间无法唯一定位")
                    labels[key] = (stamp, index)
                    index += 1
        return labels

    def _trading_script(self, request, symbol):
        c = self.config
        prefix = re.match(r"[A-Za-z]+", symbol)
        if prefix is None or prefix.group().upper() != c.session_calendar.product:
            raise HistoryError("历史合约品种与会话日历不一致")
        labels = self._trading_labels(request)
        days = sorted({owner for owner, _ in labels})
        if not days:
            raise HistoryError("历史请求区间没有交易分钟")
        dates = ",".join(f"{day[:4]}.{day[4:6]}.{day[6:]}" for day in days)
        fields = ",".join(f"{physical} as {logical}" for logical, physical in c.columns.items())
        # 读取涉及的完整交易日：累计量额差分需要请求窗口之前的基准，不能先截窗口再diff。
        clock_field = (f'long(nanotimestamp({c.time_column})) as history_time_ns'
            if c.time_column_kind == "timestamp" else f'string({c.time_column}) as history_time')
        return (f'select top {request.max_rows + 1} {fields},'
            f'string({c.date_column}) as history_day,{clock_field} '
            f'from loadTable("{c.database}","{c.table}") '
            f'where {c.columns["instrument_id"]}="{symbol}",{c.date_column} in [{dates}] '
            f'order by {c.date_column},{c.time_column}')

    def _read_trading(self, rows, request):
        c = self.config
        labels = self._trading_labels(request)
        ordered = {}
        for source in rows:
            row = dict(source)
            owner = str(row.pop("history_day")).replace(".", "").replace("-", "")
            actual_stamp = None
            if c.time_column_kind == "timestamp":
                value = row.pop("history_time_ns")
                if int(value) != value:
                    raise HistoryError("历史完整时间戳精度丢失")
                label_ns = int(value) - (8 * 3600 * 10**9 if c.source_timezone == "Asia/Shanghai" else 0)
                if label_ns % MINUTE_NS:
                    raise HistoryError("历史完整时间戳须对齐分钟")
                actual_stamp = label_ns + (MINUTE_NS - 1 if c.time_label == "start" else -1)
                if actual_stamp >= request.end_ns:
                    continue
                point = ns_datetime(actual_stamp, c.session_calendar.zone)
                window = c.session_calendar.window(point)
                if window is None:
                    continue  # 盘外或收盘边界的快照不是完整交易分钟，也不能用作模型行情。
                clock = ns_datetime(label_ns, c.session_calendar.zone).strftime("%H:%M:%S")
                if window.trading_day != owner:
                    raise HistoryError("历史自然时间戳与交易日分区不一致")
            else:
                clock = str(row.pop("history_time"))
            try:
                parsed = datetime.strptime(clock.split(".")[0], "%H:%M:%S")
                if "." in clock and int(clock.split(".", 1)[1]) != 0:
                    raise ValueError("minute label has fractional seconds")
            except ValueError as error:
                raise HistoryError("历史分钟时钟无效") from error
            key = (owner, parsed.strftime("%H:%M:%S"))
            if key not in labels:
                raise HistoryError(f"历史交易日/分钟不在会话内: day={owner} time={clock}")
            stamp, index = labels[key]
            if actual_stamp is not None and actual_stamp != stamp:
                raise HistoryError("历史完整时间戳与交易会话定位不一致")
            if stamp >= request.end_ns:
                continue  # 不使用请求截止后的行，包括尚未完成的当前分钟。
            if c.instrument_format == "symbol":
                row["instrument_id"] = str(row["instrument_id"]) + "." + c.venue
            point = HistoryBar(ts_event=stamp, **row)
            entry = (owner, index, point)
            if stamp in ordered and ordered[stamp] != entry:
                raise HistoryError(f"历史同一分钟内容冲突: ts_event={stamp}")
            ordered[stamp] = entry
        bars, previous = [], None
        for stamp in sorted(ordered):
            owner, index, point = ordered[stamp]
            if c.volume_mode == "cumulative":
                current = point
                if previous is not None and previous[0] == owner:
                    if previous[1] + 1 != index:
                        if stamp >= request.start_ns:
                            raise HistoryError(f"累计历史缺少前一分钟，不能把跨分钟量额归入本分钟: ts_event={stamp}")
                        previous = (owner, index, current)
                        continue
                    volume = current.volume - previous[2].volume
                    value = current.value - previous[2].value
                elif index == 0:
                    volume, value = current.volume, current.value
                else:
                    # 窗口首行是盘中快照，只作差分基准；覆盖检查会拒绝缺少的目标分钟。
                    previous = (owner, index, current)
                    continue
                previous = (owner, index, current)
                if volume < 0 or value < 0 or (volume == 0 and value != 0):
                    raise HistoryError(f"累计历史量额倒退或不同步: ts_event={stamp} volume={volume} value={value}")
                point = replace(current, volume=volume, value=value)
            if stamp >= request.start_ns:
                bars.append(point)
        return select_bars(bars, request)

    def read(self, request):
        with self._lock:
            if self._session is None:
                raise HistoryUnavailable("历史库尚未打开")
            try:
                frame = self._session.run(self._script(request))
            except HistoryError:
                raise
            except Exception:
                raise HistoryUnavailable("历史分钟查询失败") from None
            if not hasattr(frame, "to_dict"):
                raise HistoryError("历史库须返回标准表格")
            rows = frame.to_dict("records")
            if len(rows) > request.max_rows:
                raise HistoryError("历史查询被行数上限截断")
            if self.config.date_basis == "trading":
                return self._read_trading(rows, request)
            bars = []
            for row in rows:
                stamp = row.pop("ts_event")
                if int(stamp) != stamp:
                    raise HistoryError("历史时间戳精度丢失")
                if self.config.instrument_format == "symbol":
                    row["instrument_id"] = str(row["instrument_id"]) + "." + self.config.venue
                bars.append(HistoryBar(ts_event=int(stamp), **row))
            return select_bars(bars, request)

    def close(self):
        with self._lock:
            session, self._session = self._session, None
            if session is not None:
                session.close()
