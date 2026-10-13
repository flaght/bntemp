# GAP08 配置驱动特征接入交接

2026-10-12最新覆盖定位：用户截图的原表order by minTime/top5从11:25开始，依次至11:29；所以上次09:31—11:27区间返回11:25/11:26/11:27三行与原表和结束标签过滤一致，没有时间过滤异常证据，无需改代码或重复该读取。截图未展示count/min/max统计结果，不记当天总行数或末分钟。用户同时确认index_1min持续更新当天行情；证据GAP08_DOLPHINDB_COVERAGE_20261012.json。

当前通过范围：通用能力离线9组252项及D0、真实DolphinDB元信息/样例、三行标准化读取/CSV逐记录往返。五条量额均递增，支持累计推断但尚未确认累计规则/单位，不差分或自动换单位。updateTime为最后更新时间，保持observed。08只消费指数close，不将当前量额定义推广为标准分钟量额。

下一步在行情时段，以原有真实日历/会话/CTP行情参数启动08 Recording，指数段设--index-source dolphindb-dfs --index-dataset index.minute --index-poll-seconds 0.2 --index-query-timeout 2 --frame-wait-seconds 5。--mode recording记录目标，不连接TD或产生模型成交。会话日期、估值日期及指数/交易腿行情须一致；实际calendar路径按运行机已有真实文件设置，不虚构路径或补工作日日历。验证同分钟完整决策、实际收到时间/期限、超时skipped_index_frames及不补旧目标、缓存健康与恢复就绪。5秒是初次验收预算，不承诺查询/发布延迟；测量后由策略配置调整。当前尚无这轮真实Recording通过证据，GAP08整体仍开放。仅文档/证据更新，无需部署，清单PF2026101203_REV2_COVERAGE_SHA256.txt。

以下区间覆盖排查保留为历史：

2026-10-12最新真实读取：报告feature-readonly-1791777601420035517，status passed、records3、file_roundtrip passed。Provider.open类型核验、实际时间拼接/标准化、DataHub快照和CSV逐记录往返已通过；三个事件正确对应上海2026-08-11 11:25/11:26/11:27。availability为当前观测，available_ns=1791777601957611581，不能视作8月历史可用性。

当时覆盖待定位：请求09:31—11:27却只有11:25—11:27三行，head/tail为相同数据，没有取得开盘数据；此前量额累计推断未新增证据。静态核对区间读取默认上限10000、RawProvider top max_rows+1，不使用inspect的三行样例限制；不能据此猜测源缺数据或宣布过滤错误。为此提供以下两条只读查询：

```sql
select count(Code) as rows, min(minTime) as first_minute, max(minTime) as last_minute
from loadTable("dfs://min_bar", "index_1min")
where Code = "000852", date = 2026.08.11;

select top 5 date, minTime, Code, volume, turnover
from loadTable("dfs://min_bar", "index_1min")
where Code = "000852", date = 2026.08.11
order by minTime;
```

原表统计若只三行则是该日来源覆盖不足，应换实际完整日期验证累计/单位；原表若有早盘记录则排查标准时间过滤。证据GAP08_DOLPHINDB_CSV_PASS_20261012.json，导出SHA264bf4c52370541398561099e5a25737c7dde32cae688666812c4bfac20e95f6。本次仍只更新文档/证据，无源码/配置改动，无需部署；清单PF2026101203_REV2_CSV_PASS_SHA256.txt。Parquet真实往返、连续实时来源与08 Recording仍无新增通过证据，GAP08整体仍开放。

2026-10-12当前状态PF1203_REV2：GAP08通用能力离线验收通过。用户补验features35、definitions10、index17、legacy14及D0，option-vega33、option-vega-live12、live-common113全部通过；加上此前entry9及feature-query-live9，共9组、252项测试及D0验证。新报告feature-datahub-1791777070789295266、cross-section-acceptance-1791777092792985540，证据GAP08_DECLARATIVE_OFFLINE_PASS_20261012.json。真实库及在线策略验收仍开放，GAP08不整体关闭。这里只更新文档/证据，无源码或配置改动，无需重新部署或重复已过组；清单PF2026101203_REV2_OFFLINE_PASS_SHA256.txt。

真实DolphinDB探针已通过：报告feature-readonly-1791777288281146665，实际dfs://min_bar/index_1min，minTime TIME、date DATE、Code SYMBOL、volume LONG、updateTime TIMESTAMP，其余数值DOUBLE；配置SHA与交付相同。证据GAP08_DOLPHINDB_SCHEMA_PASS_20261012.json。1970日期是SDK对TIME的表示，业务日期必须使用date拼接；当前映射已如此声明。inspect只读取元信息和样例，尚未执行Provider.open类型校验或标准化区间读取。

用户确认updateTime为最后写入/更新时间，继续observed，不将其作为历史首次可用证据。三个样例显示volume/turnover疑似当日累计，单位与累计语义未确认，不自动差分；pct_change三条都等于close/open-1，不能解释为较昨收收益率。08目前只消费指数close，其他策略不能默认该原表volume是标准分钟成交量。

