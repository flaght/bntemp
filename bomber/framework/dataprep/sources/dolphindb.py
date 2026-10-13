"""DolphinDB DFS 参考表的只读适配；与行情流订阅会话分开。"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from threading import RLock

from .base import ReferenceDataSource, ReferenceDataset, ReferenceQuery, ReferenceSourceError
from .normalize import normalize_dolphindb_frame, validate_hours_query


@dataclass(frozen=True)
class DolphinDbReferenceConfig:
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    database: str = "dfs://bomber_daily"
    futures_table: str = "fut_basic"
    options_table: str = "opt_basic"
    factors_table: str = "fut_adjustment_factors"
    structure_table: str = "fut_contract"
    read_timeout_seconds: int = 15
    max_rows: int = 100_000
    enable_ssl: bool = False
    hours_table: str = "trading_hours"
    overrides_table: str | None = None

    def __post_init__(self):
        if not self.host.strip() or not self.username or not self.password:
            raise ValueError("DolphinDB 连接配置不完整")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("DolphinDB 端口无效")
        if re.fullmatch(r"dfs://[A-Za-z0-9_/-]+", self.database) is None:
            raise ValueError("数据库须为 dfs:// 路径，只允许字母、数字、下划线、斜线和连字符")
        for table in (self.futures_table, self.options_table, self.factors_table, self.structure_table, self.hours_table):
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table) is None:
                raise ValueError("参考表名必须是普通标识符")
        if self.overrides_table is not None and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.overrides_table) is None:
            raise ValueError("例外表名必须是普通标识符")
        if type(self.read_timeout_seconds) is not int or not 1 <= self.read_timeout_seconds <= 120:
            raise ValueError("数据库读取超时须为1至120秒整数")
        if type(self.max_rows) is not int or self.max_rows < 1:
            raise ValueError("查询行数上限须为正整数")

    @classmethod
    def from_env(cls, *, database=None, read_timeout_seconds=15):
        def required(name):
            value = os.getenv(name, "").strip()
            if not value:
                raise ValueError(f"缺少环境变量: {name}")
            return value
        return cls(host=required("DDB_HOST"), port=int(os.getenv("DDB_PORT", "8848")),
            username=required("DDB_USERNAME"), password=required("DDB_PASSWORD"),
            database=database or os.getenv("DDB_REFERENCE_DATABASE", "dfs://bomber_daily"),
            futures_table=os.getenv("DDB_FUT_BASIC_TABLE", "fut_basic"),
            options_table=os.getenv("DDB_OPT_BASIC_TABLE", "opt_basic"),
            factors_table=os.getenv("DDB_FACTOR_TABLE", "fut_adjustment_factors"),
            structure_table=os.getenv("DDB_CONTRACT_TABLE", "fut_contract"),
            hours_table=os.getenv("DDB_TRADING_HOURS_TABLE", "trading_hours"),
            overrides_table=os.getenv("DDB_SESSION_OVERRIDES_TABLE") or None,
            read_timeout_seconds=read_timeout_seconds,
            enable_ssl=os.getenv("DDB_ENABLE_SSL", "false").lower() in {"1", "true", "yes"})


def _session_factory(**kwargs):
    try:
        import dolphindb
    except ImportError:
        raise ReferenceSourceError("DolphinDB 数据源需要在运行环境安装 dolphindb SDK") from None
    return dolphindb.session(**kwargs)


class DolphinDbReferenceSource(ReferenceDataSource):
    def __init__(self, config: DolphinDbReferenceConfig, *, session_factory=None):
        if not isinstance(config, DolphinDbReferenceConfig):
            raise TypeError("DolphinDB 数据源需要 DolphinDbReferenceConfig")
        self.config = config
        self._factory = session_factory or _session_factory
        self._session = None
        self._lock = RLock()

    def open(self):
        with self._lock:
            if self._session is not None:
                return
            session = None
            try:
                session = self._factory(enableSSL=self.config.enable_ssl)
                connected = session.connect(self.config.host, self.config.port,
                    self.config.username, self.config.password, reconnect=False,
                    readTimeout=self.config.read_timeout_seconds,
                    writeTimeout=self.config.read_timeout_seconds)
                if connected is not True:
                    raise ConnectionError("connect did not confirm success")
                self._session = session
            except Exception:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
                raise ReferenceSourceError("DolphinDB 连接失败；检查凭据、网络及SDK超时参数支持") from None

    def close(self):
        with self._lock:
            session, self._session = self._session, None
            if session is not None:
                try:
                    session.close()
                except Exception:
                    raise ReferenceSourceError("DolphinDB 会话关闭失败") from None

    def _table(self, dataset):
        if dataset is ReferenceDataset.SESSION_OVERRIDES:
            if self.config.overrides_table is None:
                raise ValueError("尚未配置session_overrides来源表")
            return self.config.overrides_table
        return {
            ReferenceDataset.FUTURES_BASIC: self.config.futures_table,
            ReferenceDataset.OPTIONS_BASIC: self.config.options_table,
            ReferenceDataset.ADJUSTMENT_FACTORS: self.config.factors_table,
            ReferenceDataset.CONTRACT_STRUCTURE: self.config.structure_table,
            ReferenceDataset.TRADING_HOURS: self.config.hours_table,
        }[dataset]

    def _script(self, dataset, query):
        # 所有可进入脚本的名称、代码及日期已受限校验，不接受任意SQL片段。
        if dataset in {ReferenceDataset.TRADING_HOURS, ReferenceDataset.SESSION_OVERRIDES}:
            validate_hours_query(query)
        conditions = []
        if query.start_date:
            conditions.append("date >= " + query.start_date.strftime("%Y.%m.%d"))
        if query.end_date:
            conditions.append("date <= " + query.end_date.strftime("%Y.%m.%d"))
        if query.products and dataset is not ReferenceDataset.OPTIONS_BASIC:
            field = "Code" if dataset in {ReferenceDataset.CONTRACT_STRUCTURE, ReferenceDataset.TRADING_HOURS,
                                         ReferenceDataset.SESSION_OVERRIDES} else "contractObject"
            values = ",".join('"' + p + '"' for p in query.products)
            conditions.append(f"upper({field}) in [{values}]")
        if query.symbols:
            if dataset is ReferenceDataset.CONTRACT_STRUCTURE:
                raise ValueError("期限结构应按 products 查询")
            values = ",".join('"' + s + '"' for s in query.symbols)
            conditions.append(f"upper(Code) in [{values}]")
        if query.active_on:
            if dataset not in {ReferenceDataset.FUTURES_BASIC, ReferenceDataset.OPTIONS_BASIC}:
                raise ValueError("active_on 仅用于合约基础资料")
            day = query.active_on.strftime("%Y.%m.%d")
            conditions.extend((f"date <= {day}", f"lastTradeDate >= {day}"))
        where = " where " + ", ".join(conditions) if conditions else ""
        # DDB TIME/MINUTE/SECOND are clock values, not epoch timestamps. Convert
        # these explicitly at the source boundary rather than guess SDK types.
        fields = ("Code,segment_id,date_anchor,start_day_offset,string(start_time) as start_time,"
                  "end_day_offset,string(end_time) as end_time"
                  if dataset is ReferenceDataset.TRADING_HOURS else "*")
        return (f'select top {self.config.max_rows + 1} {fields} from '
                f'loadTable("{self.config.database}", "{self._table(dataset)}"){where}')

    def read(self, dataset, query):
        dataset = ReferenceDataset(dataset)
        if not isinstance(query, ReferenceQuery):
            raise TypeError("查询需要 ReferenceQuery")
        script = self._script(dataset, query)
        with self._lock:
            if self._session is None:
                raise ReferenceSourceError("须先显式 open() 数据源")
            try:
                frame = self._session.run(script)
            except Exception:
                raise ReferenceSourceError(f"DolphinDB {dataset.value} 查询失败；禁止解释为空数据") from None
        if len(frame) > self.config.max_rows:
            raise ReferenceSourceError("参考查询超过行数上限，请缩小范围；不会返回截断资料")
        return normalize_dolphindb_frame(frame, dataset, query,
            source=f"dolphindb:{self.config.database}/{self._table(dataset)}")
