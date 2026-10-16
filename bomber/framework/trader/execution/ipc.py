"""同机账户执行协议：JSON、Unix socket和同代持久化事件，不导入策略应用。"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import fields, is_dataclass
from decimal import Decimal
from enum import Enum
import hashlib
import hmac
import json
import secrets
import socket
import threading
import time

MAX_MESSAGE = 1_048_576
MAX_PRICE_AGE_NS = 120_000_000_000


def wire(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return {field.name: wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): wire(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire(v) for v in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def encode(value):
    raw = json.dumps(wire(value), ensure_ascii=False, allow_nan=False, sort_keys=True).encode() + b"\n"
    if len(raw) > MAX_MESSAGE:
        raise ValueError("执行协议消息超过上限")
    return raw


def receive(connection):
    data = bytearray()
    while not data.endswith(b"\n"):
        part = connection.recv(min(65536, MAX_MESSAGE + 1 - len(data)))
        if not part:
            raise ConnectionError("执行协议连接提前结束")
        data.extend(part)
        if len(data) > MAX_MESSAGE:
            raise ValueError("执行协议消息超过上限")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("执行协议消息须为对象")
    return value


def decode_event(row):
    from .events import ExecutionIdentity, FillEvent, OrderUpdateEvent
    if row["type"] not in {"FillEvent", "OrderUpdateEvent"}:
        raise ValueError("未知持久化执行事件类型")
    value = dict(row["event"])
    value["identity"] = ExecutionIdentity(**value["identity"])
    return (FillEvent if row["type"] == "FillEvent" else OrderUpdateEvent)(**value)


class ExecutionEventJournal:
    """与账户订单/成交同代保存；确认不删除原记录，支持重启补交和审计。"""
    def __init__(self, strategy_ids, *, max_events=10000):
        self.ids, self.max_events = frozenset(strategy_ids), max_events
        self.events = {sid: [] for sid in self.ids}
        self.ack = {sid: 0 for sid in self.ids}
        self.requests = {}
        self._lock = threading.RLock()

    def append(self, sid, event):
        with self._lock:
            rows = self.events[sid]
            if any(row["event"]["event_id"] == event.event_id for row in rows):
                return
            if len(rows) >= self.max_events:
                raise RuntimeError("持久化策略事件日志已满，拒绝丢弃回报")
            rows.append({"sequence": len(rows) + 1, "type": type(event).__name__, "event": wire(event)})

    def acknowledge(self, sid, cursor):
        with self._lock:
            if isinstance(cursor, bool) or not isinstance(cursor, int) or not self.ack[sid] <= cursor <= len(self.events[sid]):
                raise ValueError("策略事件确认游标无效")
            self.ack[sid] = cursor

    def read(self, sid, cursor):
        with self._lock:
            if not 0 <= cursor <= len(self.events[sid]):
                raise ValueError("策略事件游标超前")
            return deepcopy(self.events[sid][cursor:cursor + 100])

    def snapshot_state(self):
        with self._lock:
            return deepcopy({"version": 1, "events": self.events, "ack": self.ack, "requests": self.requests})

    def restore_state(self, state):
        if state.get("version") != 1 or set(state["events"]) != self.ids or set(state["ack"]) != self.ids:
            raise ValueError("策略事件日志恢复身份不符")
        for sid, rows in state["events"].items():
            if (len(rows) > self.max_events or not 0 <= state["ack"][sid] <= len(rows)
                    or [row["sequence"] for row in rows] != list(range(1, len(rows) + 1))):
                raise ValueError("策略事件日志恢复序列无效")
            for row in rows:
                event = decode_event(row)
                if event.identity.strategy_id != sid:
                    raise ValueError("事件日志包含其他策略")
        with self._lock:
            self.events, self.ack, self.requests = deepcopy((state["events"], state["ack"], state["requests"]))


class AccountExecutionProtocol:
    """账户控制线程处理请求；单策略连接租约不等于物理账户租约。"""
    def __init__(self, service, *, keys, trading_day, lease_seconds=30, stop_seconds=15):
        if service.execution_mode != "independent" or set(keys) != set(service._ports):
            raise ValueError("独立进程协议须注册完整独立订单服务")
        self.service, self.keys, self.trading_day = service, dict(keys), trading_day
        self.lease_seconds, self.stop_seconds = lease_seconds, stop_seconds
        self.leases = {}
        self.closed_tokens = {}
        self.stopped_acknowledged = set()
        if service.event_journal is None:
            service.event_journal = ExecutionEventJournal(keys)
        self.journal = service.event_journal
        for sid, port in service._ports.items():
            port.ready_check = lambda sid=sid: bool(self.leases.get(sid, {}).get("ready")
                and time.monotonic() < self.leases[sid]["deadline"])

    def state(self, sid):
        with self.service.client._submit_lock:
            return self._state_locked(sid)

    def _state_locked(self, sid):
        service, port = self.service, self.service._ports[sid]
        source = service.snapshot_state()["sources"].get(sid)
        return {"client_id": service.client_id, "account_id": service.account_id,
            "trading_day": self.trading_day, "configuration": port.configuration(),
            "reconciled": port.is_reconciled and getattr(service.client, "is_armed", True),
            "source_revision": source["revision"] if source else 0,
            "positions": {key: str(service.position_manager.position(sid, key)) for key in port.routes},
            "working": {key: str(service.position_manager.attribution.working_quantity(
                service.client_id, instrument, sid, key)) for key, instrument in port.routes.items()},
            "ack": self.journal.ack[sid], "execution_stop": self.service.execution_stop}

    def stop_owner(self, sid):
        port = self.service._ports[sid]
        if port._started:
            port.begin_stop()
            deadline = time.monotonic() + self.stop_seconds
            while True:
                self.service.poll()
                if port.finish_stop_events():
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("策略连接停止未取得撤单终态，保留账户状态")
                time.sleep(0.02)
            port.stop()
        lease = self.leases.pop(sid, None)
        if lease:
            self.closed_tokens[sid] = lease["token"]
            self.stopped_acknowledged.discard(sid)
        self.service._save()

    def poll(self):
        for sid, lease in tuple(self.leases.items()):
            if time.monotonic() >= lease["deadline"]:
                self.stop_owner(sid)
        self.service.poll()

    def _update_prices(self, port, prices, *, discard_expired=False):
        from bomber.framework.market.basic.base import InstrumentId
        now, updates, expired = time.time_ns(), [], []
        for instrument, row in prices.items():
            parsed = InstrumentId.from_str(instrument)
            stamp, price = row["ts_event"], Decimal(row["price"])
            valid_stamp = not isinstance(stamp, bool) and isinstance(stamp, int) and stamp > 0
            age = now - stamp if valid_stamp else None
            reason = ("foreign_route" if parsed not in port.routes.values() else
                "invalid_timestamp" if not valid_stamp else
                "invalid_price" if not price.is_finite() or price <= 0 else
                "future" if age < 0 else "expired" if age > MAX_PRICE_AGE_NS else None)
            if reason is not None:
                context = {"strategy_id": port.strategy_id, "instrument_id": instrument, "reason": reason,
                    "price": str(price), "ts_event": stamp, "checked_at_ns": now,
                    "age_seconds": age / 1e9 if age is not None else None,
                    "max_age_seconds": MAX_PRICE_AGE_NS / 1e9}
                if discard_expired and reason == "expired":
                    expired.append(context)
                    continue
                raise ValueError(f"策略价格路由或时效不符: {context}")
            updates.append((parsed, price, stamp))
        for instrument, price, stamp in updates:
            self.service.prices.update(instrument, price, stamp)
        return expired

    def handle(self, message):
        from bomber.framework.market.basic.base import InstrumentId
        from bomber.framework.trader.contracts import ExecutionRequest
        if message.get("version") != 1:
            raise ValueError("执行协议版本不符")
        sid = message.get("strategy_id")
        if sid not in self.keys or not hmac.compare_digest(str(message.get("key", "")), self.keys[sid]):
            raise ValueError("执行协议身份不符")
        action, data = message.get("action"), message.get("data", {})
        port = self.service._ports[sid]
        if action == "describe":
            return {"state": self.state(sid)}
        if action == "ack_stopped":
            if (sid in self.leases or not hmac.compare_digest(str(message.get("lease", "")),
                                                             self.closed_tokens.get(sid, ""))):
                raise ValueError("已停止连接的回报确认身份不符")
            self.journal.acknowledge(sid, data["cursor"])
            self.stopped_acknowledged.add(sid)
            self.service._save()
            return {"state": self.state(sid)}
        if action == "attach":
            if sid in self.leases:
                raise RuntimeError("该策略已有活动进程；先停止或等待连接租约到期")
            if data.get("configuration") != port.configuration():
                raise ValueError("策略进程路由或限额与账户声明不一致")
            if sid in self.service._draining:
                raise RuntimeError("旧策略撤单尚未完成")
            port.start()
            self.closed_tokens.pop(sid, None)
            self.stopped_acknowledged.discard(sid)
            token = secrets.token_hex(24)
            self.leases[sid] = {"token": token, "ready": False,
                                "deadline": time.monotonic() + self.lease_seconds}
            return {"lease": token, "state": self.state(sid)}
        lease = self.leases.get(sid)
        if not lease or not hmac.compare_digest(str(message.get("lease", "")), lease["token"]):
            raise ValueError("策略连接租约失效")
        if time.monotonic() >= lease["deadline"]:
            self.stop_owner(sid)
            raise RuntimeError("策略连接租约已到期")
        lease["deadline"] = time.monotonic() + self.lease_seconds
        if action == "stop":
            self.stop_owner(sid)
            return {"state": self.state(sid), "events": self.journal.read(sid, data["cursor"])}
        if action == "cancel":
            port.cancel_strategy(sid)
        elif action == "poll":
            # 客户端筛选后，排队/传输可让临界参考价过期。只丢弃该观测，
            # 不用旧价更新账户，也不能让回报交付被行情同步阻断；报单仍严格校验。
            expired = self._update_prices(port, data.get("prices", {}), discard_expired=True)
            lease["ready"] = bool(data.get("ready", False))
            self.journal.acknowledge(sid, data["cursor"])
            self.service._save()
            with self.service.client._submit_lock:
                return {"state": self.state(sid), "events": self.journal.read(sid, data["cursor"]),
                    "expired_prices": expired}
        elif action in {"submit", "continue"}:
            if self.service.execution_stop is not None:
                return {"state": self.state(sid), "accepted": False,
                        "reason": self.service.execution_stop["reason"]}
            # Tick可能已使本地健康恢复，而下一次周期poll尚未到达；发送请求携带
            # 本次检查结果。旧客户端继续使用最近poll，不能把缺失字段当作True。
            ready = data.get("ready", lease["ready"])
            if not isinstance(ready, bool):
                raise ValueError("策略就绪声明须为布尔值")
            if not ready:
                lease["ready"] = False
                raise RuntimeError("策略行情/参考尚未就绪")
            self._update_prices(port, data["prices"])
            lease["ready"] = ready  # 价格/路由校验完成后更新，拒绝请求不解除旧闭闸。
            if action == "continue":
                port.continue_target(sid, data["target_key"], data["now_ns"],
                    instrument_id=InstrumentId.from_str(data["instrument_id"]))
            else:
                value = dict(data["request"])
                if value["strategy_id"] != sid or value["client_id"] != self.service.client_id:
                    raise ValueError("请求不能冒用其他策略或账户")
                key = f"{sid}:{message['request_id']}"
                digest = hashlib.sha256(encode(data)).hexdigest()
                previous = self.journal.requests.get(key)
                if previous is not None:
                    if previous["digest"] != digest or previous["status"] != "completed":
                        raise RuntimeError("请求冲突或发送未决，拒绝重复发送")
                    return {"state": self.state(sid), "duplicate": True}
                if len(self.journal.requests) >= 10000:
                    raise RuntimeError("执行请求日志已满")
                self.journal.requests[key] = {"digest": digest, "status": "pending"}
                self.service._save()  # 写前请求记录；异常不猜测是否已发送。
                value["targets"] = {InstrumentId.from_str(i): Decimal(q) for i, q in value["targets"].items()}
                port.submit_targets(ExecutionRequest(**value))
                self.journal.requests[key]["status"] = "completed"
                self.service._save()
        else:
            raise ValueError("未知执行协议动作")
        return {"state": self.state(sid)}


class UnixExecutionServer:
    def __init__(self, path, protocol):
        import os
        self.path, self.protocol = path, protocol
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket.bind(str(path))  # 不删除其他活动/未确认服务的socket。
            os.chmod(path, 0o600)
            self.socket.listen(8)
            self.socket.setblocking(False)
        except BaseException:
            self.socket.close()
            raise

    def poll(self):
        try:
            connection, _ = self.socket.accept()
        except BlockingIOError:
            return
        with connection:
            connection.settimeout(1)
            try:
                response = {"ok": True, "result": self.protocol.handle(receive(connection))}
            except Exception as error:
                response = {"ok": False, "error": str(error)}
            try:
                connection.sendall(encode(response))
            except (BrokenPipeError, ConnectionError):
                pass  # 已执行请求保留持久化记录，丢回复不能导致再次报单。

    def close(self):
        self.socket.close()
        self.path.unlink(missing_ok=True)


class UnixExecutionConnection:
    def __init__(self, path, sid, key, *, timeout=60):
        self.path, self.sid, self.key, self.timeout = path, sid, key, timeout
        self.lease = None
        self._lock = threading.RLock()

    def call(self, action, data, *, request_id=None):
        with self._lock, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(str(self.path))
            connection.sendall(encode({"version": 1, "strategy_id": self.sid, "key": self.key,
                "lease": self.lease, "request_id": request_id or secrets.token_hex(16),
                "action": action, "data": data}))
            response = receive(connection)
            if not response.get("ok"):
                raise RuntimeError(response.get("error", "执行协议失败"))
            result = response["result"]
            if action == "attach":
                self.lease = result["lease"]
            return result
