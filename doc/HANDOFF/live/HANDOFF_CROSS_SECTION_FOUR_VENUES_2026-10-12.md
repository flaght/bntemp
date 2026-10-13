# 04横截面四交易所支持（PF2026101208）

最新真实off/fills运行：用户报告cross-section-simnow-1791814045283164343。
MD/TD均20261013，七个合约都有原始Tick及深度观察，timestamp_regressed均false；
账户关联off、策略来源fills、account_positions=None，独立账本成功进入会话。
实际结果failed：参考缓存观测过期或时钟回退，synchronized_frames/rebalances/
orders/fills均0、活动单0、cleanup_errors[]，本策略和执行账本各合约均0。
未形成同步帧/目标/成交，不能登记四交易所交易通过。临时硬件工具缺失没有
阻止登录/行情就绪，不是本次退出点。原状态路径仍是night-20261013-20261012-
130844-134697.json，保留该文件；本轮没有新归属成交，可使用独立新文件重试。

首次参考准备从effective_ns1791814045572745678至first_observed_ns
1791814067168853419约21.596秒，默认参考观测上限30秒。控制线程还串行
刷新资金/订单，刷新期间行情线程继续读取此前发布的参考缓存；存在旧缓存
先过期、后续成功刷新再发布的可能。最终manifest的observed_at_ns较新，不足以
证明异常当刻缓存年龄，也没有确切系统时钟回退证据，原因仍按慢刷新推断。
下一次建议保持off/fills及行情时效参数，显式设置
`--reference-max-observation-seconds 120 --reference-max-refresh-duration-seconds 60`，
这两项为已有CLI能力，只扩大有界参考时效/刷新耗时预算，不取消过期/失败/
交易日/版本检查，不更改生产默认。若仍失败，需补异常时的缓存年龄与刷新
耗时诊断再定位，不把相同报错直接认定为NTP回退。本次只记录证据及参数路线，
未运行本机Python/测试/柜台，不补造REV2离线通过。

2026-10-12用户纠正执行配置：策略管理自己的仓位，但报单到SimNow。
七品种实时参考已实际取得：AU/AG/RB/NI为SHFE，SC为INE，I为DCE，
MA701为CZCE；TD20261013、来源20261012、参考ready，尚未启动MD或报单。
本次失败来自命令默认strict，管理合约中已有AG空1/SC多1而拒绝空仓启动。
此前回复将strict限制当成唯一运行路线不符合用户需求，现修正命令为已有
`--account-reconciliation off --strategy-position-source fills`，保留mode simnow及
报单授权；不用model或改生产默认。off不导入物理仓位，策略账本只按本策略
归属的真实成交更新，资金/活动订单/物理账户锁仍保留。独立新文件从零策略
账本开始，旧物理仓位不自动归属，策略新增报单仍改变同一柜台总仓。
继续已有策略账本应使用同日/同组合/同政策原文件，不能新文件丢掉归属。
本次只修正文档，生产代码/测试/验收摘要不改，也没有操作柜台旧仓。
四交易所参考加载已取得真实证据，原生行情/订单/恢复未通过本次失败证明；
REV2离线是否通过未在此附件展示，不补造结果。

最新修订PF2026101208_REV2：用户远端04组76项，75项未报错、1项错误，
测试0.799秒、模块exit1/2.052秒。唯一错误是新增恢复用例在runner.stop()
解绑策略后调用strategy.position，属于测试生命周期错误。停机前保留策略API
仓位断言，停机/恢复后改查真实runner.position_manager归属账本；ledger及
revision断言、真实stop/restore、停机前权威今昨仓查询都保留，生产代码不改。
已过期Bar日志为既有测试场景，临时报告为无网络夹具，不是实际柜台记录。

在原版完整同步基础上，只同步PF2026101208_REV2_FILES.txt列出的3个测试/
验收文件；两个入口摘要配套更新，文档无需上传。先仅复测失败cross-section组：

```bash
python -u -m tests.run_cross_section_acceptance --only cross-section
```

REV2本机仅静态读写与摘要检查，未执行Python/测试/编译；修正后远端仍待验。
此摘录没有其他组输出，不登记它们通过或失败。04通过后补此前未通过或未运行
的live-common/entry/assembly/ctp-recovery组，已通过组不用重复。原版历史摘要保留。

用户要求支持SHFE、INE、大商所DCE、郑商所CZCE。触发日志为七品种
`AU,AG,SC,RB,I,MA,NI`在组合参考准入时失败；TD已登录20261013，
orders_submitted=0，柜台AG空1/SC多1、活动单0。本次只修改本地代码，
没有连接柜台、接管/平掉旧仓或修改交易状态文件。

## 实现

1. `dataprep/live_portfolio.py`共享四交易所白名单和期货代码适配。
   SHFE/INE/DCE使用小写YYMM；CZCE资料中的MA2701或MA701映射到
   MA701.CZCE。源资料、条款、因子和指纹保留；只适配在线角色快照。
   CLI可核对原生代码或资料代码，四位郑商所预期额外核对原始条款，
   避免不同十年位被截断成相同柜台代码。不推断主力/交割年，不扩展期权。
