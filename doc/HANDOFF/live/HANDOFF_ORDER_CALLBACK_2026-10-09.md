# PF2026100903 统一订单回调入口

日期：2026-10-09。用户要求 01 EMA 展示完整订单／成交回调，并以常用 `on_order` 命名。修改公共策略模板前已在当前对话通知接口与兼容行为。状态：新版五组回归及当前CU接管Recording通过，待实际订单／成交回调验收；本机没有运行 Python、测试、回测或编译。项目无 Git 元数据，不虚构提交号。

## 接口与兼容

统一模板分派订单事件至 `on_order(OrderUpdateEvent)`。默认实现转发旧 `on_order_update`，已有仅重写旧方法的策略保持行为；新策略推荐重写 on_order。同时重写两者时，框架只调用 on_order，不分别自动调用两遍。子类需要父类观察行为时显式调用 `super().on_order(event)`，与其他策略生命周期回调一致。

订单状态、账户仓位、在途量及策略归属先更新，再调用订单回调，随后才是该次逐笔成交回调 `on_fill(FillEvent)`。本次不修改归属账本、状态机、回报去重、交易闸门或授权范围。OrderUpdateEvent 的数量及累计成交仍是账户母订单事实，多策略归属由冻结分配计划提供；不能在 on_order 中按累计成交再次记仓。on_fill 继续提供本次分配的实际成交增量。

## 01 EMA 示例与报告

MainEmaStrategy 新增 on_order，保存 `order_updates_received`、`last_order_update`、`last_order_position`；on_fill 保留成交次数和回调仓位，并补全事件类型标注。两个回调仅观察，不修改 EMA 公式或额外提交目标。

在线 summary.json 新增订单回调次数、最后状态、客户端订单 ID、原因及回调内归属仓位。Recording 不产生真实订单回调时，计数 0、最后状态空是正常结果；显式接管后的策略归属仍应核验为账户实际仓位。

## 修改文件和验证

部署范围以 [PF2026100903_SHA256.txt](../../evidence/checksums/PF2026100903_SHA256.txt) 为准，包含公共模板、01 策略／在线报告、README 和受影响测试。必须同步模板与策略，不能仅更新 demo。历史真实引擎探针改为重写 on_order 并调用父类，接口预检增加三个订单观察字段；集中验收纳入 template.py 的来源摘要。

扩展的用例覆盖新入口、旧入口、两者同时定义时的唯一通知，受理／部分成交／撤单／拒单、重复成交、事件隔离及状态先更新；现有当前 01 回测装配、真实引擎及 CTP 接管反手增加订单回调归属检查。本机仅阅读核对、空白检查和文件摘要，尚未得到本轮运行结果。

在远程项目根目录同步后执行：

```bash
sha256sum -c doc/evidence/checksums/PF2026100903_SHA256.txt
python -u -m tests.run_main_ema_acceptance --only attribution --only dispatch --only historical --only assembly --only entry
```

此前 PF2026100902 的 11 组离线通过仍是该版本的有效历史证据，不能代替本次修改后的五组回归。state／autosave／recovery／controlled／routing／ctp 未改动，其既有结果保留；仅在新失败或新改动需要时扩大复测。PF2026100902_MAIN_EMA.patch 和 PF2026100902_SYNC_ASSEMBLY.py 是上一轮部署的历史修复工具，不用于更新本轮订单回调版本。

GAP-01 当前完整 Recording 和实际成交仍待验收；报告 simnow-1791506768279184908 在盘前启动健康未就绪，不因本次补充回调而自动修复或视为通过。五组回归通过后，在有效交易时段沿用已确认的接管 Recording 配置继续。本次没有联网、发单或向其他工作对话发送消息；跨任务待同步。

## 首轮远程反馈：历史探针仍为旧入口

用户提供 historical 在 `len(strategy.order_events) >= len(strategy.fill_observations)` 处失败，退出 1、耗时 1.243 秒。回溯该断言位于第 499 行、文件入口位于第 548 行，与 PF0902 历史测试一致；本机 PF0903 对应位置为 502／554。结合新主策略 on_order 优先于子类旧 on_order_update 的既定规则，判断历史测试探针未同步：父策略观察到订单，但旧探针没有收到其所重写入口的通知。未提供实际远程摘要，不把此判断写成逐字节部署证据。

