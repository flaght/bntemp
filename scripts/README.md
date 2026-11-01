# 脚本目录职责

本目录放置可独立运行的工程工具和通道联调入口，按用途划分。工程维护工具负责源码同步与发布准备，通道联调入口负责验证交易系统连接、账户查询和执行链路。

```text
scripts/
├── sync-bomber-framework.py
├── README.md
└── integration/
    ├── README.md
    ├── ctp/
    │   ├── README.md
    │   └── td_readonly.py
    └── binance/
        └── README.md
```

| 位置 | 职责 | 当前内容 |
| --- | --- | --- |
| scripts 根目录 | 工程维护与框架发布准备 | `sync-bomber-framework.py` 将 `bomber/framework` 原样同步到目标项目，保留构建资源并检查旧导入；默认只报告差异，`--apply` 构建暂存副本 |
| [integration](integration/README.md) | 独立于策略的通道接入与联调 | 组织 CTP、Binance 的连接、查询、执行验证及恢复联调入口 |
| [integration/ctp](integration/ctp/README.md) | CTP 行情与交易通道联调 | 当前已有 TD 只读入口：登录、结算确认、仓位／资金／活动订单查询 |
| [integration/binance](integration/binance/README.md) | Binance 通道联调 | 当前已建立归属说明，入口尚未迁入 |

`scripts/integration` 编排现有公共能力；连接、规划、风控、回报与恢复实现归 `trader`，行情实现归 `market`。具体策略及回测／实盘装配归 `demos`，自动断言与回归测试归 `tests`。新代码不依赖旧 `examples` 入口；迁移期间旧入口可以转发到新实现。

发布工具与柜台联调分别运行。执行 `sync-bomber-framework.py` 不代表连接柜台；运行 `integration` 入口也不承担框架打包或发布。

## 期权 Parquet 转标准 Feather

`convert_option_parquet.py` 独立运行，只需要 pandas、numpy 和 pyarrow。将品种级
Parquet 按交易日、合约拆为 `<output-dir>/YYYYMMDD/合约_YYYYMMDD.feather`：

```bash
python scripts/convert_option_parquet.py \
  --input /work/backTest/data/market_data/options/5min/MO.parquet \
  --output-dir /work/backTest/data/kline/opt
```

输入列为 `trade_date,code,timestamp,open,high,low,close,volume,open_interest`；
存在 `act_time` 时核对它与 timestamp 的北京时间一致。输出列为
`symbol,trade_date,datetime,open,high,low,close,volume,open_interest`，其中
trade_date 为 YYYY-MM-DD 字符串，datetime 为带 Asia/Shanghai 时区的纳秒时间。
零价格、零成交量和原 open_interest 保留；不补价格、不重采样、不生成缺失日期或合约。
open_interest 只做数值解析，保留负数、空值和无穷值；转换时打印各类数量及最多五条样本，
不因该辅助字段异常删除 Bar，也不擅自填零或解释为累计持仓。OHLC 和 volume 仍校验有限非负数。
高低价未包住开收盘价的行默认原样写入，打印 `invalid_ohlc_rows` 数量及最多五条样本；
不自动扩大高低价或删除记录。需要在转换阶段拒绝此类行时加 `--strict-ohlc`。
当前公共执行输入默认要求价格为正，零价格数据的执行用途仍需另行明确，转换不会擅自删除它们。
公共执行输入仍会检查 OHLC 大小关系，格式转换完成不等于这些异常行已符合撮合要求。

默认源 timestamp 是 M5 **结束时间**，原样保存；仅当源数据确定采用开始时间时使用
`--timestamp-label start`，向后移五分钟。可用 `--start-day/--end-day` 选择日期，
`--symbol` 可重复选择合约。重复合约时间、无效数值或时间不一致时失败；已有目标文件默认
拒绝覆盖，需要显式 `--overwrite`。每个文件先在同目录暂存再原子发布。一次读取所需列，
不会将数据分成 M1；导入回测时仍应显式指定 `interval_seconds=300` 和 `5-MINUTE`。
