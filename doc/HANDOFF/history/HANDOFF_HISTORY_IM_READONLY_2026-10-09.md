# PF2026100912 IM固定合约原始分钟只读验证

用户确认minTime为分钟开始，先验证固定合约原始行情，再接fut_adjustment_factors。源码已完成，远程测试/真实库查询待验证；本机未执行Python、连接库或CTP。此增量不宣称CFFEX交易入口或连续股指日历已经接通，GAP04保持开放。

## 数据契约

实际配置`demos/01_main_ema/history_im_raw.json`：dfs://min_bar/cffex_1min，Code保留IM2611大小写，date与minTime组合北京时间，OHLC原始字段。查询使用[官方concatDateTime](https://docs.dolphindb.com/en/Functions/c/concatDateTime.html)和nanotimestamp转纳秒；北京时间墙钟减8小时后得到UTC，分钟开始加59999999999ns得到完整分钟末。按日期分区、真实合约、精确UTC区间限定查询，多读一行检测截断。官方函数支持DATE与SECOND/TIME/NANOTIME；源Schema若不是这些类型须按实际类型调整，不能只看截图就认定。

单ts_event列配置继续兼容，默认源时区UTC。新组合配置须明确date_column/time_column、source_timezone=Asia/Shanghai、time_unit=ns、time_label=start，不能同时提供ts_event映射。暂只支持UTC/Asia/Shanghai，不猜测其他时区或分钟结束标签。

纳秒请求边界、分钟偏移和时区偏移均使用显式LONG字面量后缀l，按[官方数据类型](https://docs.dolphindb.com/en/Programming/DataTypesandStructures/DataTypes/DataTypes.html)避免默认INT溢出。这也覆盖既有单时间戳查询生成，返回契约不变。

原始close不升级为adjusted_close，不设factor=1，不预热主力连续EMA，不认定IM2611为主力。源volume/turnover是累计口径，当前配置不映射到标准分钟量；后续接入时须查前序基准并差分。此轮只核验原始OHLC、合约和完整分钟时间/条数，未验证成交量、历史修订as-of、主力跨月、实时追赶或柜台订单。

## 同步及离线复测

同步PF2026100912_SHA256.txt所列完整文件集，在远程pro根目录执行：

```bash
sha256sum -c doc/evidence/checksums/PF2026100912_SHA256.txt &&
python -u -m tests.run_main_ema_acceptance \
  --only history-provider --only gap03 --only continuous --only entry
```

history-provider现在41项：此前33项加8项组合日期/时间、UTC/北京时间、Schema拒绝、原始探针与资源关闭、CLI授权/时间和报告碰撞回归。PF0911历史通过证据不能当作本次新增源码通过；本轮未改CTP/归属/恢复实现，其其他已通过模块不重复。

## 实际DolphinDB只读验证

先查用户截图已展示的固定IM2611，2026-10-09 09:30至09:40，共10根已结束分钟。连接复用.env的DDB_HOST/PORT/USERNAME/PASSWORD和显式超时；不连接CTP，不写策略状态、不发布目标。报告路径不存在才能写入，避免覆盖证据。

```bash
IM_RAW_REPORT="tests/results/im2611-raw-$(date +%s%N)-$$.json"
python -u -m scripts.integration.history.readonly \
  --connect --config demos/01_main_ema/history_im_raw.json \
  --instrument IM2611.CFFEX \
  --start 2026-10-09T09:30:00+08:00 \
  --end 2026-10-09T09:40:00+08:00 \
  --expected-bars 10 --timeout 15 --report "$IM_RAW_REPORT"
```

预期结果：status=passed、bars=10、price_basis=raw、ema_warmed=false、orders_submitted=0。首根ts_event=1791509459999999999（北京时间09:30:59.999999999），末根=1791509999999999999（09:39:59.999999999）；minute_start以UTC显示首根01:30、末根01:39。第一根OHLC应与截图同源行一致（7059）；若源发生修订则核对真实当前源，不强制复刻截图值。

如果实际返回0行/数量不符、类型错误或查询失败，保留原输出并核对Schema。不能通过allow参数绕过此原始数据验证；探针无该参数。后续再按实际历史因子表接EMA复权，结合CFFEX股指日历和入口扩展验证，不将这个只读配置直接当作已可运行的股指run_live命令。
