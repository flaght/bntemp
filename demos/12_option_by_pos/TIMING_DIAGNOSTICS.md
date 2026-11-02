本次先查成交时间；价格留到时间链路明确后再查。

**目前的证据**

北京时间 2026-05-29，IM2606 有两笔减仓、一笔加仓：

| 动作 | 新版目标/订单创建 | 新版成交 | 旧版成交 |
| --- | --- | --- | --- |
| 卖出 2 手减仓 | 10:00 | 10:01 | 10:05 |
| 卖出 2 手减仓 | 11:00 | 11:01 | 11:05 |
| 买入 1 手加仓 | 13:30 | 13:31 | 13:35 |

新版 orders.csv 的 ts_init 确认了创建时刻。旧版成交中的订单 ID 也包含
10:05、11:05、13:35，但 ID 仅作线索，实际创建时刻还需订单事件或现场追踪确认。

旧版 bomber_adapter/bridge.py 的 on_bar 先判断新时间戳，再调用
_flush_bar_batch 处理上一时间戳的全部 Bar。用户 onBatchBar 收到的逻辑时间仍是上一批的时间，
而引擎时钟已经走到新事件的时间。因此不能只打印 onBatchBar 的 ts 来判断订单何时提交。

已用旧版真实 Python 方法、轻量 SDK 替身复现：

- 10:00 之后下一条行情在 10:01 到达：10:00 批次在 10:01 创建订单。
- 10:00 之后下一条行情直到 10:05 才到达：10:00 批次在 10:05 创建订单。

这验证了机制，尚未证明原回测当天为什么没有更早的事件触发。需区分源数据缺失、加载过滤、
订阅范围、频率/标签配置及延迟队列。测试不模拟原生成交，也不验证历史价格。

**先记录完整时间链路**

给同一笔目标关联以下信息，查出两版第一次出现差异的位置：

1. 输入目标的逻辑时间、目标仓位与调整前仓位。
2. 行情的合约、周期、ts_event（Bar 时间）、ts_init（可用时间）。
3. 用户回调开始时的模拟时钟、当前批次时间和触发下一批的合约。
4. 创建订单的模拟时刻、client_order_id，以及是否进入等待队列。
5. 放行订单的行情时间、成交回报的事件时间。

要同时观察所有合约的事件流：期权 M5 或另一张期货的 M1 都可能触发旧版批次结束。
只跟踪 IM2606 会漏掉触发源。

已提供 trace_timing.py，使用标准库 sys.setprofile 包装原入口，在 Python 函数调用边界读取
这些信息，不修改策略目标、行情或撮合参数，不记录价格。它会增加诊断运行的耗时。

在原运行目录、原 Python 环境执行下面的形式。ENTRY 换成实际入口；`--` 后填写原命令的
全部原参数，尖括号部分是说明，不能原样执行：

```text
python /实际工程/pro/demos/12_option_by_pos/trace_timing.py \
  --entry /实际运行的/ENTRY.py \
  --output /tmp/old_timing_20260529.jsonl \
  --focus-day 2026-05-29 \
  --focus-instrument IM2606 \
  -- <全部原回测参数>
```

新版再运行一次，入口换为原 run_backtest.py，日志文件换为
/tmp/new_timing_20260529.jsonl。日志采用独占创建，已有文件不会覆盖。
默认只记录 09:58–10:06、10:58–11:06、13:28–13:36；可重复指定
`--window HH:MM-HH:MM` 覆盖窗口。日志中的 source 是实际加载的源码路径。

保留原回测起点、目标文件、数据与执行配置。不要从 5 月 29 日空仓启动：之前的仓位、
行情缓存和等待队列会影响结果。若入口再启动子进程，此追踪器仅覆盖当前主线程，需在实际
回放进程的入口运行；已有 Python profiler 时应换独立诊断进程。

重点筛选：

- 旧版 `_flush_bar_batch` 的 call：batch_ns_bj 是逻辑批次时间，clock_bj 是实际处理时间，
  trigger_instrument 是触发合约，last_focus_bar_ns_bj 是更新前最后收到的 IM2606 Bar 时间。
- 旧版 `_on_send_target_vol`：signal_ns_bj 与 clock_bj 比较。
- 旧版 `_submit_target` 的 return：order_init_ns_bj 与 client_order_id。
- 新版 enqueue / _prepare：request_ns_bj、after_ts、pending 中的 after_ns、remaining_bars。
- 新版 _release_orders 和两版 on_order_filled：何时放行，何时回报成交。
- 行情行：ts_event_bj 与 ts_init_bj 是否一致，实际 bar_type 是否为预期周期。

判断顺序如下：

