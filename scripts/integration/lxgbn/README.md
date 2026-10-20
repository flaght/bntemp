# Windows LXGBN 调用项目 SDK

LXGBN（DXBM-LXG）是 Windows 交易宿主，本项目是供其内嵌 Python 调用的 SDK。入口为 `bomber.framework.lxgbn`。本目录没有独立交易进程、HTTP 服务或厂商 SDK 加载器。

CTP 已有实际链路继续复用；BN 本期延后。首期仅测试环境，一个策略、一份绑定、一个合约，普通限价 Normal 与投机。实际 Windows/柜台联调尚未完成。

## Python 3.12 已确认后的第一步

先运行 [environment_probe.py](environment_probe.py)，再决定构建范围。探针本身只用标准库，记录系统、完整 Python 版本、解释器位数、二进制后缀和实际模块路径，分开检查契约、Bomber 内核、market/basic 扩展和完整会话工厂。只做导入，不构造会话、不调用报撤单端口；通过也不代表原生 ABI/数值、回调线程或真实交易已验收。

下面假设本期源码位于 `D:\bomber-pro`，报告保存到 `D:\lxgbn-test`。先在匹配宿主版本和位数的 Windows Python 3.12 环境运行：

```powershell
cd D:\bomber-pro
python scripts\integration\lxgbn\environment_probe.py --project-root D:\bomber-pro --output D:\lxgbn-test\standalone-environment.json
```

这里的 `python` 必须是选定的 3.12 解释器；不要根据系统默认命令推断它与宿主相同。没有独立 Python 时，可直接做下面的内嵌检查。

**还需在 LXGBN 实际内嵌 Python 内运行一次**，它的 DLL 搜索路径、已加载模块和 Python 搜索路径可能与独立 Python 不同。通过宿主现有脚本入口执行下面的代码，不另起解释器：

```python
import runpy

probe = runpy.run_path(r'D:\bomber-pro\scripts\integration\lxgbn\environment_probe.py')
report = probe['collect_report'](project_root=r'D:\bomber-pro')
probe['write_report'](report, r'D:\lxgbn-test\embedded-environment.json')
```

探针会临时加入显式源码目录，导入后恢复 `sys.path`；不会清除宿主已加载的模块。如果 framework 实际来自另一个目录，报告会阻止通过，应先统一部署路径并正常重启测试宿主，不在运行中强行替换原生模块。不要从 Linux/macOS 复制扩展二进制。

| 报告结果 | 下一步 |
| --- | --- |
| `sdk_contract` 失败 | 检查本期源码是否部署完整、实际加载路径是否正确 |
| `bomber_engine` 失败 | 先提供匹配 Windows/CPython 3.12/位数的 Bomber 内核；已有兼容产物复用，没有才构建内核 |
| 内核通过、`market_basic` 失败 | 按具体错误核对 `.pyd` 和依赖；没有兼容产物才构建两个扩展 |
| 前面通过、`session_factory` 失败 | 根据报告中的具体依赖/导入错误处理，不能把契约可导入当成 SDK 全部可用 |
| 内嵌报告 `windows_imports_ready=true` | 做原生行情对象量价检查，再接 HostPort/字段复制及只读绑定；尚不授权实单 |

需要构建 market/basic 时，在匹配宿主的 Python 3.12 构建环境、项目根目录执行：

```powershell
python bomber\framework\market\basic\setup.py build_ext --inplace
```

前提是已经提供匹配的 Bomber 内核及其 `.pxd`、Cython/NumPy/setuptools 和可用的 Windows C 编译工具。若内核尚缺失，不先执行这条命令。详见 [market/basic 构建说明](../../../bomber/framework/market/basic/README.md)。

本期不要求因此重编译整个 LXGBN；只有需要修改其原生接口/回调转发或绑定时才涉及宿主构建。当前仍需由宿主接入 `send_order/cancel_order/query_binding` 和原生字段复制函数，并核对三个回调参数、线程/GIL、通用 CLOSE 及快照水位。Python 3.12 版本一致本身不能代替这些联调。

## 宿主调用顺序

[embedded_example.py](embedded_example.py) 展示装配函数。宿主传入现有 `StrategyTemplate` 策略、绑定、`HostPort`、回调字段复制函数、行情绑定、跳价、风控和状态文件。策略算法无需识别厂商。

```python
from bomber.framework.lxgbn import create_session

session = create_session(
    strategy=strategy, binding=binding, host=host, copies=copies,
    data_bindings=data_bindings, target_key='main', price_increment='1',
    risk_limits=risk_limits, state_file=state_file,
)
# 在宿主原生回调对象仍有效时安装下列三个转发，然后只读启动。
session.start(resume=False)  # 已有状态文件必须显式 resume=True。
session.authorize()         # 核对通过后显式开放测试报单。

# 宿主已有事件线程：标准 Bomber 行情对象由宿主转换。
session.on_market('lxg', standard_tick_or_bar)
session.poll()              # 也需在没有行情时由宿主持续调度。

# 对应 C++ override 的完整参数，不要漏掉 clientOrderId/orderMsg。
session.onOrderAck(clientOrderId, orderMsg)
session.onOrderFill(clientOrderId, orderMsg, execMsg)
session.onOrderFinished(clientOrderId, orderMsg)

session.begin_stop()        # 闭闸、向宿主请求撤本策略已知活动单。
# 宿主继续转发回报并 poll；活动单未终结时 finish_stop 会拒绝结束。
session.finish_stop()
# 此后宿主解除回调并确认原生回调线程退出，才能释放 Python 对象/解释器。
```

