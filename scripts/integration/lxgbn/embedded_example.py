"""LXGBN 内嵌 Python 中的装配示例；参数由交易宿主提供，无独立 main。"""
from bomber.framework.lxgbn import CallbackCopies, create_session


def create_in_terminal(*, strategy, binding, host, copy_order, copy_fill,
                       data_bindings, risk_limits, state_file, price_increment,
                       state_components=None):
    """返回未启动会话，供宿主先安装原生回调，再调用 start/authorize。"""
    return create_session(strategy=strategy, binding=binding, host=host,
        copies=CallbackCopies(order=copy_order, fill=copy_fill),
        data_bindings=data_bindings, target_key='main', price_increment=price_increment,
        risk_limits=risk_limits, state_file=state_file, state_components=state_components)


# C++/Python 策略包装层转发示意（self.session 由上面的工厂构造）：
# def onOrderAck(self, clientOrderId, orderMsg):
#     return self.session.onOrderAck(clientOrderId, orderMsg)
# def onOrderFill(self, clientOrderId, orderMsg, execMsg):
#     return self.session.onOrderFill(clientOrderId, orderMsg, execMsg)
# def onOrderFinished(self, clientOrderId, orderMsg):
#     return self.session.onOrderFinished(clientOrderId, orderMsg)
#
# 回调返回 False 时，宿主记录故障及原生事实，停止新增交易并进入只读恢复。
# 生命周期与事件调度/退出顺序见同目录 README.md。
