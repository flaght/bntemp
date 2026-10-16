"""应用层共享会话跨日身份核对；算法参数及非08固定参考不能隐式改变。"""
from copy import deepcopy
from decimal import Decimal
import logging

from bomber.framework.trader.runtime.live.channels.ctp_rollover import observe_rollover


def validate_strategy_identity(old, new):
    previous, current = deepcopy(old), deepcopy(new)
    if not previous["trading_day"] < current["trading_day"] or current["environment"] != "realtime":
        raise ValueError("共享跨日恢复仅支持向前的realtime交易日")
    previous["trading_day"] = current["trading_day"]
    if new["kind"] == "option_vega" and "reference" in new:
        from importlib import import_module
        import_module("demos.08_option_vega.rollover").validate_identity(previous["reference"], current["reference"])
        previous["reference"] = current["reference"]
        old_routes, new_routes = previous["routes"], current["routes"]
        for key in set(old_routes["routes"]) & set(new_routes["routes"]):
            instrument = old_routes["routes"][key]
            if instrument != new_routes["routes"][key] or old_routes["limits"][instrument] != new_routes["limits"][instrument]:
                raise ValueError("08跨日已有路由或风险限额变化")
        if old_routes["budget"]["max_gross_notional"] != new_routes["budget"]["max_gross_notional"]:
            raise ValueError("08跨日不能改变策略名义预算")
        previous["routes"] = current["routes"]
    if previous != current:
        raise ValueError("共享跨日不能更改策略身份、路由、预算、参数或固定参考")


