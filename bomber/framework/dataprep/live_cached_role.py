"""为单角色在线参考提供控制线程缓存，保留原适配器的读取接口。"""

from datetime import datetime
import time
import logging

from bomber.framework.datahub.sector_roles import SectorDataUnavailable
from .live_cache import LiveReferenceCache
from .sources.base import ReferenceSourceError
from .live_role import FileRoleReferences


class CachedRoleReferences:
    refresh_on_event = False

    def __init__(self, references):
        self.source = references
        for key in ("product", "role", "trading_day", "started_ns", "paths",
                    "factor_date_basis", "factor_availability"):
            setattr(self, key, getattr(references, key))
        self._published = LiveReferenceCache()
        self._retry_at_ns = 0
        self._retry_seconds = 5
        self._reducing = False
        self.refresh()

    @property
    def publication_lock(self):
        return self._published.publication_lock

    @publication_lock.setter
    def publication_lock(self, value):
        self._published.publication_lock = value

    def refresh(self):
        now = time.time_ns()
        if now < self._retry_at_ns:
            return
        previous_failure = self._published.manifest.get("refresh_failure")
        def load():
            assignment = self.source.snapshot(time.time_ns())
            return assignment, self.source.spec, self.source.manifest
        refreshed = self._published.refresh(load, transient_errors=(ReferenceSourceError,))
        if not refreshed:
            self._retry_at_ns = time.time_ns() + self._retry_seconds * 1_000_000_000
            manifest = self.manifest
            logging.getLogger(__name__).warning(
                "基础资料刷新失败，等待重试: product=%s retry_seconds=%s cached_ready=%s age_seconds=%s error=%s",
                self.product, self._retry_seconds, manifest['ready'],
                (time.time_ns() - manifest['observed_at_ns']) / 1e9, manifest['refresh_failure'])
        else:
            self._retry_at_ns = 0
            if previous_failure:
                logging.getLogger(__name__).info("基础资料刷新恢复: product=%s", self.product)
        _ = self.reduce_only  # 记录过期和恢复的状态变化。

    @property
    def spec(self):
        return self._published.value[1]

    @property
    def version(self):
        assignment = self._published.value[0]
        return (self.spec, assignment.instrument(self.product, self.role),
                assignment.factor(self.product, self.role))

    @property
    def manifest(self):
        return self._published.manifest

    @property
    def reduce_only(self):
        reducing = self._published.value is not None and not self.manifest['ready']
        if reducing != self._reducing:
            self._reducing = reducing
            logging.getLogger(__name__).warning(
                "基础资料执行状态变化: product=%s mode=%s reason=%s",
                self.product, 'REDUCE_ONLY' if reducing else 'NORMAL', self.manifest['failure'])
        return reducing

    @property
    def factor_date(self):
        return datetime.fromisoformat(self.manifest["factor_date"]).date()

    @property
    def instrument_id(self):
        return FileRoleReferences.instrument_id.fget(self)

    def instrument_meta(self):
        return FileRoleReferences.instrument_meta(self)

    def snapshot(self, as_of_ns):
        try:
            return self._published.snapshot(as_of_ns)
        except RuntimeError as error:
            # 未生效的参考仍遵循单角色策略已有的缺失资料处理接口。
            if self.reduce_only or str(error) == "当前参考版本在Bar时刻尚不可见":
                raise SectorDataUnavailable(str(error)) from error
            raise
