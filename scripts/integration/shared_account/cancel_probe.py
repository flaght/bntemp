"""观察自然产生的共享母单，由非提交者停止自身触发既有撤单流程。"""

from copy import deepcopy
from decimal import Decimal
import time


class SharedMotherCancelProbe:
    def __init__(self, owner, *, clock_ns=time.time_ns):
        self.owner, self.clock_ns = owner, clock_ns
        self.mother = None
        self.requested_at_ns = self.stopped_at_ns = None
        self.peers_active_after_stop = []

    def poll(self, host):
        if self.mother is not None:
            return
        owner = host.strategies[self.owner]
        if not owner.active:
            return
        reports = tuple(host.account.execution.backend.reports)
        accepted = {report.client_order_id for report in reports
                    if report.report_type.value == "ACCEPTED"}
        for token, order in host.service.position_manager.attribution.state()["orders"].items():
            owners = {leg["strategy_id"] for leg in order["legs"]}
            if (self.owner not in owners or len(owners) < 2
                    or order["submitting_strategy"] == self.owner or order["released"]
                    or order["client_order_id"] not in accepted
                    or not any(Decimal(leg["filled"]) < Decimal(leg["quantity"])
                               for leg in order["legs"])
                    or not all(sid in host.strategies and host.strategies[sid].active for sid in owners)):
                continue
            self.mother = {"allocation_token": token, **deepcopy(order)}
            self.requested_at_ns = self.clock_ns()
            print(f"共享撤单验收捕获母单: {order['client_order_id']} 提交者={order['submitting_strategy']} "
                  f"停止所有者={self.owner} 冻结所有者={sorted(owners)}", flush=True)
            host.stop_strategy(self.owner, reason="acceptance_shared_cancel")
            self.stopped_at_ns = host.strategies[self.owner].stopped_at_ns
            self.peers_active_after_stop = sorted(sid for sid in owners - {self.owner}
                                                 if host.strategies[sid].active)
            return

    def result(self, host, *, runtime_status):
        result = {"status": "not_exercised", "stop_owner": self.owner,
            "selection": "accepted_working_shared_mother_with_non_submitter_owner",
            "extra_targets_submitted": 0, "limit_prices_overridden": False,
            "mother": self.mother, "requested_at_ns": self.requested_at_ns,
            "stopped_at_ns": self.stopped_at_ns,
            "peers_active_after_stop": self.peers_active_after_stop,
            "partial_fill_observed": False, "rejection_observed": False,
            "peer_target_continuations_after_stop": 0, "peer_fills_after_stop": 0}
        if self.mother is None or host is None:
            result["reason"] = "未捕获符合条件的在途共享母单；不制造目标或扩大订单"
            return result
        cid = self.mother["client_order_id"]
        reports = tuple(host.account.execution.backend.reports)
        mother_reports = [row for row in reports if row.client_order_id == cid]
        types = [row.report_type.value for row in mother_reports]
        result.update(mother_report_types=types,
            partial_fill_observed="PARTIALLY_FILLED" in types,
            rejection_observed="REJECTED" in types,
            canceled_report_ids=[row.report_id for row in mother_reports
                if row.report_type.value == "CANCELED" and row.ts_event >= self.requested_at_ns])
        if self.stopped_at_ns is not None:
            peers = set(self.peers_active_after_stop)
            result["peer_target_continuations_after_stop"] = sum(
                row.action == "TARGET_SUBMITTED" and row.detail in peers and row.ts_ns > self.stopped_at_ns
                for row in host.service.client.audit_records)
            result["peer_fills_after_stop"] = sum(
                row.report_type.value in {"FILLED", "PARTIALLY_FILLED"} and row.ts_event > self.stopped_at_ns
                and any(leg["strategy_id"] in peers for leg in row.metadata.get("allocation_legs", ()))
                for row in reports)
        if self.stopped_at_ns is None or runtime_status == "failed":
            result.update(status="failed", reason="撤单/停机未完成或本轮运行失败，须核对完整报告")
        elif result["canceled_report_ids"] and self.peers_active_after_stop:
            result.update(status="passed", reason="真实回报记录母单撤单，非提交者停止后同单其他策略保持活动")
        else:
            result["reason"] = "捕获后母单可能已成交或未取得撤单回报，不登记撤单验收通过"
        return result
