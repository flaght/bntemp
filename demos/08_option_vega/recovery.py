"""08自身的会话恢复与验收；通用账户、订单、归属检查使用公共模块。"""
import time
from copy import deepcopy
from decimal import Decimal
from bomber.framework.trader.runtime.live.channels.ctp_rollover import observe_rollover

from bomber.framework.trader.runtime.live.local import LocalExecutionLifecycle
from bomber.framework.trader.runtime.live.recovery import restored_target_revision
from bomber.framework.trader.runtime.live.channels.ctp import (CtpSessionLifecycle,
    occupied_positions)
from bomber.framework.trader.runtime.live.channels.ctp_recovery import (
    CtpResumeLifecycle, validate_ctp_restored_positions)


def validate_restored_session(session, day):
    validate_ctp_restored_positions(session, day, session.recovery_binding)
    revision = restored_target_revision(session, session.recovery_binding, allow_empty=True)
    session.strategy.restore_target_revision(revision)
    return revision


class OptionControl:
    def check_frames(self, session):
        if session.runner.failure or getattr(session.client, "report_errors", ()):
            raise RuntimeError(session.runner.failure or "执行回报冲突")
        from bomber.framework.trader.runtime.live.lifecycle import pause_requested
        if pause_requested(session):
            return
        session.references.refresh()
        session.runner.poll_features()
        if not session.runner.session_ready():
            raise RuntimeError(f"期权会话未就绪: {session.readiness.check().as_dict()}")
        stale = session.runner.stale_instruments()
        if stale:
            session.runner.close_role_gate(f"必需行情失效: {stale}")
        session.runner.flush_frames()

    def verify(self, session):
        if session.runner.failure or getattr(session.client, "report_errors", ()):
            raise RuntimeError(session.runner.failure or "执行回报冲突")
        if session.strategy.frames == 0 and not getattr(session.runner, "scheduled_pause", False):
            raise RuntimeError("没有取得封闭分钟帧")
        if self.require_fills and session.strategy.fills_received == 0:
            raise RuntimeError("本次未取得归属成交，不能通过成交验收")

    def snapshot(self, session, context):
        return {"resume_ready": self.resume_ready,
            "restored_generation": self.restored_generation,
            "bar_time_basis": "replay_test_mapped" if getattr(getattr(session, "bar_feed", None), "time_map", None) else "source_event",
            "valuation_day": self.valuation_day,
            "request_time_basis": "receive_wall_clock", "reference_time_basis": "observation_wall_clock",
            "trading_day": context.trading_day if context else None,
            "observed_td_trading_day": getattr(self, "_observed_td_day", None),
            "observed_md_trading_day": getattr(self, "_observed_md_day", None),
            "reference_manifest": {} if context is None or context.references is None else context.references.manifest,
            "orders_submitted": getattr(getattr(self, "driver", None), "submitted_orders", 0),
            "acceptance": "attributed_fills" if self.require_fills else "market_frames",
            "positions_retained_on_stop": True}


class OptionLocalLifecycle(OptionControl, LocalExecutionLifecycle):
    def __init__(self, trading_day, *, mode, valuation_day, resume=False, state_file=None,
                 ready_seconds=90, require_fills=False):
        super().__init__(trading_day, resume=resume, state_file=state_file, ready_seconds=ready_seconds)
        self.mode, self.valuation_day, self.require_fills = mode, valuation_day, require_fills
        self._observed_md_day = None

    @property
    def event_label(self):
        return "Recording目标" if self.mode == "recording" else "模型执行回报"

    def restore_session(self, session, context):
        saved = session.manager.repository.load()
        if saved is None or saved.payload["component_states"]["session_identity"]["identity"]["td_day"] == context.trading_day:
            return super().restore_session(session, context)
        return session.manager.restore(payload_transform=lambda payload: model_payload_for_day(session, payload))

    def poll(self, session):
        self.check_frames(session)
        return True

    def shutdown(self, session, context):
        if session is not None:
            session.strategy.stopping = True
            self._observed_md_day = getattr(session.upstream, "latest_trading_day", None)
        return super().shutdown(session, context)


