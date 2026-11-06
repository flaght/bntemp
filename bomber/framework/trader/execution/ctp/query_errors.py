"""CTP只读查询的拒绝及调度中断；不适用于报单或撤单的自动重试。"""


class CtpQueryRejected(RuntimeError):
    def __init__(self, kind, method, request_id, code):
        self.kind, self.method = kind, method
        self.request_id, self.code = request_id, code
        self.retryable = method.startswith("reqQry") and code in {-2, -3}
        super().__init__(f"CTP {method}请求被拒绝: {code} (kind={kind} request_id={request_id})")


class CtpQueryInterrupted(TimeoutError):
    """调度要求停止等待；已受理的查询仍须等待原最终回报，不能覆盖。"""


class CtpStopReconciliationRequired(RuntimeError):
    """TD断线后无法确认撤单和仓位；保留检查点及锁，不标记正常停机。"""