上述片段表示生命周期各阶段，并非顺序执行一次即可完成的脚本。构造和只读启动不发送交易请求；策略 `on_start` 应只做初始化，授权后才能输出交易目标。反手先平仓，待后续策略目标更新才推进反向开仓，不另加自动重报机制。要恢复指标/信号窗口时，向 `state_components` 传入策略既有的 `snapshot_state/restore_state` 组件；框架只自动恢复目标版本及执行状态。

## 宿主实现的三个端口

- `send_order(OrderCommand) -> SubmitResult`：使用请求中的整数 `client_order_id`，原样转发到三个回调。`queued` 只代表已接收；`rejected` 必须表示确定未发送；超时、未知返回或异常都保留未决订单、禁止重发。普通限价请求包含 `attribute=1`、`hedge_flag='Speculation'`，没有额外 TIF。宿主负责最终交易校验与 OPEN/CLOSE 底层转换，不能把缺失能力静默降级。
- `cancel_order(client_order_id) -> None`：按已登记关联撤单；返回不表示已撤。撤单拒绝回报必须标记 `rejection_of='cancel'`，保留原工作单。
- `query_binding() -> BindingSnapshot`：提供当前策略/绑定的归属、活动单、初始化/完整性、真实执行可用状态及能力声明。它不是账户资金或全量仓位查询。默认未声明能力，尤其 `generic_close`、`acked_is_accepted`，都会阻止授权。

`Binding` 含 account_id、binding_id、strategy_id、trading_day、source_session、instrument_id、environment。source_session 是宿主实际原生 ID 命名空间，不是框架每次启动随机生成的字符串。此版本只接受 `environment='test'`。

`BindingSnapshot.boundary` 和快照中 `delivery_sequence` 共同定义宿主交付流。宿主须在交付时分配连续序号，且查询水位覆盖的事件都已提交给 SDK；同绑定恢复需延续水位或重放原流。它们是新定义的宿主接入契约，**不假定 OrderMsg_t 自带序号，也不能用本地计数假装原生业务顺序/完整快照**。无法建立边界时保持只读。框架不会据“活动单列表为空”推断历史订单已撤。

## 原生对象复制与线程

`CallbackCopies(order=copy_order, fill=copy_fill)` 由宿主接入层提供。复制函数在回调有效期内返回只含值的 `OrderSnapshot` / `FillSnapshot`，并完成：

- 校验原始 AccountId/StrategyId/InstrumentID 等与实际绑定的对应关系。不可直接把配置身份填入所有回报而省略核对。
- 保留稳定原生订单 ID、成交 ID；用明确的枚举映射输出 BUY/SELL、OPEN/CLOSE/CLOSE_TODAY/CLOSE_YESTERDAY 等。除已知 Normal=1 外，不猜测原生枚举整数值。
- 明确固定委托量、单笔成交量和累计量的不同含义，验证单位、字符编码和纳秒时间转换；Finished 不合成成交。
- 区分报单拒绝和撤单拒绝；无法确定时输出 unknown，框架会闭闸。
- 原始订单和成交必须指向同一订单。算法父子单、Market/FAK/FOK 和其他未声明能力不接入本期普通单路径。

三个回调保持 2/3/2 参数，只复制并放入有界队列。返回 False 表示转换/身份/容量故障，宿主必须记录并保留原生事实用于恢复。回调可由其他原生线程调用；宿主负责取得 GIL 和保证复制期间对象一致。生命周期、行情、`poll()` 由同一宿主线程串行调用，发送期间同步回调只能入队，不能重入 `poll()` 执行业务回调。

`on_disconnect(reason)` 在宿主失联/交易不可用时调用，立即锁存故障。SDK 不创建事件线程，也不登录、关闭宿主交易系统。停止期间继续处理实际撤单/成交，全部核对并保存后宿主才可释放绑定。

## 持久化与首期恢复限制

复用现有 JSON 检查点和公共 OMS/归属。发送前写入整数 ID/字符串 ID、PENDING、冻结分配；处理回报后保存。发送结果不确定时保留原关联和在途，不重复报单。重复成交按稳定 ID 去重；终态先到则等待真实成交补齐，终态后新成交闭闸。

同一检查点只允许同一绑定/交易日/原生会话，已知单全部终态、快照水位和归属一致时自动恢复。活动单、丢失成交、未知事件、换日/换会话需宿主提供只读查询/重放恢复后再评审；本版不自动接管。宿主负责一个绑定只有一份活动会话，并为每份绑定使用独立状态文件；本版不新增跨进程锁或通用分布式 fencing。

## Windows 验证边界

契约模块不依赖外部 LXGBN 包，完整策略会话继续依赖项目现有 Bomber 原生引擎。Windows 现场必须已有与内嵌 CPython、CPU 架构、C++/Boost.Python 和运行库兼容的引擎及宿主绑定。标准库契约可导入不能证明原生引擎已完成 Windows 发布；本批没有移植或编译该引擎。

离线测试使用真实 Runner、风控、订单机、归属和 JSON 保存，宿主端口和原生行情类型为受控替身：

```sh
PYTHONPYCACHEPREFIX=/tmp/m02-pycache python3.12 -m unittest \
  tests.test_lxgbn_execution tests.test_shared_account tests.test_execution_composition \
  tests.test_shared_process_cli tests.test_live_common tests.test_live_channels -q
```

实际 Windows 加载、原生对象字段复制与线程规则、快照水位、真实受理/撤单/成交和稳定 ID 仍需现场验证。同一份实际证据关联 M02/D5/GAP10 的 LXGBN 子项，不据离线测试关闭整体审计项。详细依据见[设计](../../../doc/architecture/M02_EXECUTION_CLIENT_DESIGN_2026-10-19.md)与[对接说明](../../../doc/integration/DXBM_LXG_INTEGRATION_SPEC.md)。
