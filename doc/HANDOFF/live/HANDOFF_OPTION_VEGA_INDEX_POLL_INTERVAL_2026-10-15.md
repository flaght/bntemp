# 08指数轮询间隔运行覆盖

用户要求将当前0.2秒指数DFS轮询调整为1秒。已给08快捷持续入口新增`--index-poll-seconds`，范围(0,30]秒，省略时保持原检查点的配置。

## 恢复兼容方式

该参数作为本次进程运行调度覆盖：入口仍使用原argv、账户预算和会话身份进行严格恢复；装配好指数FeatureQueryCache后，在连接前通过公共`set_poll_seconds`设置实际轮询间隔。不会修改检查点里的来源声明、策略参数、已选约日期或归属仓位，也不写一份“改过身份”的JSON。下一次不带该参数启动，将恢复原保存的轮询间隔；需要一直1秒时保留启动参数。当前常驻进程每窗口都会重新应用。

公共缓存的`manifest.poll_seconds`反映实际间隔。启动输出`指数轮询运行配置`含saved_poll_seconds、effective_poll_seconds及process_runtime_only；每窗口报告增加runtime_index_polling。market_sources中保存来源声明的poll_seconds仍可能为0.2，那是用于恢复的原声明，实际频率看上述运行字段和feature_queries[].poll_seconds。

仅08快捷入口本批新增此CLI覆盖；已有多策略配置/原生run_live的参数身份政策保持。本次不放宽任何来源、可见性、账户、合约或策略参数核验。

## 使用

当前策略Ctrl+C正常保存退出，更新源码后执行原命令，只增加：

```text
--index-poll-seconds 1
```

现有账户及signal文件、时段库/表、交易授权、--run-forever等保持原配置。源代码补充包：

```bash
cd /workspace/worker/pj/Chrono/nautilus_trader_demo/pro || exit 1
tar -xzf doc/evidence/archives/OPTION_VEGA_INDEX_POLL_INTERVAL_20261015.tar.gz
sha256sum -c doc/evidence/checksums/OPTION_VEGA_INDEX_POLL_INTERVAL_20261015_SHA256.txt
```

成功应用时可见：

```text
指数轮询运行配置: {"strategy_id":"option-vega-live","dataset_id":"index.minute","key":"000852","saved_poll_seconds":0.2,"effective_poll_seconds":1.0,"scope":"process_runtime_only"}
```

查询完成后至少等待1秒，实际还包括数据库耗时和调度耗时；不是严格每秒固定发请求。已有FeatureQueryCache启动时的强制首查保持。帧接收期限仍为原20秒或保存值；若指数更晚到达，按原规则跳过，不延长期限。频率降低不修复过期候选Bar或指数上游发布延迟。

## 验证

本地无网络，SDK实体/数据库/柜台为夹具，公共缓存、08实际装配与执行逻辑保持：原81项回归、新5项（1秒节流且不清缓存、非法频率、离线参数、身份保持且新帧真实进入策略、缺省不覆盖）、既有共享21项、分钟日志7项，共81+33通过。原空仓归属及严格身份恢复由既有回归保持。没有连接服务器修改运行中进程；实际生效待用户更新后启动确认。