样例updateTime比结束标签晚3.080/3.100/3.101秒，仅为历史更新差值，尚非实时查询可用延迟证据。08帧等待默认--frame-wait-seconds 2，从首条本机帧接收开始；与--index-query-timeout 2数据库查询超时不同。真实Recording可先显式--frame-wait-seconds 5，测实际接收/超时后再定业务预算；保留有限等待、超时跳过。

已执行的区间读取/CSV往返命令如下（留作历史，不覆盖既有导出）：

```bash
python -u -m scripts.integration.features.read_only --connect --dataset index.minute --key 000852 \
  --start 2026-08-11T09:31:00+08:00 --end 2026-08-11T11:27:00+08:00 \
  --export tests/results/index-000852-20260811-morning.csv
head -n 6 tests/results/index-000852-20260811-morning.csv
tail -n 3 tests/results/index-000852-20260811-morning.csv
```

本次读取记录了标准事件范围及file_roundtrip=passed，但没有开盘数据，累计语义按顶部步骤继续核验。observed导出的历史Bar在当前查询完成后才可见，不能作为历史交易日已可用的证据；本轮文件只验标准化/往返，真实历史回测需另外提供可信可用时刻/版本来源。然后08真实Recording验证同分钟对齐、迟到/超时跳过及恢复就绪。MongoDB现有证据仍为离线夹具，真实来源需另验。本机仍未运行Python、测试、编译或数据库，历史清单/失败证据保留。

以下为此前诊断/交付历史：

2026-10-12前一轮：entry9项（0.008秒）及feature-query-live9项（0.052秒、模块exit0）通过，报告feature-datahub-1791776662886287950、cross-section-acceptance-1791776669360771462；证据GAP08_DECLARATIVE_ENTRY_QUERY_20261012.json，清单PF2026101203_REV2_ACCEPTANCE_SHA256.txt。其时尚未收到其他组结果。

2026-10-12补丁PF1203_REV2：用户feature-query-live9项中CLI1项错误，解析器只接受dolphindb，确认远端08/run_live未同步已交付DFS参数。其余8项未报错，集中入口停止，后续组和完整feature-datahub结果未提供。生产源码不改；补集中入口内置特征/08摘要预检，新增3项隔离测试，当前entry9项；本机未运行。新清单PF2026101203_REV2_SHA256.txt，旧PF1203保留历史，doc仍不需部署。

最小先补覆盖 `demos/08_option_vega/run_live.py`，目标SHA为d0f7583522717554acef5de32b59317151a4ec72018986797909b6517f0c2b61。
同时核对并同步08的live_runtime.py、recovery.py，避免下一组使用旧Runner/控制器；更新三个验收文件tests/run_cross_section_acceptance.py、tests/run_feature_datahub.py、tests/test_feature_acceptance_entry.py。集中入口会在导入原生模块前逐文件列出剩余混版，默认合并内置摘要，不用外部doc清单。
先复测feature-query-live及entry预检组，已出8个成功指示也不是整组通过。随后补尚未展示的option-vega/option-vega-live/live-common和完整feature-datahub；仅变更验收代码时无需反复跑已通过业务组，本轮08补覆盖后装配需验。
证据GAP08_DECLARATIVE_DEPLOYMENT_20261012.json。真实库/Recording仍开放，不降低测试断言以适配旧CLI。

2026-10-12，PF2026101203。用户确认“普通已计算特征只新增Schema和映射配置，Payload按声明生成、类型校验且不可变”为核心，并授权继续开发。
状态：源码/配置交付，静态审阅和SHA核对；本机不运行Python、测试、编译或数据库。GAP08及真实DolphinDB/08 Recording仍待远程验收。

## 最终扩展路径

`JSON datasets[] → dataset_from_definition → frozen/slots Payload + DatasetSpec → FeatureProviderFactory → FeatureQueryCache / FeatureRefreshService → DataHub → 策略需求与帧对齐`。

普通基差、波动率和其他已计算特征只在业务清单加入dataset，不增加Python文件、特征专用Provider或Channel。
新增计算算法可另外提供计算代码；新增存储协议/物理格式才增加通用适配器。策略自己决定绑定关系、对齐和超时动作。

