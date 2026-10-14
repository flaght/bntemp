# 08共享执行接入与启动跨日恢复

本次实现用户要求的08接入共享账户服务，以及昨日归属仓位恢复后继续管理。
执行基础复用GAP10：应用工厂新增option_vega，支持netted／independent，公共框架
不导入demo算法。08仍接入DolphinDB交易标的一分钟流和index.minute指数特征。

`scripts.integration.options.shared` 为日盘正常恢复入口：读取原账本，禁单观察TD日，
核验后启动公共共享服务与08，运行至15:00或主动停止。原策略参数不变，原空一手
归属保留；首次原生文件转换和以后跨日更新均备份原代再提交。恢复期间禁保存，
任何账户／资金／活动订单／总仓／成本／条款冲突保持原文件，不自动清仓。

共享宿主和worker补指数查询／分钟封帧控制，保存08选约去重状态，同日不重复选约。
次日不重放旧目标，新的选约时点仍可运行；清除价格、IV及旧分钟缓存。候选链
零仓合约允许扩展／移除，非零持仓及目标路由不得移除。真实成交关联与去重不清空。

边界：仅realtime、向前TD日、原订单终态、未知活动订单为零、权威总仓一致。
独立执行跨日支持CFFEX通用平仓；SHFE等逐策略今昨归属未增加，仍拒绝独立昨仓。
不是跨夜常驻、到期自动换约、账户归属重建或多份检查点合并；固定策略集合保持。
旧08导入要求保留原strategy_id及CTP client_id。09:29—14:56可启动，午休继续维护
账户、下午等待新鲜分钟；启动等待最多180秒，不能在午休中段等待到下午开盘。

回归：08基础81项、专项15项、原迁移12项、原预检9项通过；共享账户65项、
本机socket/进程10项、公共Live116项通过。使用真实公共组件及替身柜台／行情；
08本地SDK实体使用既有离线shim。状态／CTP恢复另有6条公共路径通过SDK实体shim
执行；原生state/recovery直接入口在本机因缺fixes.Bar不能加载，Linux须补跑。
不能将本地替身验证登记为原生柜台通过。

Linux同步后一次集中检查：

```bash
sha256sum -c doc/evidence/checksums/OPTION_VEGA_SHARED_ROLLOVER_20261014_SHA256.txt
python -u -m tests.run_main_ema_acceptance \
  --only option-shared --only shared-account --only shared-ipc --only live-common \
  --only state --only recovery
```

日盘启动命令和完整边界见demos/08_option_vega/README.md开头的正常启动节。
本机未连接数据库、MD或TD，未发实际订单。服务器仍须核对原仓恢复、DolphinDB
同分钟决策、当前日期选约、停止后的活动订单及归属；不重复之前的原始行情探针。
