"""DolphinDB行情通道健康适配；不读取任何交易账户或CTP配置。"""
import time
from ..contracts import ChannelIdentity, ReadinessIssue, ReadinessResult
from ..health import timestamp_issue


class DolphinMarketChannel:
    def __init__(self, feed, *, max_receive_age_seconds=120, allow_historical=False, clock_ns=None,
                 event_offset_seconds=0):
        self.feed = feed
        self.identity = ChannelIdentity("market", "dolphindb", feed.source_id)
        self.max_age_ns = int(max_receive_age_seconds * 1e9)
        if self.max_age_ns <= 0:
            raise ValueError("DolphinDB接收时效须为正数")
        self.allow_historical = allow_historical
        self.clock_ns = clock_ns or time.time_ns
        self.event_offset_ns = int(event_offset_seconds * 1e9)

    def check(self):
        health = self.feed.health_snapshot
        issues = []
        failure = getattr(self.feed, "conversion_failure", None)
        if failure:
            issues.append(ReadinessIssue(self.identity, "CONVERSION_ERROR", str(failure)))
        if getattr(health.state, "value", health.state) != "ready":
            issues.append(ReadinessIssue(self.identity, "STREAM_UNHEALTHY", str(health.reason)))
        received = getattr(self.feed, "latest_received_ns", health.last_event_received_ns)
        event = getattr(self.feed, "latest_event_ns", health.last_event_ts)
        if event is not None:
            event += self.event_offset_ns
        for kind, timestamp, historical in (("RECEIVE", received, False),
                                            ("EVENT", event, self.allow_historical)):
            issue = timestamp_issue(self.identity, kind, timestamp, self.clock_ns(), self.max_age_ns,
                                    allow_historical=historical)
            if issue:
                issues.append(issue)
        return ReadinessResult((self.identity,), tuple(issues))
