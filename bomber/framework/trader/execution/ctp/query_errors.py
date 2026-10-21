"""查询发送未受理的错误；不适用于报单或撤单的自动重试。"""


class CtpQueryRejected(RuntimeError):
    def __init__(self, kind, method, request_id, code):
        self.kind, self.method = kind, method
        self.request_id, self.code = request_id, code
        self.retryable = method.startswith("reqQry") and code in {-2, -3}
        super().__init__(f"CTP {method}请求被拒绝: {code} (kind={kind} request_id={request_id})")
