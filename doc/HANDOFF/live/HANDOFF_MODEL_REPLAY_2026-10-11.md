# Replay模型运行、恢复验收与MD报告修正（PF2026101101）

## 最新验收状态（2026-10-11）

以下状态来自用户提供的远程最终报告。行情来源为CTP第二套Replay，
执行来源为reference-price模型，account_reconciliation=off；模型仓位
独立于物理账户。要求MD/参考交易日20261009，资料来源日20261008。

| 验收项 | 状态 | 运行证据 |
| --- | --- | --- |
| 01真实MD驱动模型运行 | 通过 | 9根Bar、1笔模型成交，最终目标与策略仓位均为RB=-1 |
| 04真实MD驱动模型运行 | 通过 | 9个同步帧、6次调仓、12笔模型成交；最终AU=0、AG=+1、SC=-1 |
| 04同文件重启恢复 | 通过 | 接续目标版本6→7，第4帧才执行新目标，6笔模型成交；最终AU=0、AG=-1、SC=+1 |
| 01模型重启恢复 | 待验收 | 尚无本轮远程恢复输出 |
| 01/04同时在线运行 | 待验收 | 已贴运行时间不重叠，不能用单独通过代替并行证据 |
| 第三方多账户真实委托/成交 | 待验收 | 本轮account_dispatch为空，回报均为execution_origin=model |

三份成功报告的完整摘要、逐笔模型成交、原粘贴文本SHA256及北京时间
保存在[结构化证据](../../evidence/live/MODEL_REPLAY_20261011.json)。金额/
数量使用十进制字符串，原始输出解析不执行粘贴代码。

### 04首次成功运行

报告cross-section-model-1791706100508175757，北京时间2026-10-11
16:08:20开始。synchronized_frames=9、rebalances=6、fills_received=12、
order_updates_received=24；目标、strategy_positions、execution_positions
一致为au2612.SHFE=0、ag2612.SHFE=+1、sc2611.INE=-1。未归属仓位全为0，
account_positions=None、observed_td_trading_day=None、cleanup_errors=[]，
status=passed。模型成交包含平仓及反向开仓，成交ID各自唯一。

### 04重启恢复

报告cross-section-model-1791707067358178583，北京时间2026-10-11
16:24:27开始，沿用原文件demos/04_cross_section/results/
model-cross-section-20261011-080812.json。恢复的首批订单request_revision=7，
接续前一会话的6个目标版本，synchronized_frame=4；AG current=+1先卖出平仓，
SC current=-1先买入平空再买入开多，AU从0开空。第7帧再调整AU/AG。
粘贴命令片段不完整，恢复判定依据实际版本、旧仓位和新目标时点，不补造命令日志。

本轮synchronized_frames=9、rebalances=6、fills_received=6、模型回报12条，
最后目标、策略仓位与执行账本一致为AU=0、AG=-1、SC=+1。未归属仓位全为0，
cleanup_errors=[]、status=passed。该原文件已经保存新仓位；下次恢复应从此
最新状态开始，不能再把首次成功的AG=+1、SC=-1当作当前文件内容。

此前15:18无行情失败记录保留在下节，后续16:08成功说明本轮市场数据链路
已恢复，不据此证明每天的开放时段或两策略并行。成功报告的实际MD日仍显示None，
与停机清空状态的问题一致；MD报告修正版是否已在远程同步尚无校验结果。

本次只更新文档/证据，运行代码保持REV2；本机公共回归证据仍为110项通过。
用户还报告集中入口“全部选定离线模块通过”，归属模块18项通过；未补造
其他组的逐项数量。当前完整清单为
[PF2026101101_REV3_SHA256.txt](../../evidence/checksums/PF2026101101_REV3_SHA256.txt)，
旧清单保留为历史版本，本次文档更新不要求重复运行已通过测试。

## 首次运行与MD报告修正记录

用户提供01/04两份远程输出。本次本机未连接远程MD/TD、数据库或第三方端。

01报告model-1791673733149458028：北京时间2026-10-11 07:08:53开始，
bars_used=9，ACCEPTED/FILLED各一条，模型SELL一手，last_target=-1，
strategy_position=-1，execution_positions rb2701.SHFE=-1，account_position=None，
observed_td_trading_day=None，cleanup_errors=[]，status=passed。
这是真实MD驱动的模型成交证据，不是柜台成交证据。

04报告cross-section-model-1791703103161891710：北京时间2026-10-11
15:18:23开始，session_readiness.ready=False，STREAM_UNHEALTHY/STALE、
RECEIVE_MISSING、MD_TRADING_DAY_MISMATCH actual=None，AU/AG/SC原始行情
事件/接收时间均为空，synchronized_frames=0、模型回报0，status=failed。
卡在行情就绪之前，没有执行端不就绪条目，也没有账户锁冲突证据。
两次相隔约8小时，不能声明并行在线验收通过。

04时间在用户此前提供的第二套环境截图16:00开放窗口之前，推测环境尚未
提供行情；日志缺少MD连接/登录/订阅信息，不能仅凭此确定柜台服务状态。
应在原始行情探针确认所有合约有行情后，再同时运行两个模型入口。

01停机报告observed_md_trading_day=None是报告读取时点问题：正式
CtpLiveDataFeed._stop_network_client会清空latest_trading_day。修正在
runtime/live/channels/ctp_market.py中，停止前保存实际观测日，停止后
snapshot沿用此值。未收行情时仍为None，不用expected日伪造观测值。
策略、就绪规则和Feed状态清理保持原行为。增加两项回归，live-common
110项通过（此前108项加报告保留/行情缺失两项）。

代码变更仅ctp_market.py、tests/test_execution_composition.py；架构和交接
索引记录本次证据。新完整部署清单为PF2026101101_REV2_SHA256.txt，旧摘要保留。

远程先复测公共组，再在MD可用时采样（只连接行情，不发单）：

```bash
sha256sum -c doc/evidence/checksums/PF2026101101_REV2_SHA256.txt
python -u -m tests.run_cross_section_acceptance --only live-common
export CTP_MD_ADDRESS=tcp://182.254.243.31:40011
export CTP_PRODUCTION_MODE=true
for PROBE_SYMBOL in rb2701 au2612 ag2612 sc2611; do
  CTP_SYMBOL="$PROBE_SYMBOL" CTP_CONNECT_TIMEOUT=30 CTP_MARKET_TIMEOUT=60 \
    CTP_MARKET_SAMPLE_SECONDS=5 python -u -m tests.run_ctp_live --stage 4 || break
done
```

原始TradingDay若变化，重新核对参考资料交易日/来源日，不能盲目沿用20261009。
探针全部通过后按上一份交接的两个完整model命令同时启动，分别使用新文件名。
04失败启动可能已保存文件，新验收不用该文件冒充成功恢复。01原成功文件保留，
其--resume另行验收。第三方多账户真实回报尚未提供，仍待独立验收。
