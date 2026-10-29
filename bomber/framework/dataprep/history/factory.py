"""历史行情Provider工厂；扩展后端只需注册，不改策略或恢复协调器。"""
from .base import HistoryProvider
from .file import FileHistoryProvider
from .dolphindb import DolphinDbHistoryProvider
from ..sources.policy import DataSourcePurpose


class MongoDbHistoryProvider(HistoryProvider):
    def __init__(self, config, **kwargs):
        self.config = config
    def open(self):
        raise NotImplementedError("MongoDB历史Provider尚未接入；可通过工厂注册替代实现")
    def read(self, request):
        raise NotImplementedError("MongoDB历史Provider尚未接入")
    def close(self):
        pass


class HistoryProviderFactory:
    _providers = {"file": FileHistoryProvider, "dolphindb": DolphinDbHistoryProvider, "mongodb": MongoDbHistoryProvider}

    @classmethod
    def backends(cls):
        return tuple(cls._providers)

    @classmethod
    def create_for(cls, purpose, backend, config, **kwargs):
        purpose = DataSourcePurpose(purpose)
        source = cls._providers.get(str(backend).strip().lower())
        if source is None:
            raise ValueError("历史Provider未注册")
        allowed = {DataSourcePurpose.OFFLINE_MARKET_HISTORY: {"file"},
            DataSourcePurpose.LIVE_MARKET_HISTORY: {"file", "database"},
            DataSourcePurpose.LIVE_RECOVERY: {"database"}}
        if purpose not in allowed or source.storage_kind not in allowed[purpose]:
            raise ValueError("用途不允许该历史Provider，禁止静默回退")
        return cls.create(backend, config, **kwargs)

    @classmethod
    def create(cls, backend, config, **kwargs):
        name = str(backend).strip().lower()
        if name not in cls._providers:
            raise ValueError(f"未知历史行情Provider: {name}")
        return cls._providers[name](config, **kwargs)

    @classmethod
    def register(cls, backend, provider_type, *, replace=False):
        name = str(backend).strip().lower()
        if not name or (name in cls._providers and not replace):
            raise ValueError("历史Provider名称为空或已注册")
        if not isinstance(provider_type, type) or not issubclass(provider_type, HistoryProvider):
            raise TypeError("工厂只接受HistoryProvider子类")
        cls._providers[name] = provider_type


def build_history_service(*, history_config=None, history_file=None, calendar=None,
                          required_fields=(), missing_policy="fail", connection=None):
    """组装普通历史行情JSON和服务；不打开连接，不创建另一份会话日历。

    字段需求和缺失政策由业务显式声明。文件优先级、盘中恢复禁用文件、
    Provider去重和故障语义继续由HistoryService负责。
    """
    import json
    from pathlib import Path
    from .base import HistoryError
    from .dolphindb import DolphinDbHistoryConfig
    from .service import HistoryService
    from ...dolphindb import DolphinDbConnectionConfig

    if missing_policy not in {"fail", "allow"}:
        raise HistoryError("历史缺失策略须为fail/allow")
    config = None
    if history_config is not None:
        values = json.loads(Path(history_config).read_text())
        if not isinstance(values, dict):
            raise HistoryError("历史配置须为JSON对象")
        if values.get("date_basis") == "trading":
            if calendar is None:
                raise HistoryError("交易日历史预热缺少会话日历")
            values["session_calendar"] = calendar
        values.setdefault("allow_missing_minutes", missing_policy == "allow")
        config = DolphinDbHistoryConfig(connection=(connection if connection is not None else
            DolphinDbConnectionConfig.from_env(purpose="market")), **values)
        missing = set(required_fields) - config.columns.keys()
        if missing:
            raise HistoryError(f"历史配置缺少所需字段: {sorted(missing)}")
    return HistoryService(database_config=config, file_path=history_file, missing_policy=missing_policy)