class OptionCtpLifecycle(OptionControl, CtpResumeLifecycle, CtpSessionLifecycle):
    def __init__(self, *args, valuation_day, state_file, require_fills=False, **kwargs):
        super().__init__(*args, state_file=state_file, **kwargs)
        self.valuation_day, self.require_fills = valuation_day, require_fills
        self.bar_time_basis = "source_event"

    def validate_restored_session(self, session, day):
        return validate_restored_session(session, day)

    def restore_session(self, session, context):
        saved = session.manager.repository.load()
        if saved is None:
            return session.manager.restore()
        old = saved.payload["component_states"]["session_identity"]["identity"]
        if old["td_day"] == context.trading_day:
            return session.manager.restore()
        return session.manager.restore(payload_transform=lambda payload: payload_for_day(session, payload,
            driver=self.driver, transport=self.transport, day=context.trading_day))

    def poll(self, session):
        if self.lost:
            raise RuntimeError(self.lost[-1])
        self.check_frames(session)
        if getattr(session.client, "portfolio_failure", None):
            raise RuntimeError(session.client.portfolio_failure)
        if self.driver.submitted_orders >= self.max_session_orders:
            return False  # 公共停机流程撤单、核验与保存，保留已成交仓位。
        if time.monotonic() >= self._next_check:
            session.client.refresh_account_state()
            if self.account_reconciliation != "off":
                gross = self._managed_gross(occupied_positions(self.transport))
                expected = self._expected_gross(session)
                self.position_differences = {key: {"observed": gross.get(key, (0, 0)),
                    "execution": expected.get(key, (0, 0))} for key in gross.keys() | expected.keys()
                    if gross.get(key, (0, 0)) != expected.get(key, (0, 0))}
                if self.position_differences and self.account_reconciliation == "strict":
                    raise RuntimeError(f"柜台与策略成交账本不一致: {self.position_differences}")
            self._next_check = time.monotonic() + self.reconcile_seconds
        return True

    @staticmethod
    def _expected_gross(session):
        result = {}
        for instrument in session.position_scope.instruments:
            from bomber.framework.market.basic.base import InstrumentId
            snap = session.ledger.snapshot(InstrumentId.from_str(instrument))
            if snap.long_total or snap.short_total:
                result[instrument] = (snap.long_total, snap.short_total)
        return result

    def shutdown(self, session, context):
        if session is not None:
            session.strategy.stopping = True
        return super().shutdown(session, context)




def source_identity_with_legacy_metadata(sources):
    """只补旧版默认指数声明缺失的身份字段，指纹/来源仍须完全匹配。"""
    result = deepcopy(sources)
    index = result.get("index", {})
    missing = {key for key in ("alignment", "computation_version") if key not in index}
    if not missing or index.get("channel") != "dolphindb-dfs":
        return result
    import json
    from bomber.framework.dataprep.features import DEFAULT_FEATURE_CONFIG
    from bomber.framework.datahub.features.schema import dataset_from_definition
    definition = json.loads(DEFAULT_FEATURE_CONFIG.read_text())
    schema = next(row["schema"] for row in definition["datasets"]
                  if row["schema"]["dataset_id"] == "index.minute" and row["schema"]["schema_version"] == 1)
    spec = dataset_from_definition(schema)
    if (index.get("dataset_id"), index.get("schema_version"), index.get("schema_fingerprint")) == (
            spec.dataset_id, spec.schema_version, spec.fingerprint):
        values = {"alignment": spec.default_alignment.value, "computation_version": spec.computation_version}
        for key in missing:
            index[key] = values[key]
    return result


def validate_identity(old, new, *, mode="simnow"):
    allowed = {"td_day", "valuation_day", "calendar", "calendar_manifest", "reference_fingerprint", "contracts"}
    if old.get("mode") != mode or new.get("mode") != mode or old.get("environment") != "realtime" or new.get("environment") != "realtime":
        raise ValueError("08跨日自动恢复须使用相同实时执行模式")
    if mode == "simnow" and old.get("policy") != {"version": 1, "account_reconciliation": "strict", "position_source": "fills"}:
        raise ValueError("08跨日恢复须使用strict及真实成交归属")
    if not old["td_day"] < new["td_day"] or new["valuation_day"].replace("-", "") != new["td_day"]:
        raise ValueError("08恢复须向前跨日并使用当日估值日")
    if old["calendar_manifest"]["calendar_id"] != new["calendar_manifest"]["calendar_id"]:
        raise ValueError("08跨日不能更换交易日历")
    sources = source_identity_with_legacy_metadata(old["sources"])
    if sources != new.get("sources") or old.get("labels") != new.get("labels"):
        if (sources["market"]["channel"] != "ctp" or new["sources"]["market"]["channel"] != "dolphindb"
                or sources["index"] != new["sources"]["index"]
                or old["labels"]["index-bars"] != new["labels"]["index-bars"]):
            from bomber.framework.trader.runtime.live.recovery import identity_difference_paths
            raise ValueError("08跨日不能隐式更换指数特征或已有DolphinDB来源；差异字段="
                + ", ".join(identity_difference_paths(
                    {"sources": sources, "labels": old["labels"]},
                    {"sources": new["sources"], "labels": new["labels"]})))
        allowed.update(("sources", "labels"))
    elif sources != old["sources"]:
        allowed.add("sources")
    if any(old.get(key) != new.get(key) for key in old.keys() | new.keys() if key not in allowed):
        raise ValueError("08跨日恢复不能改变策略、风险或执行配置")


def restore_signal(session, state):
    signal = deepcopy(state)
    for name, expected in (("option_goals", session.strategy.contracts), ("future_goals", session.strategy.futures)):
        old = signal[name]
        if any(Decimal(value) for key, value in old.items() if key not in expected):
            raise ValueError("08恢复移除的合约仍有非零目标")
        signal[name] = {key: old.get(key, "0") for key in expected}
    signal["sent_days"] = [day for day in signal["sent_days"] if day in {str(d) for d in session.strategy.trading_days}]
    signal["hedge_sent"] = [token for token in signal.get("hedge_sent", ())
        if token.split("_", 1)[0] in {str(d) for d in session.strategy.trading_days}]
    session.signal_checkpoint.restore_state(signal)


