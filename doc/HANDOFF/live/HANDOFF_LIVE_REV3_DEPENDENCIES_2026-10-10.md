# PF2026101036 REV3：持续模式依赖与Replay聚合同步

用户远程出现两项失败：

- `continuous`导入`demos.01_main_ema.checkpoint`失败，未进入测试。
- `live-common`运行69项时，Replay接收时间Tick副本没有`aggressor_side`，1项报错。

本地存在`checkpoint.py`，但此前PF2026101036部署摘要遗漏该既有依赖；
04集中验收的SOURCES也遗漏了持续模式的检查点、日历和测试文件。本轮修正
`tests/run_cross_section_acceptance.py`，增加这四项及Tick工厂来源。
本轮没有修改生产逻辑或删除字段保持断言。

本地`aggregation.py`的ReceiveTimeTradeTickBarFeed构造副本时已经传递
`aggressor_side=tick.aggressor_side`。测试替身保留传入字段，当前本地69项公共
测试通过。远程缺字段最符合聚合文件与测试文件版本不一致的情况；尚无远程
文件摘要，不能确认具体差异。请同步下列文件并核对REV3摘要后再复测。

同步源码/依赖（只有第一项本轮修改，其他项为已有源码）：

1. `tests/run_cross_section_acceptance.py`
2. `bomber/framework/market/stream/aggregation.py`
3. `bomber/framework/market/basic/base.py`
4. `demos/01_main_ema/checkpoint.py`
5. `bomber/framework/trader/runtime/trading_sessions.py`
6. `demos/01_main_ema/shfe_cu_2026.json`
7. `tests/test_main_ema_continuous.py`

新文档为本记录，新增摘要为`../../evidence/checksums/PF2026101036_REV3_SHA256.txt`，摘要涵盖上述
七个文件及本记录。原PF2026101036和REV2摘要保留，REV3增量不会恢复旧版文件。

本地验证：69项公共回归通过；验收SOURCES存在且无重复，新增依赖均列入；
五个相关Python文件编译通过。持续模式原生测试本地无法加载Bar实体，需Linux执行。
已收到的REV2 Linux `ema-recovery`9项通过证据仍保留，不将本次导入失败混作
该恢复组失败，也不据此声明实际柜台验证通过。

```bash
sha256sum -c doc/evidence/checksums/PF2026101036_REV3_SHA256.txt
python -u -m tests.run_cross_section_acceptance --only live-common --only continuous
```

若摘要一致后仍失败，保留本次摘要输出及完整报错，继续按实际代码排查。
