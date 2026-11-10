"""保留全部修订和接收证据；整批校验后原子发布。"""
from dataclasses import replace
from threading import RLock
from bisect import bisect_right, insort

from .contracts import Alignment, ExternalDataRecord, FeatureError, integer


class FeatureStore:
    def __init__(self, registry):
        self.registry = registry
        self._rows = {}
        self._slots = {}
        self._events = {}
        self._health = {}
        self._lock = RLock()
        self.generation = 0

    def publish(self, records, *, health_updates=()):
        with self._lock:
            # 只暂存本批增量。逐分钟发布时复制全部历史会导致平方级工作量。
            staged = {}
            grouped = {}
            for row in records:
                if not isinstance(row, ExternalDataRecord):
                    raise FeatureError("存储只接收ExternalDataRecord")
                spec = self.registry.get(row.dataset_id, row.schema_version)
                spec.validate_payload(row.payload)
                spec.frequency.close_ns(row.ts_event)
                old = staged.get(row.identity, self._rows.get(row.identity))
                if old is not None:
                    if replace(old, received_ns=None) != replace(row, received_ns=None):
                        raise FeatureError("相同业务槽与修订存在冲突")
                    continue  # 重读不改首次接收时间，不刷新源数据年龄。
                slot = row.identity[:-1]
                if slot not in grouped:
                    grouped[slot] = list(self._slots.get(slot, {}).values())
                siblings = grouped[slot]
                if any((item.revision < row.revision and item.available_ns > row.available_ns)
                       or (item.revision > row.revision and item.available_ns < row.available_ns)
                       for item in siblings):
                    raise FeatureError("修订序号与发布时间次序矛盾")
                staged[row.identity] = row
                siblings.append(row)
            health = {}
            for identity, observed_ns, ready_until_ns, code in health_updates:
                if len(identity) != 3 or not identity[2]:
                    raise FeatureError("来源健康须明确数据集、Schema及业务键")
                self.registry.get(identity[0], identity[1])
                integer(observed_ns, "来源观测时间")
                integer(ready_until_ns, "来源有效截止时间")
                if ready_until_ns < observed_ns or code is not None and (type(code) is not str or not code):
                    raise FeatureError("来源健康期限或错误码无效")
                if identity not in health:
                    health[identity] = list(self._health.get(identity, ()))
                events = health[identity]
                if events and observed_ns < events[-1][0]:
                    raise FeatureError("来源观测时钟回退")
                events.append((observed_ns, ready_until_ns, code))
            # 所有记录及健康事件校验通过后，在同一锁内提交；失败不污染任一索引。
            if staged or health:
                self._rows.update(staged)
                for row in staged.values():
                    slot = row.identity[:-1]
                    if slot not in self._slots:
                        self._slots[slot] = {}
                        events = self._events.setdefault(slot[:-1], [])
                        if not events or events[-1] < row.ts_event:
                            events.append(row.ts_event)
                        else:
                            insort(events, row.ts_event)
                    self._slots[slot][row.revision] = row
                self._health.update(health)
                self.generation += 1
            return self.generation

    def capture(self):
        with self._lock:
            return self.generation, tuple(self._rows.values())

    def capture_with_health(self):
        with self._lock:
            return self.generation, tuple(self._rows.values()), {
                key: tuple(events) for key, events in self._health.items()}

    def capture_for(self, requirements, query):
        """同代截取查询所需业务槽的全部修订；可见性/质量判断仍交给FeatureHub。"""
        with self._lock:
            captured, health = {}, {}
            for requirement in requirements:
                spec = self.registry.get(requirement.dataset_id, requirement.schema_version)
                identity = (requirement.dataset_id, requirement.schema_version, requirement.key)
                mode = requirement.alignment or spec.default_alignment
                if identity in self._health:
                    health[identity] = tuple(self._health[identity])
                if mode is Alignment.EXACT:
                    events = (query.event_ns,)
                else:
                    timeline = self._events.get(identity, ())
                    right = bisect_right(timeline, query.event_ns)
                    if mode is Alignment.WINDOW:
                        if requirement.window_ns is None or requirement.window_ns > query.event_ns:
                            events = ()  # 原Hub负责返回相同的参数错误。
                        else:
                            left = bisect_right(timeline, query.event_ns-requirement.window_ns)
                            events = timeline[left:right]
                    else:
                        # ASOF允许较早业务槽；未发布/尚未接收的最新槽不能遮挡可见旧槽。
                        events = ()
                        for index in range(right-1, -1, -1):
                            event = timeline[index]
                            if any(row.visible_ns <= query.as_of_ns for row in self._slots[(*identity, event)].values()):
                                events = (event,)
                                break
                for event in events:
                    for row in self._slots.get((*identity, event), {}).values():
                        captured[row.identity] = row
            return self.generation, tuple(captured.values()), health