class RecordingWindowState:
    """仅续接内存中的目标与每日选约标记；Recording始终没有真实成交仓位。"""
    def __init__(self, session):
        self.session = session

    def snapshot_state(self):
        return self.session.signal_checkpoint.snapshot_state()

    def restore_state(self, saved):
        signal = deepcopy(saved)
        # 已下架的观察目标不属于真实持仓，下一窗口不再订阅或记录该目标。
        for name, keys in (("option_goals", self.session.strategy.contracts),
                           ("future_goals", self.session.strategy.futures)):
            signal[name] = {key: value for key, value in signal[name].items() if key in keys}
        restore_signal(self.session, signal)
        self.session.strategy.restore_target_revision(saved["submissions"])
        # Recording只有目标记录，没有成交；不能以零成交事实覆盖已记录目标。
        self.session.strategy.restored = False


def rollover_routes(session, payload, *, mode="simnow"):
    new = session.session_identity
    old = payload["component_states"]["session_identity"]["identity"]
    validate_identity(old, new, mode=mode)
    sid = session.strategy.strategy_id
    owned = {row["target_key"]: Decimal(row["quantity"]) for row in payload["positions"]["strategy_positions"]
             if row["strategy_id"] == sid}
    ids = session.references.instrument_ids
    routes = {str(i) for i in ids.values()}
    if any(q for key, q in owned.items() if key not in routes):
        raise ValueError("08昨日持仓不在当日条款中，拒绝丢弃持仓")
    for key, q in owned.items():
        symbol = key.rsplit(".", 1)[0]
        if q and symbol in old["contracts"] and old["contracts"][symbol] != new["contracts"].get(symbol):
            raise ValueError("08持仓期权条款或同月对冲关系变化")
    targets = payload["targets"]
    if len(targets) != 1 or targets[0]["strategy_id"] != sid:
        raise ValueError("08原目标账本策略身份无效")
    if any(Decimal(q) for key, q in targets[0]["targets"].items() if key not in routes):
        raise ValueError("08移除合约仍有未完成旧目标")
    return owned, routes


def payload_for_day(session, payload, *, driver, transport, day):
    owned, routes = rollover_routes(session, payload)
    ids = session.references.instrument_ids
    multipliers = {instrument: session.references.contracts[key].multiplier if key in session.references.contracts
                   else session.references.futures[key]["contMultNum"] for key, instrument in ids.items()}
    session.rollover_audit = observe_rollover(payload, driver=driver, transport=transport, day=day,
        multipliers=multipliers, scope=session.position_scope)
    return rebind_rollover(session, payload, owned, routes, driver.driver_id)


def model_payload_for_day(session, payload):
    """模型仓位仍来自原成交账本；只更新当日路由和信号，不登录CTP。"""
    from bomber.framework.trader.persistence import _decode_orders
    if payload["ctp_ledgers"] or payload["ctp_drivers"] or any(
            not row.state.status.is_terminal for rows in payload["orders"].values() for row in _decode_orders(rows)):
        raise ValueError("模型跨日须无物理账户账本及未终结订单")
    owned, routes = rollover_routes(session, payload, mode="model")
    return rebind_rollover(session, payload, owned, routes, session.client.client_id)


def rebind_rollover(session, payload, owned, routes, client_id):
    sid, ids, new = session.strategy.strategy_id, session.references.instrument_ids, session.session_identity
    targets = payload["targets"]
    values = {key: str(owned.get(key, Decimal(0))) for key in sorted(routes)}
    targets[0]["targets"] = values
    payload["portfolio"]["contributions"][sid] = [
        {"key": {"client_id": client_id, "instrument_id": key}, "quantity": q} for key, q in values.items()]
    payload["portfolio"]["known_keys"] = [{"client_id": client_id, "instrument_id": key} for key in sorted(routes)]
    for name in ("strategy_positions", "strategy_revisions"):
        payload["positions"][name] = [row for row in payload["positions"][name] if row["target_key"] in routes]
    payload["positions"]["attribution"]["owned"] = [row for row in payload["positions"]["attribution"]["owned"]
        if row["target_key"] in routes or Decimal(row["quantity"])]
    signal = payload["component_states"]["vega_signal"]
    signal["option_goals"] = {key: values[str(ids[key])] for key in session.strategy.contracts}
    signal["future_goals"] = {key: values[str(ids[key])] for key in session.strategy.futures}
    signal["sent_days"] = [d for d in signal["sent_days"] if d in new["calendar"]]
    signal["hedge_sent"] = [token for token in signal.get("hedge_sent", ()) if token.split("_", 1)[0] in new["calendar"]]
    signal["flatten_requested"] = False
    payload["component_states"]["session_identity"]["identity"] = new
    return payload
