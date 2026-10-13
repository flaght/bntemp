# 分钟行情导出

`kline_task.py` 支持 `--kind future / option / index`。按日期范围逐自然日查询，没有数据的日期跳过，不要求指定品种，也不依赖 ultron 交易日历。数据库固定为 `dfs://min_bar`；future/option 使用 `cffex_1min`，index 使用 `index_1min`。只需指定 `--kind`，不再提供 `--database`、`--table` 参数。

## 导出期货

在 pro 根目录执行，连接信息沿用 `.env` 中 DDB_HOST、DDB_PORT、DDB_USER、DDB_PASSWORD：

```bash
python examples/opts2/kline_task.py \
  --kind future \
  --start-day 2026-08-13 --end-day 2026-09-30 \
  --output-dir /workspace/data/temp_bomber1/ctp/kline/fut
```

默认无需 `--fut-basic`，也不自动读取环境变量中的基本资料文件。交易所优先使用源 exchangeCD/exchange 字段或 Code 后缀；无标识时，固定的 cffex_1min 表使用 CCFX，生成 IM2609.CCFX。无基本资料时 vwap 全部为 0.0。可选传入 --fut-basic，使用资料中的合约代码大小写和交易所；此时 vwap 暂用 close 占位。

当前 future 分支只导出中金所期货 IF、IH、IC、IM、TS、TF、T、TL 的四位月份合约；混合表中的 SF702 等商品期货和含 C/P 行权价的期权记录跳过，并打印跳过的代码示例。不能因为表名为 cffex_1min 就把全部期货标成 CCFX。数据库与表名按 --kind 固定选择。

输出：`fut/YYYYMMDD/<symbol>_YYYYMMDD.feather`，例如 `fut/20260911/IM2609_20260911.feather`。覆盖已有文件必须显式加 `--overwrite`。

字段顺序与已有期货格式一致：

```text
vt_symbol symbol exchange open high low close date time datetime volume value open_interest vwap
```

- vt_symbol 为合约代码和资料交易所标识拼接，如 rb2703.XSGE、IM2609.CCFX。
- date/time 为 YYYY-MM-DD/HH:MM:SS 字符串，datetime 为本地无时区时间；由源 date+minTime 拼接，不转为 UTC。源 date 必须是该分钟实际自然日期；夜盘若源库 date 是交易日而非实际日期，应先修正源时间，代码不会猜测前一交易日。
- volume/value 为当分钟成交量/成交额；源 volume/turnover 是交易日累计值，按合约、查询日排序差分。volume、value、OHLC、open_interest 存为浮点数。
- open_interest 是当时持仓快照，不差分，缺字段会报错。
- 未提供基本资料时 vwap 为 0.0；提供资料时暂用 close 占位。均不计算真实成交均价，后续再调整。
- 不额外保存 timestamp、trade_date 或累计量字段。期权/指数继续保留各自原有字段与时区格式。

## 首条累计值

默认 `--first-row-policy error`。如果查询首条是 09:37、累计量已经 15503，缺少此前分钟，无法从这一行得知 09:37 的真实增量，不能把 15503 当作该分钟成交量。

只有确认首条累计值确实全属于第一条分钟区间时，才使用 `--first-row-policy zero-baseline`。否则应补齐更早分钟或提供上一分钟累计基准，不能通过覆盖选项自动恢复缺失分钟。现有实现遇到累计值回退也会报错。

运行 `python examples/opts2/kline_task.py --help` 查看参数。当前无 DDB 服务的测试仅验证转换和写文件路径，不代表已验证真实数据库数据。