def payload_for_day(account, payload):
    if "vega_signal" in payload["component_states"]:
        return import_option_payload(account, payload)
    old = payload["component_states"]["session_identity"]["identity"]
    new = account.manager.state_components["session_identity"].identity
    if old["strategies"].keys() != new["strategies"].keys():
        raise ValueError("跨日不能更改账户策略集合")
    candidate = deepcopy(old)
    for sid in new["strategies"]:
        validate_strategy_identity(old["strategies"][sid], new["strategies"][sid])
    candidate["strategies"] = new["strategies"]
    candidate["trading_day"] = new["trading_day"]
    if old.get("managed_instruments") != new.get("managed_instruments"):
        before = set(old.get("managed_instruments", [])); after = set(new.get("managed_instruments", []))
        affected = before ^ after
        option_routes = set()
        for sid, row in new["strategies"].items():
            if row["kind"] == "option_vega":
                option_routes.update(old["strategies"][sid]["routes"]["routes"].values())
                option_routes.update(row["routes"]["routes"].values())
        if not affected <= option_routes:
            raise ValueError("跨日管理范围变化包含非08路由")
        candidate["managed_instruments"] = new.get("managed_instruments")
    if candidate != new:
        raise ValueError("跨日不能更改账户身份、执行模式或管理范围")
    shared = payload["component_states"]["shared_account"]
    if shared.get("failed") or shared.get("pending") or shared.get("shared", {}).get("draining"):
        raise ValueError("共享跨日存在未决发送或未完成停止")
    journal = shared.get("shared", {}).get("event_journal", {})
    if any(row["status"] != "completed" for row in journal.get("requests", {}).values()):
        raise ValueError("共享跨日存在未决跨进程请求")
    # 08候选链可以增加/移除零仓合约；持仓、旧目标或在途非零均拒绝移除。
    current_routes = account.service.snapshot_state()["shared"]["routes"]
    for sid, declaration in current_routes.items():
        if new["strategies"][sid]["kind"] != "option_vega":
            continue
        old_routes = shared["shared"]["routes"][sid]["routes"]
        routes = declaration["routes"]
        removed = set(old_routes)-set(routes)
        for row in payload["positions"]["strategy_positions"]:
            if row["strategy_id"] == sid and row["target_key"] in removed and Decimal(row["quantity"]):
                raise ValueError("08移除的候选仍有归属持仓")
        source = shared["sources"].get(sid)
        if source:
            for row in source["rows"]:
                if row["target_key"] in removed and Decimal(row["quantity"]):
                    raise ValueError("08移除的候选仍有非零旧目标")
            values = {key: source["targets"].get(instrument, "0") for key, instrument in routes.items()}
            source["targets"] = {instrument: values[key] for key, instrument in routes.items()}
            source["rows"] = [dict(strategy_id=sid, target_key=key, instrument_id=i, quantity=values[key])
                              for key, i in routes.items()]
            payload["portfolio"]["contributions"][sid] = [
                {"key": {"client_id": account.service.client_id, "instrument_id": i}, "quantity": values[key]}
                for key, i in routes.items()]
        for name in ("strategy_positions", "strategy_revisions"):
            payload["positions"][name] = [row for row in payload["positions"][name]
                if row["strategy_id"] != sid or row["target_key"] not in removed]
        payload["positions"]["attribution"]["owned"] = [row for row in payload["positions"]["attribution"]["owned"]
            if row["strategy_id"] != sid or row["target_key"] not in removed or Decimal(row["quantity"])]
    shared["shared"]["routes"] = current_routes
    if account.position_scope is not None:
        shared["shared"]["managed_instruments"] = sorted(account.position_scope.instruments)
        if "external_positions" in payload["component_states"]:
            payload["component_states"]["external_positions"]["managed_instruments"] = sorted(account.position_scope.instruments)
    known = sorted({i for declaration in current_routes.values() for i in declaration["routes"].values()})
    shared["known_instruments"] = known
    payload["portfolio"]["known_keys"] = [{"client_id": account.service.client_id, "instrument_id": i} for i in known]
    limits = account.service.instrument_limits
    account.rollover_audit = observe_rollover(payload, driver=account.driver, transport=account.transport,
        day=account.expected_day, multipliers={i: limit.contract_multiplier for i, limit in limits.items()},
        scope=account.position_scope, preserve_external=account.preserve_external,
        generic_close_venues=account.generic_close_venues)
    if account.preserve_external:
        from bomber.framework.trader.runtime.live.channels.ctp_owned import restore_external_baseline
        payload = restore_external_baseline(account, payload, refresh=True)
        payload["component_states"]["session_identity"]["identity"] = new
        return payload
    external = payload["component_states"].get("external_positions")
    if external is not None:
        from bomber.framework.trader.runtime.ctp_account import position_buckets
        # 范围外仓位可由其他策略跨日交易；只刷新观测基线，不创建策略归属。
        observed = account.position_scope.external(account.rollover_audit["gross"])
        expected = {key: tuple(Decimal(q) for q in values) for key, values in external["gross"].items()}
        account.driver.start(lambda report: None)
        try:
            from bomber.framework.trader.runtime.live.channels.ctp import occupied_positions, assert_account_ready
            if account.driver.trading_day != account.expected_day:
                raise RuntimeError("跨日外部基线查询TD日不一致")
            assert_account_ready(account.driver)
            rows = [row for row in account.transport.query_position_details()
                    if not account.position_scope.contains(f"{row['InstrumentID']}.{row['ExchangeID']}")]
            buckets = position_buckets(rows, observed)
            if {key: (r.quantities[0]+r.quantities[1], r.quantities[2]+r.quantities[3]) for key, r in buckets.items()} != observed:
                raise RuntimeError("跨日外部明细与总仓不一致")
            if occupied_positions(account.transport) != account.rollover_audit["gross"]:
                raise RuntimeError("跨日外部基线查询期间柜台持仓发生变化")
            assert_account_ready(account.driver)
            # 同合约外部基线不在本次刷新范围内，仍须通过原管理范围核验。
            retained = {key: values for key, values in external["gross"].items()
                        if account.position_scope.contains(key)}
            retained_buckets = {key: values for key, values in external["buckets"].items()
                                if account.position_scope.contains(key)}
            external["gross"] = {**retained, **{key: [str(q) for q in values] for key, values in observed.items()}}
            external["buckets"] = {**retained_buckets,
                **{key: [str(q) for q in r.quantities] for key, r in buckets.items()}}
            changes = {key: {"before": [str(q) for q in expected.get(key, (0, 0))],
                             "after": [str(q) for q in observed.get(key, (0, 0))]}
                       for key in sorted(expected.keys() | observed.keys())
                       if not account.position_scope.contains(key)
                       and expected.get(key, (0, 0)) != observed.get(key, (0, 0))}
            account.rollover_audit["external_baseline_refresh"] = {
                "scope": "outside_managed_instruments", "changes": changes,
                "gross": {key: [str(q) for q in values] for key, values in observed.items()}}
            if changes:
                logging.getLogger("SharedAccountRollover").info(
                    "跨日范围外仓位基线已核验，待恢复最终对账提交: changes=%s", changes)
        finally:
            account.driver.stop()
    payload["component_states"]["session_identity"]["identity"] = new
    return payload


