# Live 公共能力集中验收排查

日期：2026-10-29。针对远端逐组失败后的集中检查；本次未连接真实数据库或柜台。

## 修复

- `tests/test_gap03.py`：参考数据库故障用例仍替换已迁移的`run_live.CtpLiveDataFeed`。改为替换当前实际调用的`create_market_feed`，继续断言数据库失败时不读取本地资料、不构建行情源。
- GAP-03中预期的参数拒绝测试收集stderr，保留`SystemExit`断言，避免正常拒绝提示淹没真实失败。
- `tests/run_main_ema_acceptance.py`：增加`--keep-going`，每组仍在独立子进程执行。失败和超时写入原有逐组日志及`summary.json`，继续后续组；整轮存在任一失败仍返回非零，不会被后续通过覆盖。默认保持首次失败即停止。
- 新增`tests/test_main_ema_acceptance.py`四项回归，验证默认停止、多失败汇总、超时后继续、指定组全部通过；已加入集中验收及源码清单。

## 检查结果及范围

原有47组逐组执行，再用已有SDK实体夹具补查可运行部分；加入验收入口自身回归后共48组。测试之间有重叠，不累计为唯一测试数量。

| 本次验证方式 | 组数 | 结果 |
| --- | --- | --- |
| 按原子命令直接运行 | 17 | 通过：acceptance-runner、trading-hours、session-readonly、execution-composition、shared-account、shared-mother-probe、shared-discovery、ctp-depth-health、calendar-history、cross-history、live-common、history-public、lifecycle-public、continuous-public、reference-regression、role-research、role-signal |
| 复用已有SDK实体导入夹具 | 13 | 通过：sessions、option-minute-diagnostics、option-shared、calendar-live、gap03、joint、attribution、td、close-existing、fields、driver、autosave、recovery。未构造替代原生EMA数值算法 |
| 本机不能完成完整组 | 18 | shared-ipc、cross-live、ema-recovery、history-adjustment、im-runtime、history-provider、continuous、dispatch、state、historical、assembly、runtime、references、controlled、health、routing、ctp、entry |

18组的限制包括缺少原生行情／回测／EMA扩展、SDK夹具接口覆盖不完整，以及本机沙箱禁止Unix socket bind。它们不记为通过；之前已有的远端通过记录仍保留在进度台账中。

GAP-03的22项全部通过；补充检查包括Calendar Live 33项、联合执行24项、持仓归属19项、期权共享72项及期权诊断／装配31项。静态扫描了测试中显式导入策略入口的`patch.object`引用，发现并修复上述GAP-03失效名称，未发现第二处同类缺失。

新增模式已通过14个可在本机完成部署检查的选定组集中运行验证。其余三个直接通过组的主体已单独运行，其在集中入口中的全局部署检查需要原生引擎，未绕过该检查。

本轮未发现可运行范围内的其他失败；不能据此宣布48组或真实交易验收全部通过。

## 远端一次收齐剩余失败

同步本次变更，包含新增`tests/test_main_ema_acceptance.py`，在原生Bomber环境、pro根目录执行：

```bash
PYTHONPATH=. python -u -m tests.run_main_ema_acceptance --keep-going
```

默认报告位于`tests/results/main-ema-acceptance-<时间戳>/`。查看最终`summary.json`和退出码非零组的同名日志；脚本末尾会列出所有失败组及集中复测命令。部署检查失败仍会先停止，须先补齐部署，避免在错误版本上运行全部测试。

[进度台账](LIVE_COMMON_PROGRESS.md)