| 第一处分歧 | 下一步检查 |
| --- | --- |
| 目标时间已经不同 | positions 原文件、时间槽、起点/终点标签和时区 |
| 相同目标，回调处理时间不同 | 批次结束机制、实际全局事件流 |
| 回调相同，创建或提交时间不同 | execution_delay_bars、延迟队列、目标汇总 |
| 提交相同，成交时间不同 | 本合约后续行情、订单放行、原生撮合与事件顺序 |

若旧版记录 `batch=10:00, clock=10:05`，且 10:01–10:04 没有 on_bar，说明这些事件没有到达桥接层。
再核对实际源文件及加载后的时间戳：原始存在、加载后不存在，则检查日期/合约过滤、重采样与解析；
加载后存在、桥接层不存在，则检查 add_data 和订阅。源文件路径以实际旧版 DataLoader 为准，
不能假设旧版也读取新版按日 Feather。

无需停住程序的另一种办法是 IDE Logpoint：在同样的位置打印上述字段，然后自动继续。
结构化日志便于两版对照；Logpoint 适合临时查看少量变量。

**条件断点的用法**

用原入口和全部原参数启动：

```text
python -m pdb /实际运行的/ENTRY.py <全部原参数>
```

旧版先在 run.py 的 runner.run(strategy) 前暂停。当前仓库是第 285 行，确认现场版本后调整。
暂停后检查实际导入路径：

```text
p __import__('bomber_adapter.bridge', fromlist=['BomberBridge']).__file__
```

以下路径换成上一步输出的实际 bridge.py 路径。当前仓库第 172 行是
`self._flush_bar_batch()`，第 583 行是创建订单。条件只覆盖第一笔目标，避免每根 Bar 都停。

```text
b /实际路径/bridge.py:172, self._batch_ts == 1780020000000000000
b /实际路径/bridge.py:583, code == 'IM2606' and 1780019940000000000 <= self.clock.timestamp_ns() <= 1780020360000000000
c
```

在第 172 行暂停时，检查：

```text
p (str(inst_id), ts, self._batch_ts, self.clock.timestamp_ns())
p self._last_bar_ts.get('IM2606.CFFEX')
w
```

含义依次是：当前触发合约、当前事件时间、被处理的旧批次时间、模拟时钟。
这里 last_bar_ts 在当前 Bar 更新前读取，应结合当前触发合约理解。

可定义北京时间显示别名，避免看大整数：

```text
alias bj p __import__('pandas').Timestamp(%1, unit='ns', tz='UTC').tz_convert('Asia/Shanghai')
bj ts
bj self._batch_ts
bj self.clock.timestamp_ns()
c
```

到第 583 行时：

```text
p (code, target_vol, current_qty, delta, self._active_bar_ts)
n
p (str(order.client_order_id), order.ts_init)
bj order.ts_init
```

第 583 行执行前 order 尚未创建，必须先 n 才能检查 order。
如果在第 172 行看到 batch=10:00、clock=10:05，并在第 583 行仍是 clock=10:05，
就确认延迟已经发生在创建订单之前；之后再查是哪条行情推动了时钟。

新版可在 strategy.py 第 153 行 set_targets 设置条件
`signal_ns == 1780020000000000000`，检查 signal_ns、observed_ns、goals；
在 gateway.py 第 156 行设置条件
`str(instrument_id) == 'IM2606.CFFEX' and 1780020000000000000 <= ts_event <= 1780020360000000000`，
检查 item.after_ts、item.remaining_bars、item.last_market_ts。该行在 for 循环体内，item 已存在。
再在 on_order_filled 检查 event.client_order_id 和 event.ts_event。

常用 pdb 命令：`c` 运行到下一断点；`n` 执行当前行但不进入函数；`s` 进入 Python 函数；
`w` 查看调用栈；`u/d` 切换栈帧；`p` 查看变量；`b` 列断点；`disable 编号` 暂停断点；
`cl 编号` 删除断点。IDE 的条件断点使用相同表达式，Logpoint 只打印并继续。

注意：

- 使用模拟时钟 self.clock.timestamp_ns()，不用 datetime.now()。
- 桥接层和新版是纳秒；旧版用户 onBatchBar 的 ts 是微秒，10:00 对应 1780020000000000。
- 条件中的变量必须在断点行所属栈帧且已经赋值；不要在函数入口使用尚未创建的局部变量。
- 普通 pdb 无法逐行进入未启用调试追踪的 Cython/Rust 撮合代码，优先看 Python 提交与成交回调边界。
- 此阶段不调整撮合延迟、数据周期或填补缺失行情，先保存原行为的证据。

验证：`python -m unittest tests.test_option_timing_trace tests.test_simulation_market_timing -v`。
