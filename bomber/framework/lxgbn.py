"""供 Windows LXGBN 内嵌 Python 调用的 SDK 契约；导入本模块不加载交易引擎。"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Protocol


@dataclass(frozen=True)
class Binding:
    account_id: str
    binding_id: str
    strategy_id: str
    trading_day: str
    source_session: str
    instrument_id: str
    environment: str = 'test'

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip() for v in vars(self).values()):
            raise ValueError('绑定身份必须完整')
        if self.environment != 'test':
            raise ValueError('首期仅开放 LXGBN 测试绑定')


@dataclass(frozen=True)
class BindingSnapshot:
    binding: Binding
    revision: str
    boundary: int  # 宿主交付流的水位；不是凭接收时间推断的原生业务序号。
    owned_quantity: Decimal
    active_order_ids: tuple[int, ...] = ()
    initialized: bool = False
    complete: bool = False
    ready: bool = False
    normal_limit: bool = False
    generic_close: bool = False
    acked_is_accepted: bool = False


@dataclass(frozen=True)
class OrderCommand:
    client_order_id: int  # 发送前分配，三个原生回调必须原样返回。
    request_id: str
    binding: Binding
    side: str
    quantity: Decimal
    price: Decimal
    position_effect: str
    attribute: int = 1  # 原生 Normal；不另传 TIF。
    hedge_flag: str = 'Speculation'


@dataclass(frozen=True)
class SubmitResult:
    outcome: str  # queued / rejected（确定未发送）/ unknown
    reason: str = ''


@dataclass(frozen=True)
class OrderSnapshot:
    """宿主在原生对象有效期内复制/规范化；不猜测原生枚举数值、单位或编码。"""
    binding: Binding
    delivery_sequence: int
    native_order_id: str
    side: str
    quantity: Decimal  # 已确认含义的固定实际委托量。
    status: str  # Blank/Submited/Acked/FullFilled/Cancelled/Rejected
    cumulative_filled: Decimal
    ts_event: int  # 明确转换后的纳秒事件时间。
    attribute: int
    hedge_flag: str
    reason: str = ''
    rejection_of: str = 'unknown'  # cancel 的拒绝不能终结原订单。


@dataclass(frozen=True)
class FillSnapshot:
    binding: Binding
    delivery_sequence: int
    native_order_id: str
    trade_id: str
    side: str
    quantity: Decimal
    price: Decimal
    position_effect: str
    ts_event: int
    hedge_flag: str


class HostPort(Protocol):
    """由 LXGBN 宿主实现并传入；项目不导入或持有另一份 LXGBN SDK。"""
    def send_order(self, command: OrderCommand) -> SubmitResult: ...
    def cancel_order(self, client_order_id: int) -> None: ...
    def query_binding(self) -> BindingSnapshot: ...


@dataclass(frozen=True)
class CallbackCopies:
    order: Callable[[object], OrderSnapshot]
    fill: Callable[[object], FillSnapshot]


def create_session(**kwargs):
    """装配现有策略/Runner/执行核心；具体参数见运行时工厂及集成示例。"""
    from bomber.framework.trader.runtime.lxgbn import create_session as create
    return create(**kwargs)
