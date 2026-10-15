# 01 EMA 共享入口 DDB 预热

2026-10-15，Asia/Shanghai。范围仅为01；03/04待后续接入，08无需历史窗口。

## 交付

- 复用01现有HistoryService、DolphinDB Provider、EmaCheckpoint、实时缓冲及去重逻辑。
- 共享工厂允许01历史参数及共享同日resume；保持其他策略与持续入口原边界。
- 同进程宿主在授权前准备历史；独立worker及宿主单策略重启在begin_bars前准备历史。
- 查询后重新刷新参考、消费执行事件、轮询输入并核对健康。查询异常或健康失效不开放Bar。
- 历史仅更新指标，不推进目标版本、订单或仓位。共享恢复仍由账户检查点负责交易事实，EMA重新预热。
- 仅传history-config/file而history-minutes为0时，启动请求默认使用slow周期；日志报告实际请求根数。
- 宿主逐策略与worker报告增加history/history_audit/bars_used，保留实际来源与覆盖。

## 配置

沿用现有共享启动、停止、同日resume命令；在01策略argv中加入：

```text
--history-db dolphindb
--history-config /absolute/path/to/ema-history.json
--history-minutes 240
--history-stage recovery
--history-missing-policy fail
```

路径必须替换为实际配置，并配置对应品种的既有会话日历、时段和日期例外。
窗口可配置，240不是统一门槛；默认3/5周期最低初始化为5根。
recovery直接数据库；preopen保留文件优先、数据库补足。
启用DDB验收须核对报告sources实际包含dolphindb，不能用文件或实时预热替代。
已有状态的身份包含argv；不允许给旧状态直接添加历史参数后宣称身份一致，
应从启用预热的新配置/新状态完成首次验收，再用原配置同日resume。

## 本机验证

Python 3.12，PYTHONPATH=.validation/packages，无数据库或柜台连接。

- tests.test_shared_account：76项通过。新增覆盖预热早于授权、历史失败及加载后失去健康不开闸、
  受控DDB Provider推进EMA但不生成目标/报单/仓位、工厂仅允许01共享历史及resume；
  原单策略重启测试增加历史阶段调用检查，并继续验证其他策略版本与仓位不变。
- tests.test_shared_process_cli与tests.test_shared_ipc：22项中21项初次通过，
  唯一Unix socket用例受沙箱bind限制；获准后单项复验通过，22项均有通过结果。
- 修改的5个Python文件AST解析与git diff --check通过。
- tests.test_history_provider：导入失败，本机bomber.framework.fixes缺原生Bar，未执行该组用例。
  不能把本次共享回归扩大为完整原生历史/实时接续通过。

部署机有原生扩展后运行：

```bash
python -u -m tests.run_main_ema_acceptance --only history-provider --only shared-account --only shared-ipc --only entry
```

## 尚未验收

本次完成源码接入和上述离线验证，不登记M06实际场景完成。
CU真实分钟表、字段、时间标签及历史复权价/因子映射尚待部署资料确认，不能使用IF样例冒充CU来源。
原生历史组、真实DDB覆盖、加载期间实时接续及D1交易闭环仍需运行环境验收。
