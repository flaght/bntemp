# stgWrapper 接线组件：已完成部分与待确认项

日期：2026-10-29。实现：[stg_wrapper_adapter.py](stg_wrapper_adapter.py)。分析依据：[原生导出库分析](../../../doc/integration/LXGBN_EXPORTED_LIBRARY_REVIEW_2026-10-29.md)。此组件位于外部 scripts 目录，尚未绑定厂商真实函数；不必为本次修改重打 Bomber wheel。

组件不导入 stgWrapper、不调用 Start、不创建线程、不读取厂商配置、不连接数据库。现有普通 `import bomber.framework.lxgbn` 行为不变。它可在厂商确认库版本和签名前先完成替身回归。

## 1. 已有组件

| 组件 | 责任及边界 |
| --- | --- |
| `CommandLinks(binding)` | 当前进程的 SDK 整数 ID/request_id/原生单号关联；发送前登记；拒绝复用和覆盖。不维护仓位、成交账本或另一份订单状态机 |
| `NativeHostPort(links, send=..., cancel=..., query=...)` | 接收 Bomber 契约并调用显式注入函数；未全部接线时查询报错阻止 SDK 启动；不推断任何原生返回码 |
| `NativeEnums.from_module(rp)` | 读取实际枚举成员，拒绝未知值和错误尾空格导出；不猜原生整数 |
| `NativeCallbackCopies(...)` | 读取厂商已知的 Order/Trade 字段，配合宿主补充事实生成值快照，核对合约/策略/订单关联/量价/开平 |
| `CallbackFacts` | 宿主提供已验证绑定、真实交付序号、事件时间、普通单属性及成交投机属性等缺失事实；不预填能力或时间 |
| `NativeCallbacks(copies)` | 2/3/2 回调复制转发，故障返回 False，记录 first `last_error` 并通知 SDK 断开；不调用 poll |
| `VALUE_COPIES` | 交给 SDK 的复制函数，表示回调桥已生成独立值快照；SDK 仍会做自己的复制和校验 |
| `NativeBarCopies(...)` | 复制 Bar 的明确字段和真实周期/时间映射；历史 Bar 返回 None，不直接转为实盘事件；owner 线程用 to_event 构造标准 Bar |

此阶段支持新测试绑定的接线流程。`CommandLinks` 仅保存本进程实际登记的命令，不从 getOrder 猜历史归属，也不自动恢复旧 ID 表。SDK 本身的已终态检查点恢复能力继续保留；要使新原生包装在重启后也转发旧回报，仍需由同版原生关联/恢复方案补齐，不能通过重发旧命令来登记。

## 2. 交易函数注入：只规定 Bomber 侧形状

以下为注入函数的约定，不是声称原生 sendOrder 具有这些签名：

```python
def confirmed_send(command):
    # 接收完整 OrderCommand，在这里调用双方确认的实际原生函数。
    # 返回 SubmitResult('queued'/'rejected'/'unknown', reason)。
    raise NotImplementedError('待确认限价/开平/预分配ID/返回语义')

def confirmed_cancel(command):
    # 接收已登记的完整命令，按已确认的 ID 映射调用 cancelOrder。
    # 返回 None 不能被解释为已经撤单。
    raise NotImplementedError('待确认 cancelOrder 的参数及语义')

def confirmed_query():
    # 返回实际 BindingSnapshot，不用 CommandLinks 自行生成仓位和完整性。
    raise NotImplementedError('待确认策略范围查询与一致水位')
```

`NativeHostPort.send_order` 在调用 confirmed_send 前完成 ID 登记，因此发送函数内部同步触发回调也可关联。发送抛异常、unknown 或原始 int/None 等未规范化结果时不撤销关联，由 SDK 保留未决状态并闭闸。即使重复传入同一个命令，也不再次调用原生发送。

取消注入函数拿到的是 OrderCommand，能使用 client_order_id 或 request_id 对接已有原生映射；不能在 cancelOrder 的 int 含义未明确前直接传任意数字。

## 3. 字段与 CallbackFacts

NativeCallbackCopies 的配置示例：

```python
copies = adapter['NativeCallbackCopies'](
    links,
    adapter['NativeEnums'].from_module(rp),
    instrument_code=actual_native_contract,  # 例如实际 rb2701，而非主连猜测
    strategy_id=actual_native_strategy_id,  # 原生数值 ID，区别于 Binding.strategy_id 字符串
    quantity_field=confirmed_fixed_quantity_field,  # 只可选 volumeSubmitted/volumePlanned
    read_facts=read_native_facts,
)
```

`read_native_facts(kind, command, order, fill)` 由宿主实现，返回 CallbackFacts。kind 为 ack/fill/finished。其责任是从真实绑定上下文、已确认时间单位和原生交付流取得以下值：

| 字段 | 来源及要求 |
| --- | --- |
| `binding` | 真实账户、策略、合约、交易日/命名空间已核对后返回的 Binding；不能把所有传入对象都直接套配置 |
| `delivery_sequence` | 宿主有序交付的正整数；一次 Fill 的订单和成交快照共用此值；组件不生成/修补序号 |
| `order_ts_ns` | 此回调对应的真实订单事件纳秒时间，非回调接收时刻 |
| `attribute` | 原生实际 Normal=1，或经过确认的固定普通单路径关联；不能无条件写 1 |
| `fill_ts_ns` | Fill 必填，真实成交事件时间；缺失直接失败，不用 now() 补齐 |
| `fill_hedge` | Fill 必填，是实际原生枚举或从已确认可信订单关联取得的枚举值；由 NativeEnums 校验为 Speculation |
| `reason` | 已解码原生拒绝原因，非拒绝可为空 |
| `rejection_of` | Rejected 必须明确 order/cancel；unknown 不能通过 |

