"""分钟行情和决策的只读摘要；不查询数据、不计算目标、不参与持久化身份。"""
from datetime import datetime
import json
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")


def timestamp(value):
    return None if value is None else datetime.fromtimestamp(value / 1e9, ZONE).isoformat()


def emit(logger, title, row, *, error=False):
    (logger.error if error else logger.info)("%s: %s", title,
        json.dumps(row, ensure_ascii=False, default=str, sort_keys=True))


def market_summary(runner, frame, deadline, required):
    rows = frame.observations
    strategy = runner.strategy
    details = {}
    for key in sorted(required):
        row = rows.get(key)
        last = runner.observations.get(key)
        details[key] = {"same_minute": row is not None,
            "price": str(row.price) if row else None,
            "source": runner.sources[key],
            "bar_time": timestamp(row.source_ns) if row else None,
            "received_at": timestamp(row.received_ns) if row else None,
            "receive_delay_seconds": round((row.received_ns-frame.valuation_ns)/1e9, 3) if row else None,
            "last_seen_minute": timestamp(last[0]) if last else None}
    options = set(strategy.contracts)
    synchronous = sum(key in rows and strategy.contracts[key].future in rows and strategy.index_id in rows
                      for key in options)
    missing = sorted(required-set(rows))
    return {"strategy_id": strategy.strategy_id, "minute": timestamp(frame.valuation_ns),
        "sealed_at": timestamp(frame.request_ns), "deadline": timestamp(deadline),
        "status": "COMPLETE" if not missing else "INCOMPLETE", "missing_required": missing,
        "required": details, "option_bars": len(options.intersection(rows)),
        "configured_options": len(options), "synchronous_option_groups": synchronous,
        "future_bars": len(set(strategy.futures).intersection(rows)),
        "index_present": strategy.index_id in rows,
        "late_bars_total": runner.frames.late_count,
        "ignored_stale_candidates_total": runner.ignored_stale_candidate_bars}


def decision_summary(strategy, frame, *, error=None):
    row = dict(getattr(strategy, "last_decision", {}))
    row.update(strategy_id=strategy.strategy_id, minute=timestamp(frame.valuation_ns),
               request_at=timestamp(frame.request_ns))
    if error is not None:
        row.update(status="ERROR", failure=str(error))
    row["option_goals"] = {key: str(value) for key, value in strategy.option_goals.items()
                           if value or row.get("positions", {}).get(key)}
    row["future_goals"] = {key: str(value) for key, value in strategy.future_goals.items()
                           if value or row.get("positions", {}).get(key)}
    keys = set(row.get("positions", {})) | set(row.get("working", {})) | set(row["option_goals"]) | set(row["future_goals"])
    keys.add(strategy.index_id)
    keys.update(strategy.contracts[key].future for key in tuple(keys) if key in strategy.contracts)
    row["price_cache"] = {key: {"price": strategy.latest[key][1],
        "minute": timestamp(strategy.latest[key][0]),
        "age_seconds": (frame.valuation_ns-strategy.latest[key][0])/1e9}
        for key in sorted(keys) if key in strategy.latest}
    # 提交目标不等于订单受理或成交；只能报告已有回报计数。
    row.update(target_submissions_total=strategy.submissions,
               fills_received_total=strategy.fills_received,
               order_updates_received_total=strategy.order_updates_received)
    return row
