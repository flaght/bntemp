# 08共享快捷入口：重启参数身份漂移修复

用户在分钟日志升级后，同日正常重启失败于`SessionIdentityCheckpoint.restore_state`，报会话身份不一致；尚未进入策略判断或发单阶段。仅凭原异常没有字段差异，不能断定服务器是否还存在其他资料变化。

## 已复现缺陷与修复

首次从原生08导入共享执行时，快捷入口从原身份构建argv，账户检查点保存这份参数列表。同时解析后的08 config也保存到reference。下次快捷入口再次从reference.config重建argv，数值默认项可以出现`--min-time-value 5`变成`--min-time-value 5.0`（字典重排也会影响参数顺序），导致保存的argv与本次声明不相等。信号/账户严格身份核验因此拒绝，尽管策略数值语义没有变化。新增分钟日志本身不进入恢复身份。

已共享化的08快捷恢复改为直接复用原账户检查点中保存的完整策略argv和账户预算，不再次序列化解析后的reference.config。原生08首次导入保留已有流程。配置仍由原入口解析，当前合约/参考来源/风险/账户/策略仍做原严格校验；不对5与5.0做全局宽松比较，不自动改JSON身份，不重建持仓所有者。

公共身份失败信息增加差异字段路径（最多12个），不输出字段值，避免潜在凭据泄漏。若服务器修复后仍失败，将显示argv、reference、routes、sources等具体路径，继续按真实差异处理，不能强行覆盖。

## 部署

此时失败进程已退出，不需为本修复更改柜台仓位或重建JSON。在服务器pro根目录同步补充包后：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/OPTION_VEGA_RESUME_IDENTITY_FIX_20261015.tar.gz
sha256sum -c doc/evidence/checksums/OPTION_VEGA_RESUME_IDENTITY_FIX_20261015_SHA256.txt
```

随后执行之前相同的INFO启动命令，沿用同一state-file与默认旁侧signal文件、原订单上限。仍须显式指定时段`dfs://bomber_static/fut_trading_session`。本补充包建立在已部署的分钟日志版本之上，不覆盖那个包；旧包/旧摘要保持历史字节，不用于校验本修复版本。

可选服务器回归：

```bash
python -u -m unittest tests.test_option_shared_rollover
```

## 验证

本地SDK实体替身下，实际公共账户服务/归属/JSON原子保存/08工厂完成：原生检查点导入→正常停机→旧入口重建argv被拒绝且原文件未修改→复用保存argv成功同日恢复空1手，未产生测试委托。真正修改选约时点继续被拒绝，原文件保持；订单预算更改仍拒绝。差异字段测试验证不泄漏旧/新密码值。

原08 81项、21项共享跨日（含2项新回归）、分钟诊断7项、共享调度9项通过；公共Live117项通过。真实柜台恢复仍待用户返回新运行结果，不能把离线复现当成服务器问题唯一原因或在线通过证据。