本机新版 _ObservedEma 已重写 on_order，先 `super().on_order(event)` 再追加探针列表，并检查父策略计数及观察结果。当前源码未再修改；只同步 `tests/run_unified_historical_runtime.py`，其 SHA-256 应为 `7f04750125be9e316bc04459bc6fcc4014f1606318b1185b74a67968390f5e0d`，校验后仅复测 historical。不得删除订单数量断言或让框架重复投递两个入口来掩盖探针版本差异。本轮未提供 attribution／dispatch 的新版通过结果，不补记；assembly／entry 因集中入口停止，未取得本轮结果。

## 后续远程摘要确认与直接补丁

用户随后提供实际远程摘要 `c1c93f0d005adf02b1638af092042e62b2e39d75a5c53934a06372b129593676` 及旧探针源码，确认远程确为 PF0902 历史测试，不再仅依据行号推断。此前新摘要检查失败通过 `&&` 阻止测试执行，未改写文件，未产生新测试结果。

已生成 [PF2026100903_HISTORICAL.patch](../../evidence/patches/PF2026100903_HISTORICAL.patch)，完整包含订单接口预检、on_order 探针及三条新增观察断言。以本机当前源码反向重建旧文本，旧摘要精确匹配用户观测；在临时文件应用补丁后，最终摘要为上述新版值，cmp 与当前交付源码逐字节一致。这里只是文本补丁核验，没有执行 Python、测试或编译。

远程可直接应用补丁：先校验实际旧摘要并备份，再 patch --batch --fuzz=0 -p0，最后校验新版摘要。任一检查失败就停止，避免部分部署被误认为通过。仅复测 historical，公共模板和本机历史测试实现均无需再次修改；新版五组验收继续待证据。

## historical 远程成功反馈

用户随后确认“远程执行成功”，对应上一轮包含旧／新摘要核验、补丁应用及 historical 复测的命令。按用户确认记录本次修复和复测成功；此次没有附模块终端明细，不补造退出码、耗时、订单／成交数量或报告目录。

下一步完成新版 assembly／entry 回归。若上一轮集中命令已通过 attribution／dispatch，则保留结果不重跑；尚未取得其详细输出，需要最终汇总时核对来源与模块记录。当前不将 historical 的成功扩展为五组全部通过，完整当前 Recording 和 SimNow 实际成交仍待验收。本轮只更新交接，无源码修改或本机运行。

## assembly／entry 新版回归通过

用户在收到 `--only assembly --only entry` 命令后提供末段结果：entry 45 个测试全部 OK，测试耗时 1.543 秒，模块退出 0、模块耗时 2.641 秒；集中入口总结全部选定模块通过。结合此次两组选择，记录 assembly／entry 本轮通过；未提供 assembly 的具体数量／耗时和集中报告目录，不填入推测数据。

当前 PF0903 historical 已有用户成功确认，assembly／entry 有本轮通过总结，attribution／dispatch 新版结果已向用户询问核对，不能用接口修改前的结果代替。Recording 为不发单阶段，可在有效交易时段按 GAP01 交接第14节的显式 CU 接管配置继续；受控成交前核齐新版五组结果和本次 Recording。日志中的20260922／fake前置是测试夹具，不是当前柜台在线证据。本轮只更新记录，不修改源码或本机运行。

用户随后明确回复“新版两组均已通过”，确认 PF0903 attribution／dispatch 的修改后结果。现记录 attribution／dispatch／historical／assembly／entry 五组新版回归全部通过。此回复未附前两组具体耗时、数量或报告目录，不补造明细。后续只继续有效交易时段的当前 CU 接管 Recording 和实际成交，已通过组不重复运行。

当前Recording报告simnow-1791508384404801230已取得9根Bar及LONG目标，接管归属与账户均-1、未归属0，报单0、订单／成交回调0、最终passed。这证明当前观察字段及接管归属输出正确，不代表真实订单事件已发生。下一步沿用既有一手空仓接管／两笔反手授权验真实回调及最终仓位，详见GAP01交接第17节；本轮无源码变化。
