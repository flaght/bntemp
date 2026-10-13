# PF2026100916 正式IM Recording历史与实时分钟接续

2026-10-13迁移说明：本文旧版本引用的demo日历JSON已退役；历史命令和摘要保留原貌，仅用于追溯。当前启动使用交易日历／时段／日期例外Provider，CU停夜盘数据已迁移；配置与同步步骤见[01当前说明](../../../demos/01_main_ema/README.md)。

用户授权继续正式run_live。已有CU历史加载、Tick缓冲、恢复及柜台权威对账在生产生命周期中；股指此前受交易所及历史日历限制。本轮增量开放IM/CFFEX的realtime Recording单次观察；CFFEX报单、持续运行和交易状态恢复未开放，不能将此前指标证据当正式交易检查点。暂缓的主力/因子覆盖偏差不改。

本机仅源码、文档、静态摘要，不运行Python/测试/数据库或CTP。新增6项im-runtime离线用例，远端待验证；实际Recording待会话资料和行情核验后执行。

## 首轮远端反馈及窄改

用户im-runtime输出Ran56、errors2：两项正式生命周期用例均在assemble预期合约核对失败，未进入历史加载。原日志未显示actual，不推定已证实远端漏同步或具体大小写原因。本地live_role.py已按CFFEX大写形成InstrumentId，IM2610固定夹具预期大写一致；须先核对远端该公共文件及实际加载位置，不放宽严格身份比较。

已修复测试模块直接导入LiveTests导致unittest连带收集旧50项的问题，改为模块引用，预期只运行6项。夹具立即核对实际IM标识，失败打印参考模块文件路径、spec.symbol/venue；装配失败打印expected/actual、symbol/venue及参考类来源。不改变参考选择、因子覆盖或交易闸门。重新同步新清单及公共live_role.py、run_live.py、测试文件；先sha256sum -c全清单再--only im-runtime。若仍失败，以新字段/路径与摘要为依据定位，不凭原日志猜根因。当前没有通过证据，本机仅静态校验。

第二轮远端目录main-ema-acceptance-1791550696794604263：仅6项已正常收集，3失败均在夹具身份断言，实际im2610.CFFEX、spec.symbol='IM2610'/venue='CFFEX'，参考模块路径为远程pro/bomber/framework/dataprep/live_role.py；未进入正式生命周期。本地该文件SHA256=719aeaa0dca8e2b2fc9060fa52b6fc34965d8041c9074be32c107162f91a305c，源码条件明确CFFEX大写。检查本地Bomber InstrumentId.from_str/FFI源码未发现小写转换；远端文件摘要尚未提供，不能将部署差异当已证实根因。先单文件摘要核对/同步公共live_role.py后复测；若摘要匹配仍失败再核对远端实际property与引擎加载，不放宽身份或转换历史ID。当前无源码再修改、无本机执行。

第三轮用户反馈im-runtime6项全部OK，测试0.094秒、模块exit_code0/1.228秒，集中目录tests/results/main-ema-acceptance-1791550921750786768。正式Recording生命周期历史预热/首新分钟及缺口闭闸、合约核验、日历用例均通过；本反馈未显示单文件SHA输出，不补造摘要核验证据。已通过im-runtime不重复；PF0916所列history-provider/continuous/entry/assembly/references/role-signal兼容组结果尚未在此反馈中提供。实际在线Recording仍待本次TD/原始MD/预期合约/日历及历史因子配置核验；不将假柜台用例当在线验收。仅记录证据及文档摘要，源码不变，无本机执行。

随后用户确认这是所要求兼容回归的最后日志：entry50项OK、测试1.698秒、模块exit_code0/2.812秒，入口明确“全部选定离线模块通过”。登记本次选定兼容回归整体通过，不要求逐组重贴或重复；其他组具体条数/耗时和本轮报告路径未展示，不补造。结合im-runtime6项通过，PF0916离线阶段完成，下一步实际柜台只读/IM原始行情及在线Recording。源码未变，仅文档证据/摘要；在线验收与CFFEX报单仍未通过。

## 变更

