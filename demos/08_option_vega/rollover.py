"""08启动时跨日恢复已归属仓位；不重放昨日价格或未成交目标。"""
from copy import deepcopy
from decimal import Decimal

from bomber.framework.trader.runtime.live.channels.ctp_rollover import observe_rollover


def validate_identity(old, new):
    allowed = {"td_day", "valuation_day", "calendar", "calendar_manifest", "reference_fingerprint", "contracts"}
    if old.get("mode") != "simnow" or old.get("environment") != "realtime" or new.get("environment") != "realtime":
        raise ValueError("08跨日自动恢复仅支持实时SimNow")
    if old.get("policy") != {"version": 1, "account_reconciliation": "strict", "position_source": "fills"}:
        raise ValueError("08跨日恢复须使用strict及真实成交归属")
    if not old["td_day"] < new["td_day"] or new["valuation_day"].replace("-", "") != new["td_day"]:
        raise ValueError("08恢复须向前跨日并使用当日估值日")
    if old["calendar_manifest"]["calendar_id"] != new["calendar_manifest"]["calendar_id"]:
        raise ValueError("08跨日不能更换交易日历")
    if old.get("sources") != new.get("sources") or old.get("labels") != new.get("labels"):
        if (old["sources"]["market"]["channel"] != "ctp" or new["sources"]["market"]["channel"] != "dolphindb"
                or old["sources"]["index"] != new["sources"]["index"]
                or old["labels"]["index-bars"] != new["labels"]["index-bars"]):
            raise ValueError("08跨日不能隐式更换指数特征或已有DolphinDB来源")
        allowed.update(("sources", "labels"))
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
    session.signal_checkpoint.restore_state(signal)


def payload_for_day(session, payload, *, driver, transport, day):
    new = session.session_identity
    old = payload["component_states"]["session_identity"]["identity"]
    validate_identity(old, new)
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
    multipliers = {instrument: session.references.contracts[key].multiplier if key in session.references.contracts
                   else session.references.futures[key]["contMultNum"] for key, instrument in ids.items()}
    session.rollover_audit = observe_rollover(payload, driver=driver, transport=transport, day=day,
        multipliers=multipliers, scope=session.position_scope)
    values = {key: str(owned.get(key, Decimal(0))) for key in sorted(routes)}
    targets[0]["targets"] = values
    payload["portfolio"]["contributions"][sid] = [
        {"key": {"client_id": driver.driver_id, "instrument_id": key}, "quantity": q} for key, q in values.items()]
    payload["portfolio"]["known_keys"] = [{"client_id": driver.driver_id, "instrument_id": key} for key in sorted(routes)]
    for name in ("strategy_positions", "strategy_revisions"):
        payload["positions"][name] = [row for row in payload["positions"][name] if row["target_key"] in routes]
    payload["positions"]["attribution"]["owned"] = [row for row in payload["positions"]["attribution"]["owned"]
        if row["target_key"] in routes or Decimal(row["quantity"])]
    signal = payload["component_states"]["vega_signal"]
    signal["option_goals"] = {key: values[str(ids[key])] for key in session.strategy.contracts}
    signal["future_goals"] = {key: values[str(ids[key])] for key in session.strategy.futures}
    signal["sent_days"] = [d for d in signal["sent_days"] if d in new["calendar"]]
    signal["flatten_requested"] = False
    payload["component_states"]["session_identity"]["identity"] = new
    return payload
