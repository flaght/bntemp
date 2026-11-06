"""08交易日报告：复用公共追加与会话摘要，报告快照不作为恢复检查点。"""
import json

from bomber.framework.trader.runtime.reports import TradingDayLiveReport


class OptionLiveReport(TradingDayLiveReport):
    def record(self, session, events):
        self.record_events(events)
        if session is not None:
            self.record_jsonl("signals.jsonl", session.strategy.signals)

    def write(self, *, status, fields, session, events):
        self.record(session, events)
        if session is not None:
            strategy = session.strategy
            positions = session.runner.position_manager
            snapshot = {"trading_day": self.trading_day, "run_id": self.run_id,
                "scope": "report_snapshot_not_recovery_checkpoint", "strategy_id": strategy.strategy_id,
                "positions": {key: str(positions.position(strategy.strategy_id, str(instrument)))
                    for key, instrument in session.references.instrument_ids.items()},
                "option_goals": strategy.option_goals, "future_goals": strategy.future_goals,
                "last_decision": strategy.last_decision}
            self._write_json("strategy_state.json", snapshot)
            # Runner保留最近256帧；明确为诊断尾部，不冒充完整交易日历史。
            self._write_json("feature_frames.json", {"scope": "recent_frames", "frames": session.runner.frame_audit})
        self._write_json("strategy_parameters.json", self.metadata.get("strategy_parameters", {}))
        return super().write(status=status, fields=fields, session=session, events=events)

    def _write_json(self, name, value):
        (self.path / name).write_text(json.dumps(value, ensure_ascii=False,
            indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")