组件直接读取这些已提供的原生名称：`Order.Id/GetInst()/strategyId/direction/offset/status/hedgeFlag/volumeTraded`，固定量从配置指定的字段取得；`Trade.tradeId/orderId/strategyId/direction/offset/price/volume`。没有字段别名猜测，没有回退到错误大小写字段。

复制失败、未知 ID、方向/绑定不符、SDK 拒绝入队均返回 False。`bridge.last_error` 保留首个结构化错误：callback、error_type、error；宿主须将其连同原生事实写入自己的日志。不要只记录最后一个通用“故障已锁存”，也不要保存借用原生指针用于延迟复制。后续 poll 的错误仍由 owner 调度器捕获并记录，桥返回 True 不表示 OMS 已接受。

## 4. 装配代码

下例中 rp、binding、business_strategy、data_bindings、风险/状态文件和所有 confirmed_* / read_native_facts 均来自接入方；未确认函数没有默认实现。

```python
import runpy
from bomber.framework.lxgbn import create_session

adapter = runpy.run_path(r'D:\bomber\scripts\integration\lxgbn\stg_wrapper_adapter.py')
links = adapter['CommandLinks'](binding)
host = adapter['NativeHostPort'](
    links, send=confirmed_send, cancel=confirmed_cancel, query=confirmed_query)
copies = adapter['NativeCallbackCopies'](
    links, adapter['NativeEnums'].from_module(rp),
    instrument_code=actual_native_contract,
    strategy_id=actual_native_strategy_id,
    quantity_field=confirmed_fixed_quantity_field,
    read_facts=read_native_facts,
)
bridge = adapter['NativeCallbacks'](copies)
session = create_session(
    strategy=business_strategy, binding=binding, host=host,
    copies=adapter['VALUE_COPIES'], data_bindings=data_bindings,
    target_key='main', price_increment=actual_tick_size,
    risk_limits=risk_limits, state_file=state_file,
)
bridge.attach(session)
```

确认原生回调签名后，实际 `rp.IPyStrategy` 子类只委托这三个入口，实际初始化及其他 pure_virtual override 按同版头文件接入：

```python
def onOrderAck(self, clientOrderId, orderMsg):
    return self.bridge.onOrderAck(clientOrderId, orderMsg)

def onOrderFill(self, clientOrderId, orderMsg, execMsg):
    return self.bridge.onOrderFill(clientOrderId, orderMsg, execMsg)

def onOrderFinished(self, clientOrderId, orderMsg):
    return self.bridge.onOrderFinished(clientOrderId, orderMsg)
```

不能跳过 bridge 而把原生对象传给 session（此处 VALUE_COPIES 只对应已转换的快照）。start 前不要交付回调；start/authorize/poll/行情/停止仍在同一 owner 线程，原生包装层负责 GIL。组件的串行锁只保证本桥复制/入队有序，不证明原生查询的一致边界，不能反向拿这个锁并等待另一线程回调。

## 5. Bar 复制与同线程调度

```python
bar_copies = adapter['NativeBarCopies'](
    instrument_code=actual_native_contract,
    instrument_id=binding.instrument_id,
    bar_specs={rp.bar_time_unit_t.M1: '1-MINUTE'},
    timestamp_ns=confirmed_bar_timestamp_ns,
    price_precision=actual_price_precision,
    size_precision=actual_size_precision,
)

# 在原生 Bar 对象仍有效时复制；这一步不构造会话、不下单：
values = bar_copies.copy(raw_bar)

# values 为 None 表示历史 Bar；其他值交给宿主已有的有界 dispatcher。
# 在 SDK owner 线程执行下面两行；不从任意行情线程直接调用：
if values is not None:
    event = bar_copies.to_event(values, ts_init=actual_receive_timestamp_ns)
    session.on_market('lxg', event)
```

DataBinding 也须配置成 BAR、feed_id=lxg、bar_spec=1-MINUTE，业务策略使用 on_bar。Bar 时间字段由 confirmed_bar_timestamp_ns 从真实 `raw_bar.timeStamp` 等已确认源读取；组件不自动推断微秒/毫秒。历史预热另走已有流程，本组件不把历史 Bar 当作实时目标触发。

原生回调边界还需捕获 Bar 属性/转换/dispatcher 队列异常，记录并调用 session.on_disconnect；行情错误不能穿过 C++ 边界。没有行情时仍由 owner 定时器调 session.poll。实际 timer/dispatcher API 未确认，本组件没有偷偷创建 Python 调度线程。

## 6. 本地验证与未完成边界

```sh
python3.12 -m unittest tests.test_lxgbn_wrapper_adapter \
  tests.test_lxgbn_execution tests.test_execution_composition -q
```

新增用例使用与已提供的原生导出字段同形的替身和自定义非默认整数枚举；真实 Runner/OMS/策略归属/风控/JSON 持久化参与执行，行情标识/类型在开发机用受控替身。覆盖同步回调前关联、原生值复制生命周期、订单/成交通知、去重、两阶段停止、撤单拒绝、未知发送保留、重复 ID、身份/枚举/缺失字段拒绝、游标缺口、SDK 入队拒绝以及 Bar 时间/周期/历史过滤。

尚未完成：真实 stgWrapper 版本和回调 ABI 验证、原生限价/开平发送参数及返回码绑定、真实 CallbackFacts 和 BindingSnapshot 提供、Windows Bar 实体运行、timer/GIL 及原生恢复关联。模块可独立评审和替身测试通过，不表示能绕过这些条件启动真实交易。

本轮新增 22 项接线用例，与现有 LXGBN/公共执行组合共 88 项通过；[结构化验证记录](../../../doc/evidence/live/M02_LXGBN_WRAPPER_OFFLINE_20261029.json) 保存命令、结果及尚未验证范围。
