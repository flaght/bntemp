# RuntimeStateManager：检查点接入与运行一致性

实现：[persistence.py](../../../bomber/framework/trader/persistence.py)。原 JSON schema、CAS代次、校验和、原子替换、组件恢复回滚、CTP写前／回报后保存和跨日备份机制继续复用，没有新增检查点格式或第二套持久化引擎。

## 组件接入

`state_components={name: component}` 仍是业务状态扩展点。构造时统一要求名称为非空字符串，组件具有可调用的 `snapshot_state()` 和 `restore_state(state)`；原可选 `validate_targets(target_store)` 接口保留。

业务组件继续保存自身需要的状态：01 的 EMA数值与断点、08 的选约／对冲去重、10 的批次与到期；03／04 的指标窗口仍按原方式预热。01／08／10 的数据不能互换，组件集合及会话身份检查不会因公共化而放宽。

## 三项统一核验

| 方法 | 用途 |
| --- | --- |
| `validate_binding(target_store, portfolio_coordinator, position_manager, *, client_id=None, order_machine=None)` | 核对当前 Runner 与Manager共享同一目标、组合、仓位及执行订单机；ManagedLiveRuntime在最终会话启动前调用 |
| `validate_components()` | 在稳定启动提交／停机保存阶段调用业务组件的 validate_targets；不对报单中间态额外施加稳定态目标约束 |
| `describe_checkpoint()` | 提供文件路径、generation、restore_pending、组件名、订单客户端及CTP账本账户名，供报告诊断；不输出业务状态和凭据 |

公共生命周期的 `commit_session_checkpoint()` 在 Runner提交锁内核对当前单策略绑定的目标键、组合贡献和目标／组合版本，再执行组件校验及保存／提交。CTP写前及回报后的自动保存继续沿用原客户端锁和Manager锁顺序，不改变委托中间态的保存语义。

## 提交与失败保护

新建会话在就绪、历史准备和稳定目标核验通过后调用save。恢复会话仍由 `restore()` 做完整组件／订单／归属恢复，准备完成后调用 `commit_restore()`；存在 payload_transform 的恢复在提交前保持写入禁止状态，原代备份仍由原实现生成。

`resume_ready` 只在提交成功后设置。提交前的历史、参考、信号或稳定目标核验失败时，不授权、不放行；恢复保留原文件，新建不保存半初始化状态。提交成功后的授权／运行异常按原停机规则处理，已提交的有效状态会保留。

08共享宿主使用账户 `start_preparation()` 延迟暂存提交，直到全部策略信号恢复与准备成功后由 `authorize()` 提交。原账户JSON与逐策略信号JSON保持各自代次；信号版本不得领先账户消费版本等原一致性规则继续执行，不声称跨两个文件具备一个原子事务。

## 报告

单会话 `ManagedLiveRuntime` 和共享宿主报告统一提供 `checkpoint` 元数据。共享宿主还为逐策略状态列出 `checkpoint_generation` 和 `saved_revision`，便于核对账户与信号消费关系。准备／运行原始错误和 `cleanup_errors` 同时保留。

## 验证入口

```bash
PYTHONPATH=.:.validation/packages python -B -m tests.run_live_lifecycle_offline
```

覆盖真实文件代次续接、错误Manager／订单机绑定、组件协议、稳定目标冲突、暂存写入门控、原代备份、准备失败保留原字节、Recording无backend收尾、主错误与停机错误同时保留，以及08共享账户信号恢复失败的原文件保护。原生EMA数值及真实外部系统联调仍需安装引擎的运行环境。

[公共会话生命周期与对应文件](LiveSessionLifecycle.md) · [返回函数索引](../README.md)
