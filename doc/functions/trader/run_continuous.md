# run_continuous

用途：公共 Live `--run-forever` 编排，交易窗口内运行，休市保留进程等待，下一窗口创建新会话。

- 导入：`from bomber.framework.trader.runtime.live import run_continuous, LiveWindowContext`
- [实现](../../../bomber/framework/trader/runtime/live/continuous.py) · [会话运行器](ManagedLiveRuntime.md)
- 仅服务 Live。时段和日期仍由 `framework.calendars` 提供；回测不使用此入口。
- 核对日期：2026-10-29，Asia/Shanghai。

## 参数

```python
run_continuous(factory, *, calendar, state_file=None, max_session_orders=None,
               expected_trading_day=None, expected_source_day=None,
               order_limit_policy="stop", owner_lock=None, stop_event=None,
               clock=None, wait=None, status=None)
```

| 参数 | 含义 |
| --- | --- |
| factory | `factory(window_context=...)`，返回一次 `ManagedLiveRuntime`；已有策略、行情和执行组件仍由应用装配 |
| calendar | 公共会话日历，决定窗口所属交易日及前一来源交易日 |
| state_file | 原交易状态文件。存在时标记恢复，实际恢复仍由原生命周期和状态管理器完成 |
| max_session_orders | 按当前交易日持久化订单扣减的总额度，重连／午休不刷新；None 不额外限制 |
| expected_trading_day / expected_source_day | 可选，只核对首次成功运行前的窗口；后续由日历确定 |
| order_limit_policy | `stop`：额度耗尽退出；`wait`：跳过当前窗口，继续等后续窗口／下一交易日 |
| owner_lock | 可选进程租约。默认按state-file路径持有常驻所有者锁 |
| stop_event | 可注入Event；省略时公共StopSignal接管SIGINT／SIGTERM，结束后恢复原处理器 |
| clock / wait / status | 可注入时钟、休市等待和状态输出，支持无网络、无真实等待测试 |

返回摘要包含 `status`、`windows`、`skipped_windows`、`stopped_by_operator`。已有低层 `ContinuousPositionRuntime` 接口保留，共享账户调用继续兼容。

## 窗口与停止

`LiveWindowContext` 提供 `calendar`、`window`、`trading_day`、`source_day`、`stop_event`、`resume` 和剩余 `max_session_orders`。已有 `build_runtime()` 将其投影为本次会话参数；公共层不读取命令行Namespace或业务字段。

`ManagedLiveRuntime` 每轮先检查收盘／主动停止，再调用通道健康轮询。窗口模式忽略seconds退出计时，单次模式仍按原seconds运行。正常暂停先关闭新目标和报单授权，然后走既有撤单、核验、保存及资源释放，保留已成交仓位。

行情组件可用 `session.flush_completed_bars = True` 声明暂停时轮询最后封闭分钟。此时已禁单，保留Feed原封闭延迟。EMA指标收盘更新、Hybrid原始模型分钟转换仍属于相应业务组件。

异常、停机失败和非成功结果直接退出；同窗口提前正常结束不重复登录。日历覆盖不足和时钟倒退明确失败。

## 进程内业务状态

会话可选注册 `session.window_components = {"model": existing_model_state, ...}`，组件沿用 `snapshot_state()`／`restore_state(saved)` 契约。公共运行器在会话装配后、启动前恢复上一窗口快照；停机成功后获取下一份快照。组件集合变化会失败，不静默丢弃旧状态。

这些快照只在当前进程内续接，交易检查点继续由RuntimeStateManager处理。模型分钟不会因此写入账户状态文件；进程重启仍依赖已有历史源／分钟归档。组件注册属于现有装配，不要求策略另外实现常驻循环或continuous.py。

## 已迁移入口与范围

- [01 EMA](../../../demos/01_main_ema/run_live.py) 直接调用公共入口，保留原CU／realtime SimNow限制和额度耗尽等待政策；指标分钟处理在 [history.py](../../../demos/01_main_ema/history.py)，精确断点恢复在 [recovery.py](../../../demos/01_main_ema/recovery.py)。
- [10 Hybrid](../../../demos/10_hybrid_transformer/run_live.py) 的Recording／SimNow使用同一入口，额度耗尽退出；模型窗口在 [history.py](../../../demos/10_hybrid_transformer/history.py)，批次及跨日核验在 [checkpoint.py](../../../demos/10_hybrid_transformer/checkpoint.py)。
- **08 Vega 原本已支持 `--run-forever`**：[单策略共享执行快捷入口](../../../scripts/integration/options/shared.py) 和 [共享账户多策略／多进程入口](../../../scripts/integration/shared_account/continuous.py) 已调用同一个公共 `ContinuousPositionRuntime`，没有策略目录专属 continuous.py。`demos/08_option_vega/run_live.py` 本身仍是单次会话入口。此前清单漏列了 scripts 下的常驻路径，不能据此认定 08 没有此能力。
- 08 目前保留 `OptionWindowRuntime`／`SharedWindowRuntime` 等应用装配，尚未迁移到新增的高层 `run_continuous()`；其共享账户宿主、首次原仓导入和 server／worker 停机顺序须保留。这是高层接入待统一，不是缺少公共日历调度或常驻功能。
- 01／10 原有continuous.py已删除。03／04入口和恢复范围本轮未扩展；模块式预检不在本次实现范围。

## 验证与同步

聚合离线检查复用项目已有SDK实体夹具；真实公共调度、ManagedLiveRuntime、状态组件和历史流程参与执行，EMA接线检查不构造原生指标：

```bash
PYTHONPATH=.:.validation/packages python -B -m tests.run_live_continuous_offline
```

完整EMA数值／原生装配回归须在已安装Bomber引擎的环境执行：

```bash
PYTHONPATH=. python -B -m tests.run_main_ema_acceptance \
  --only continuous --only entry --only ema-recovery
```

真实DolphinDB／SimNow跨日与收盘联调仍需目标运行机验证。同步公共Live文件及相应demo入口、history、checkpoint／recovery，并移除远程旧demo的continuous.py。

[返回函数索引](../README.md)