- datahub/features/schema.py：白名单decimal/float/int/str/bool字段，nullable、有限值、min/max及字段大小关系；声明生成不可变类型，无动态类导入或配置表达式执行。完整Schema决定类型身份及指纹，重复读取同一声明兼容；规则/频率改变不能混用旧类型。
- dataprep/features/raw.py：通用DolphinDB原表适配，无指数类、一分钟或六位业务键硬绑定。配置Source表、业务键、字段投影、DATE+日内时间/完整时间戳/UTC整数ns、业务标签、可见性、可选修订/质量列；频率偏移读Schema。类型由实际colDefs核验。
- definition.py：一个JSON清单声明多个数据集，接入file/DolphinDB/MongoDB标准信封及DolphinDB原表。连接凭据由调用方注入；文件相对路径按清单目录。
- query.py：任意特征的通用轮询缓存；多个缓存可共享同一DataHub/注册表，原FeatureRefreshService仍可整批刷新多个来源。
- market/stream/feature_query.py：只有真实OHLCV才声明Bar投影，映射字段可重命名、频率按声明匹配、精度不损失；其他因子直接读DataHub。Bar请求EXACT，启动不灌历史，只向pending分钟送达。
- trader/runtime/live/channels/features.py：通用缓存就绪检查，无数据库IO。
- 08：index_1min只作为内置features.json的配置样例；新增index-feature-config/index-dataset/index-schema-version参数。仍显式绑定000852，一分钟OHLCV是08消费要求；框架不认识MO/IM/指数。缺指数超时跳过当前决策，报告skipped_index_frames；公式和执行路由保持。
- 公共恢复的可选查询等待钩子改名poll_features；无该方法的旧Runner保持原行为。

## 配置与版本

`bomber/framework/dataprep/features/configs/features.json` 保留用户确认的index_1min十二字段、上海时区、结束标签、observed可见性。
`features.example.json` 在同一文件声明五分钟基差（decimal/int/可空str、source-time/修订/质量列）及分钟波动率（float/bool、observed），表名只是示例，未核验真实库存在。
内置两份文件是固定验收样例。业务可复制为自己的JSON再追加datasets，使用--config/--index-feature-config或load_feature_bundle选择；无需修改Provider或验收入口，自定义配置不被源码摘要强制固定。只读报告记录配置SHA及Schema指纹，08会话身份记录所选数据集、版本和映射。

observed只在实际查询完成后可见，重读不改首次观测，内容或质量改变产生新版本；同批同槽冲突失败。source-time须有来源发布时间证据，update-time仅CLI兼容别名。标准文件保留真实可用时刻与版本，不能因下载历史数据而提前消费。原表覆盖不能恢复丢失历史。
PF1202指纹/来源身份与新声明版不同，不将旧导出/旧状态直接作为新版本兼容。保留旧包和状态，按当前定义独立导出和会话装配；以后业务变更字段/约束/频率需管理新Schema/计算版本。

## 部署迁移

按PF2026101203_SHA256.txt同步运行 `.py` **及两份内置JSON配置**。doc只用于本地交接，无须上传；验收入口使用当前内置源码/配置摘要。
移除运行机下列四个退役文件（本地已经删除）：

```text
bomber/framework/datahub/features/index.py
bomber/framework/dataprep/features/index.py
bomber/framework/market/stream/dolphin/index.py
bomber/framework/trader/runtime/live/channels/index.py
```

入口会列残留路径并在导入前失败，防止旧指数专用接入继续被误用。tests/test_dolphindb_index_features.py与test_index_query_feed.py仍是指数消费场景测试名称，不是生产特征Provider。
旧index_readonly.py为默认数据集/业务键兼容壳，实际读取统一由read_only.py实现。

## Linux 验收

```bash
python -u -m tests.run_feature_datahub
python -u -m tests.run_cross_section_acceptance \
  --only feature-query-live --only option-vega --only option-vega-live --only live-common
```

当前已收到全部所列离线组通过结果：features35、definitions10、index17、legacy14及D0、entry9、Bar场景9、option-vega33、08装配12、live-common113。Bar组包含不同业务键、重命名字段、五分钟Bar共用同一个Feed/Channel及DFS CLI；公共恢复新增可选查询钩子用例随live-common通过。数据库/柜台使用夹具，不能替代真实联网或Recording结果。
definitions组验证同一通用Provider承载多特征、不可变严格类型、约束/频率指纹、JSON拒绝可执行配置、多频率DataHub组合查询、源修订/质量、重装Schema文件往返、文件与MongoDB配置及日历注入。数据库为夹具。

真实指数只读探针（凭据沿用项目.env）：

```bash
python -u -m scripts.integration.features.read_only --connect --inspect \
  --dataset index.minute --key 000852
```

实际类型和000852样例仍待返回。区间读取另加明确的含时区--start/--end，--export新CSV/Parquet路径会逐记录核对文件往返。
接业务新特征只需指定--config业务JSON --dataset业务ID --key业务键；MongoDB CLI从MONGODB_URI注入连接。默认index_readonly旧命令仍可执行。

08沿用原真实日期/日历和CTP参数，指数段可用：

```bash
--index-source dolphindb-dfs \
--index-feature-config bomber/framework/dataprep/features/configs/features.json \
--index-dataset index.minute --index-schema-version 1 \
--index-poll-seconds 0.2 --index-query-timeout 2
```

默认标签/可见性沿用配置；CLI可显式覆盖数据库、表、标签与可见性。先Recording验证真实帧与超时跳过，再分别验证模型/柜台执行。用户旧33项option-vega通过证据不代替当前版本和实际来源验收。