def import_option_payload(account, payload):
    """原08账户文件原地续接至共享服务；保留历史，柜台核验后才提交备份。"""
    from importlib import import_module
    sessions = getattr(account, "option_sessions", {})
    old = payload["component_states"]["session_identity"]["identity"]
    sid = old["strategy_id"]
    if sid not in sessions or account.service.client_id != sid:
        raise ValueError("导入原08账本须保留原策略ID及CTP客户端ID")
    session = sessions[sid]
    from scripts.integration.options.simnow_migrate import validate_original
    validate_original(account.manager.repository.load())
    signal = deepcopy(payload["component_states"]["vega_signal"])
    if old["td_day"] == account.expected_day:
        if old != session.session_identity:
            raise ValueError("同日导入08须保留原会话身份")
    else:
        payload = import_module("demos.08_option_vega.rollover").payload_for_day(session, payload,
            driver=account.driver, transport=account.transport, day=account.expected_day)
        account.rollover_audit = session.rollover_audit
        signal = deepcopy(payload["component_states"]["vega_signal"])
    session.restore_signal(signal)
    current = account.manager.state_components["session_identity"].identity
    source, = payload["targets"]
    shared = account.service.snapshot_state()
    shared.update(portfolio_revision=payload["portfolio"]["revision"],
        strategy_revisions={sid: source["revision"]}, known_instruments=sorted(source["targets"]))
    shared["sources"] = {sid: {"revision": source["revision"], "ts_event": source["ts_event"],
        "targets": dict(source["targets"]), "rows": [dict(strategy_id=sid, target_key=key,
            instrument_id=key, quantity=value, target_revision=source["revision"])
            for key, value in source["targets"].items()], "deadline_ns": None,
        "execution_policy": "DIRECT", "metadata": {}}}
    shared["shared"]["bootstrap_signals"] = {sid: signal}
    components = {"session_identity": {"version": 1, "identity": current}, "shared_account": shared}
    if account.position_scope is not None:
        from bomber.framework.trader.runtime.ctp_account import position_buckets
        from bomber.framework.trader.runtime.live.channels.ctp import occupied_positions, assert_account_ready
        account.driver.start(lambda report: None)
        try:
            assert_account_ready(account.driver)
            gross = account.position_scope.external(occupied_positions(account.transport))
            rows = [r for r in account.transport.query_position_details()
                    if not account.position_scope.contains(f"{r['InstrumentID']}.{r['ExchangeID']}")]
            buckets = position_buckets(rows, gross)
            if {key: (r.quantities[0]+r.quantities[1], r.quantities[2]+r.quantities[3]) for key, r in buckets.items()} != gross:
                raise RuntimeError("导入08的外部明细与总仓不一致")
            components["external_positions"] = {"version": 1,
                "managed_instruments": sorted(account.position_scope.instruments),
                "gross": {key: [str(q) for q in values] for key, values in gross.items()},
                "buckets": {key: [str(q) for q in r.quantities] for key, r in buckets.items()}}
        finally:
            account.driver.stop()
    payload["component_states"] = components
    return payload
