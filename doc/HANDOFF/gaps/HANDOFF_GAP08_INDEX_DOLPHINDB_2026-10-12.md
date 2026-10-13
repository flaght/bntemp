# GAP08 指数分钟原表接入交接

本页保留PF1202历史设计。当前PF1203按用户确认改为通用原表Provider及配置驱动Schema，四个指数专用生产模块已退役；当前接口、部署迁移和验收见 [声明式特征交接](HANDOFF_GAP08_DECLARATIVE_2026-10-12.md)。

日期：2026-10-12。变更号 PF2026101202。状态：源码交付、静态审阅；本机未执行Python、测试、编译或数据库。真实类型、读取时延、文件往返、08在线消费仍待Linux验收。

用户确认 `min_bar/index_1min` 是指数分钟行情，`minTime` 是结束标签，列为
`minTime date Code open high low close prev_close volume turnover pct_change updateTime`。
截图确认表名index_1min，默认DFS路径dfs://min_bar；实际colDefs类型与样例尚未返回。
不能因字段名将updateTime认定为历史首次可用时间。

## 实现与责任

- `datahub/features/index.py`：不可变IndexMinute，真实OHLC/volume/turnover及可空prev_close/pct_change；一分钟Schema、显式Code业务键。
- `dataprep/features/index.py`：原表DolphinDbIndexProvider，open核验实际类型，DATE与TIME/MINUTE等拼接或完整时间戳，上海→UTC纳秒，结束标签只转换一次。有界整批查询、不截断、版本去重、错误清晰；不添加结算字段。
- `dataprep/features/dolphin_query.py`：只读查询会话，与行情流/参考资料会话分离；复用既有DolphinDbReferenceConfig.from_env接口，SDK延迟加载，凭据不输出。
- `market/stream/dolphin/index.py`：公共DFS查询Feed，在控制线程刷新DataHub，只将市场pending分钟的同分钟指数送入原生Bar，启动不灌入整日历史。精度损失失败；源分钟年龄不因轮询更新。
- `trader/runtime/live/channels/index.py`：只读缓存就绪检查，无数据库IO。
- `demos/08_option_vega`：新增可选index-source dolphindb-dfs；原流通道默认保留。查询不持帧锁，显式同分钟指数缺失超时跳过整次决策，报告skipped_index_frames。指数只有行情绑定；Greeks、选约、退出公式不改。
- 公共ctp_recovery.py等待循环增加可选poll_index钩子；没有此方法的01/04及其他旧Runner保持原行为。新增恢复夹具测试，安排live-common回归。
- 通用codec修正允许为空字段的Parquet解析；manifest的publication_evidence改为record_available_ns，避免将观测时间误标为上游发布证据。旧manifest仍可读。

observed默认以查询完成时刻为available_ns/观测revision，质量位2；重复内容保留首次观测，内容改变才建新版本。同一批同一分钟冲突快照失败。在线读返回的数据可晚于请求开始as_of，但Hub不会在真实可见时刻之前返回它。

update-time仅在来源确认updateTime确为首次发布/修订可用时间后显式选择；以真实时间为revision标识，不声称上游存在独立修订序号。相同发布时间内容变化失败。原表若只存最后覆盖值，无法重建消失历史修订。observed历史下载不能回填为业务分钟即刻可用，也不能用于下载前决策的因果研究；文件回测仍从标准导出文件按available_ns/received_ns消费。

## 部署

完整源清单：`doc/evidence/checksums/PF2026101202_SHA256.txt`。按清单同步全部 `.py`，特别是新文件、两套features包和两个验收入口；文档只用于本地交接，远端**无需上传doc目录**。
`tests/run_feature_datahub.py` 内置当前被测源码SHA，默认无外部manifest依赖，入口自身SHA只审计。不要将原PF1201历史manifest作为新版本默认要求。

## 远程离线验收

```bash
python -u -m tests.run_feature_datahub
python -u -m tests.run_cross_section_acceptance \
  --only index-query-live --only option-vega --only option-vega-live --only live-common
```

新增原表17个测试方法（不联网、无原生Bar前置），查询Feed8个原生夹具方法，08装配增加2个真实Runner/Recording用例，公共恢复增加1个可选查询等待用例。数量是静态计数，未写成通过结果。
第一命令仍含原35个特征测试、入口5项及legacy/D0；第二命令验证新增Bar路径和受影响既有行为。数据库SDK不是无网络组前置条件；pandas/Parquet引擎需要可用。原生组使用目标环境真实Bar扩展、数据库为注入夹具，不记作在线验收。

## 真实DolphinDB只读验收

凭据复用项目 `.env` 的DDB_HOST/PORT/USERNAME/PASSWORD，不在命令中填写密码。

```bash
python -u -m scripts.integration.features.index_readonly --connect --inspect
```

输出colDefs及000852至多3条源样例，错误报告也保留已取得Schema；只证明结构，不证明分钟时延/全日覆盖。

下一步选实际有000852数据的结束分钟区间，INDEX_START/END为带+08:00的ISO时间，INDEX_EXPORT为尚不存在的.csv/.parquet：

```bash
python -u -m scripts.integration.features.index_readonly --connect \
  --key 000852 --start "$INDEX_START" --end "$INDEX_END" \
  --timestamp-label end --availability observed --export "$INDEX_EXPORT"
```

实际读取为空则失败；标准导出之后用FileFeatureProvider逐记录比较，保留版本、可用时间、源身份和价格。报告写入tests/results/index-readonly-*/summary.json。历史as-of仅允许来源已确认update-time。

08沿用真实日期/日历、参考资料及CTP环境命令，仅将指数段改为：

```bash
--index-source dolphindb-dfs --index-database dfs://min_bar --index-table index_1min \
--index-bar-timestamp end --index-availability observed \
--index-poll-seconds 0.2 --index-query-timeout 2 \
--index-price-precision 4 --index-size-precision 4
```

先Recording验收真实分钟帧、source身份和skipped_index_frames。查询0.2秒间隔不等于承诺0.2秒数据库延迟；超过frame-wait-seconds的指数不补旧决策。Model/柜台成交各有既有授权和独立验收条件。

## 历史状态

PF1201首轮features35项中1项期限检查失败；后续预检定位远端market/frames.py缺失已交付的4行修复。该文件本地当前SHA984d7a11...，保持原修复。用户最新option-vega33项OK，但未提供feature runner完整通过结果。PF1202代码进一步扩展后，需要按新清单部署及验收，不能以旧33项结果关闭GAP08。

MongoDB标准Provider接口已存在，本次只新增DolphinDB原表场景。GAP08整体及真实指数链保持开放；历史证据和旧清单不改写。