2. 04装配为DCE/CZCE显式启用通用CLOSE，SHFE/INE原有平今/平昨保持。
   沿用先平旧方向、真实成交后新Tick续开；不在成交回调直接续开。
   公共builder/accounting增加可选今仓优先的通用平仓记账参数，默认空集合
   保持旧调用方行为。两交易所成交账本按今仓优先、再昨仓分配。
3. `runtime/ctp_account.py`为DCE/CZCE按Position及TodayPosition拆分今昨仓。
   不使用静态YdPosition估计当前昨仓；缺失、负值、非整数、超过总仓均拒绝。
   聚合成本不足以分别恢复今昨成本时拒绝require_cost，不比例拆造成本。
   本次恢复调用方不要求成本；SHFE/INE分桶解析保持。
4. CtpMdConfig与TickConverter新增可选dce_use_receive_date，默认关闭。
   04的realtime启用：忽略DCE不可靠的ActionDay，以北京时间接收日附近最近
   日期拼接原始UpdateTime/毫秒，兼容夜盘和午夜前后的短延迟Tick。
   TradingDay、逐腿事件年龄/接收年龄、回退、全局健康等检查仍生效。
   replay不启用新规则，继续保留源事件时间并以接收时间聚合Bar。

策略公式、预算取整、目标cap、风控、状态文件格式及同日恢复范围不改变。
原生行情订阅、报单、回报身份校验、柜台仓位及恢复都使用统一CTP代码，
不需要修改MD/TD C++扩展或重新编译。数据源仍须为角色、因子和基础条款
提供一致的真实合约身份及有效生命周期；不把跨表代码缺失当作已有资料。

规则核对来源：[CTP客户端开发指南](https://github.com/ctp-api/CTP-Quantitative-Trading/blob/main/CTP%20Client%20Development%20Guide.md)
的持仓汇总字段定义，以及[VeighNa持仓转换实现](https://raw.githubusercontent.com/vnpy/vnpy/master/vnpy/trader/converter.py)
的通用平仓和今仓优先记账。DCE接收日期处理也与仓库内
`3rd/vnpy_ctp/vnpy_ctp/gateway/ctp_gateway.py`的实时自然日处理核对。

## 部署与离线验证

本机遵循现有静态工作约定，未运行Python、测试、编译或在线连接。
新增04无网络方法11项（源码共76项），公共今昨仓解析方法3项；这些是
静态方法计数，不是通过结果。覆盖四交易所参考/CLI、郑商所真实MD代码、
同步完整帧、原生报单/成交/反手开平标志、去重、检查点恢复与权威查询、
缺腿深度、DCE夜盘/午夜、原始Replay日期、七品种预算不足整组归零。

按`doc/evidence/checksums/PF2026101208_FILES.txt`同步18个Python文件；
其中6个为未改的相关执行/恢复基线依赖。不要覆盖results/状态文件。
文档不需要部署远端。集中入口内置四交易所16个依赖摘要，在原生导入前
拒绝混版并打印具体路径、expected/actual；不要求远端存在doc/。
共享CTP依赖的08验收摘要也更新，独立feature-datahub入口只更新对应入口
摘要，不改特征数据业务。

已安装运行依赖的远端pro目录执行：

```bash
export PYTHONPATH=.
python -u -m tests.run_cross_section_acceptance \
  --only cross-section --only live-common --only entry \
  --only assembly --only ctp-recovery
```

04组覆盖新增四交易所语义；live-common覆盖聚合仓位/默认配置；entry、assembly
及ctp-recovery补验共享builder/feed/account解析的原有调用方。信号算法和
未改变的其他已过组无需无故重复。所有组当前待远端执行，真实四交易所
MD/原生订单/恢复另行验收，不能拿历史三品种或模型结果替代。

## 夜盘使用

同步并通过离线后，原命令可以使用：

```bash
--products AU,AG,SC,RB,I,MA,NI
```

当前真实四交易所行情/订单仍待验证。首次新会话继续核对TD/MD交易日、DolphinDB来源日、
当前主力及全账户无活动单。strict还要求管理合约空仓；用户选择独立真实成交账本
时显式传off/fills，不导入物理仓位。日志显示的AG空1/SC多1属于柜台旧仓事实，
本项没有处理这两笔仓位；若它们属于待继续的本策略，不能新文件丢掉旧归属。
既有文件仍只支持同交易日、同合约组合和同执行政策恢复，不将旧三品种/旧日
或Replay文件直接改成今晚七品种。

七品种group_fraction0.40每侧两个，200万侧预算分成每入选品种100万；
预算不足一手且require_full_groups启用时全组合归零，cap不会强制补一手。
不同品种交易时段不同：休市腿不产生Bar时没有完整帧，逐腿行情陈旧还会
闭闸结束会话；不会只交易剩余品种。有限运行时长及报单20笔上限保持，
停机撤活动单，不自动平仓。

## 协调状态

当前对话已通知公共修改及完成内容，登记在公共协调文件第1节及第8节。
未授权向其他聊天发送消息，跨任务待同步。默认配置兼容不等于本轮已验收。
本项状态为代码完成、摘要静态核对完成、远端离线/真实交易待验。
