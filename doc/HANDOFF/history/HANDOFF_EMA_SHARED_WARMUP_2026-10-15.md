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

## 后续修正：完成标签与自动复权（同日）

本节替代上文“CU表及字段待确认”的状态。用户远程证据确认
`dfs://min_bar/shfe_1min` 存储原始OHLC，没有adjusted_close；CU2611的
2026-10-14/15累计因子均为1.484671。用户明确shfe_1min、cffex_1min及index_1min
的minTime均表示已完成分钟，与原来的start假设不同。

实现新增time_label=end：21:01转为21:00:59.999999999；午夜00:00标签的分区过滤
使用次日date，标准事件仍为前日最后一纳秒，避免漏读。start及end_ns原契约继续支持。
新增history_cu.json，三份已有CFFEX历史配置改为end；旧start证据不自动视为新口径通过。

01读取原始历史时默认接SessionReferenceFactorAdjuster。每根分钟由已有SessionCalendar
确定交易日，包含夜盘、跨午夜、周末；source模式选择该交易日前最新角色来源日，
再按该来源日和固定真实合约读取唯一pcr_cumfactor。trading模式使用交易日。
历史close乘因子得到指标输入，原始Bar不修改；缺失、重复、身份错误、显式未来发布时间
或读取期间版本变化均拒绝，不手填逐日映射、不默认因子为1。

自动模式为Live指标预热，缺历史发布时间时明确标记当前读取版本，
不宣称历史时刻可见；实际available_ns仍约束历史分钟。保留显式映射旧模式及预复权字段模式。
这不改变回测的时间可见性政策，也不把当前因子无条件套用于全部历史。

当前修改需同步：history/{__init__,dolphindb,factors}.py，01的history.py、run_live.py、
history_cu.json及三份已有历史JSON，scripts/integration/history/readonly.py，
tests/test_history_session_factors.py、tests/test_history_provider.py及tests/run_main_ema_acceptance.py。

离线通过135项（去重）：新增会话因子/完成时间9项、旧因子独立于原生扩展的16项、
历史Provider/Service/只读探针34项、共享账户76项。Provider组仅隔离trader包初始化，
实际加载原Provider、服务与日历；没有伪装Bar/指标原生扩展可用。
完整history-adjustment组本机尝试32项、15项因缺原生Bar或indicators无法执行；
未将该组登记完整通过。AST与diff检查通过，真实库/柜台未连接。

远程同步后先离线运行：

```bash
python -u -m tests.run_main_ema_acceptance --only history-provider --only history-adjustment --only shared-account --only entry
```

使用新版history_cu.json，不保留错误adjusted_close映射或旧start标签。
原失败运行的检查点和报告继续保留；不要删除状态后重复报单启动。
实际DDB窗口覆盖、实时接续、D1成交闭环仍待远程证据。