- runtime.trading_sessions增加IndexSessions和load_sessions工厂。IM按显式日历日期09:30—11:30/13:00—15:00枚举分钟、排除午休/休市；未知日期或此前分钟超出覆盖范围失败，不套用CU日夜盘。历史日历跨已声明交易日回退，盘前240可读取前一日。无自动节假日推断。
- cffex_im_20260921_22.json仅覆盖已核验历史日，供离线用例和格式示例；不是当前会话的全年日历，不能直接用于20261009或之后运行。实际部署须提供当前及前一交易日的完整日历覆盖，并同步相应factor_source_days，不能把此前单日history_im_20260922.json用于当前会话。
- run_live的prepare_inputs/assemble允许IM Recording使用CFFEX；IM须--expected-instrument声明本次固定真实合约，在参考准备和装配时核对，不自动改选另一个合约。不将main一致性偏差判为已解决。
- live_role公共参考InstrumentId按CFFEX大写、其他原小写形成，匹配已验证原始IM行情/历史ID。策略比较角色时仍大小写归一；不修改角色/因子覆盖规则。
- 历史开关允许CU/IM并校验对应日历，正式历史服务与CheckpointedRunner继续按既有工厂/因子处理、缓冲完整分钟、历史只推进指标；柜台新会话空账户及活动订单预检、MD/TD日和健康核验保持必需。未把允许历史缺失当允许账户对账缺失。

## 离线验收

同步PF0916清单，旧清单不覆盖改动源码：

```bash
sha256sum -c doc/evidence/checksums/PF2026100916_SHA256.txt &&
python -u -m tests.run_main_ema_acceptance \
  --only im-runtime --only history-provider --only continuous --only entry \
  --only assembly --only references --only role-signal
```

im-runtime6项：日历工厂/开闭盘/午休/此前日期/240窗口；未知日期/不足覆盖拒绝；CLI预期合约及正确日历；正式MainEmaSessionLifecycle.start→历史2根预热无目标→Tick生成首新完整Bar记录1目标/订单0；装配拒绝IM报单或错误预期合约；历史严格缺口保持Bar闸门关闭且未部分预热。使用假TD/MD和合成角色条款，不冒充真实数据库/行情/主力一致性；历史真实服务及重启通过证据仍另保留。

## 实际Recording前的资料

下一步先柜台只读核对实际TD、全账户仓位及活动订单；已有未知仓位/订单时本次全新Recording入口拒绝，不强行分配给新EMA。核对当前选定IM真实合约的原始MD日期和行情；声明同一expected-instrument，以实际参考适配选择结果为准，不依据旧报价/旧日期下命令。

还需当前IM日历覆盖和原始分钟对应事件日期的显式factor_source_days。日历格式示例：

```json
{
  "product": "IM", "timezone": "Asia/Shanghai",
  "start": "2026-09-21", "end": "2026-09-22",
  "trading_days": ["2026-09-21", "2026-09-22"]
}
```

start/end区间内trading_days应完整列出权威交易日，未列日期按休市处理；覆盖区间外拒绝。此前日期因子必须在history-config中逐日明确声明，现有工厂不会把当前因子无条件用于历史。

资料核验后执行模板（变量必须按本次实际会话填入，不预设下一TD日期）：

```bash
python -u -m demos.01_main_ema.run_live \
  --connect --mode recording --simnow-environment realtime \
  --product IM --expected-instrument "$IM_INSTRUMENT" \
  --reference-source dolphindb \
  --factor-date-basis source --factor-availability observed-on-read \
  --expected-trading-day "$IM_TD_DAY" --expected-source-day "$IM_SOURCE_DAY" \
  --trading-calendar "$IM_CALENDAR_FILE" \
  --history-config "$IM_HISTORY_CONFIG" --history-minutes 240 \
  --history-stage auto --history-missing-policy allow \
  --fast 3 --slow 5 --quantity 1 --seconds 600
```

盘前优先文件可另加--history-file；盘中recovery直接数据库。allow仍报告缺口，不补造分钟，错合约/因子/健康异常仍拒绝。预期正式summary历史审计、EMA分钟数/数值及Recording目标，orders_submitted0、回调0；参数无enable-orders/confirm-simnow/state-file。尚无实际运行结果，不关闭GAP04或宣称正式IM恢复报单完成。
