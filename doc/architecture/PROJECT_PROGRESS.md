# Bomber pro 项目进度报告（更新版）

**截至：2026 年 9 月 22 日。** 本报告依据更新后的[项目状态台账 (line 826)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:826)及最新策略架构文档整理，未重新运行测试。

---

## 一、总体进展

项目已形成统一的“行情 → 策略目标 → 组合汇总 → 订单规划与风控 → 执行后端 → 订单、成交及仓位回报”主链。历史回测、基础风险控制和执行事件回传已在测试环境验证。六类策略均已有实现或装配进展，其中单标的 EMA 的核心回测链路，以及外部目标计划策略的基础期货回测，已有明确的服务器验证结果。

**本次文档更新的核心进展在于在线执行端底层能力（P4 阶段）的实质性突破：**
1. **Binance 权威活动订单读取与 Native 绑定（P4-BN1/2）**：无网络全市场普通订单与 Algo 订单查询验证通过，Algo 订单未清空即闭闸，绝不回退至 Cache。
2. **原生 CTP TraderApi 报单规范与 Transport/Driver（P4-CTP1～3）**：完成了显式限价/GFD/投机报单构造及字段校验（P4-CTP1），实现了 `CtpNativeTraderDriver` 与 `CtpTraderTransport` 的前置连接、认证、登录、结算确认、ReqQry 分片聚合与推送防竞态（P4-CTP2/3，已在 `uv-nautilus` 通过）；异步拒单与撤单失败闭闸（P4-CTP4）代码已实现。
3. **边界明确**：实际 Binance DEMO 账户连接按既定安排暂缓；CTP 真实 SimNow 及实盘连接尚未接入，所有 Driver 对真实传输保持默认禁单。

[来源：P4 执行边界 (line 2018)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:2018)

---

## 二、已完成的基础能力

| 方向 | 当前成果 |
|---|---|
| **行情** | CTP 历史 Tick、Bar 解析和原生实时行情已验证；DolphinDB Tick、Bar 实流已验证；Binance 离线 Bar 和实时 Quote 基础能力已验证。行情健康状态可进入策略执行闸门。 |
| **策略与组合** | 统一策略模板、Runner、逻辑数据绑定、目标版本管理、组合净额、执行路由、账户仓位及在途量管理已建立，主要链路已通过测试。 |
| **历史回测** | 统一历史 Runtime、Nautilus 模拟后端和模拟执行客户端已验证；订单最早在信号之后的行情事件撮合（严格 N→N+1 顺序）。 |
| **风控与恢复** | 基础数量、仓位、名义金额和行情时效检查，Kill Switch、回报去重、状态文件保护及重启恢复闸门已验证。 |
| **执行事件与接口** | 统一订单、成交、仓位和账户事件契约（P0）已建立；假 Live 后端（P1）和模拟客户端（P2）的分发回调已通过；受控 Live 客户端账户读取器（P3d）与活动订单安全恢复（P3e）已通过；Binance 权威活动订单读取（P4-BN）与原生 CTP TraderApi 驱动/传输层（P4-CTP2/3）无网络测试已通过。 |

> *注：以上“已验证”指文档记录的相应测试环境结果，具体柜台和真实账户仍需单独验收。*  
> [来源：核心能力台账 (line 838)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:838)、[优先级台账 (line 1950)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:1950)

---

## 三、策略进展

| 策略类别 | 已完成 | 当前待完成 |
|---|---|---|
| **第一类：单标的 EMA（example01/02）** | 已统一策略逻辑，验证预热、多空目标及模拟撮合。CTP 历史 Bar、Tick 已进入统一回测主链，分别取得 1 笔和 45 笔成交。 | 增强后的固定基准逐笔审计、Binance 回测和在线 EMA 探针仍待回归。[详情 (line 1694)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:1694) |
| **第二类：五品种截面动量（example03/04）** | 共用策略、五路同步、收益率排名、定期调仓和五腿目标提交已编码；Feather Bar 与 CTP Quote Tick 聚合 MID Bar 的装配及测试入口已建立。 | 服务器和指定真实文件验收；实际五品种 CTP Tick 文件仍缺。[详情 (line 1745)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:1745) |
| **第三类：期货／期权联动及安全换月（example05）** | 期货、Call、Put 逻辑信号及单腿历史换月机制已编码，包含撤旧单、平旧仓、确认归零后开新仓。 | 已编写的离线测试待服务器运行；三腿协调、真实选权、裸腿控制、失败补偿及正式期权回测尚未完成。[详情 (line 1768)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:1768) |
| **第四类：RB 四角色复权价穿越（example06/07）** | 最小研究数据核、四角色复权和穿越信号已建立。真实 Bar 探针处理 1,317 根 Bar、264 个完整帧；该样本无穿越信号。 | 跨换月真实成交、多日正式回测、复权因子口径和结算核对仍待验收。[详情 (line 1805)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:1805) |
| **第五类：黑色系跨品种策略（example08/09/10）** | JM、I、RB 次主力复权收益率信号和 RB 主力执行装配已编码；角色、因子的时点查询及纯信号已在本地验证。 | 真实 Feather 数据下的同步、目标、订单和成交链路待远程验收。[详情 (line 1054)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:1054) |
| **第六类：外部目标计划驱动（example101、cta_by_pos、cta_rn）** | 完整目标计划、独立时钟触发和组合目标替换已实现。S1～S6 已在用户服务器通过：RB、SA 样本共 698 根 Bar、2 个计划时点、4 笔订单和 4 笔成交；逐笔审计确认成交晚于目标时点，最终仓位及在途量为零。 | 混合期货／期权／指数回测、生产交易日历、持久化时钟和多腿异常处理尚未完成。[详情 (line 1855)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:1855) |

*与上一版相比，第二至第五类策略未变更正式回测验收状态；更新内容主要集中在柜台执行端与交易底层架构。*

---

## 四、本次新增的执行层进展（P3 闭环与 P4 突破）

### 1. P3 受控在线样板持续巩固
- **账户读取器（P3d）**：实现 `NautilusReportedAccountReader`，经 TradingNode 发送只读 `QueryAccount` 并严格等待本次查询后的交易所 HTTP 报告，余额映射、超时与心跳闭闸（P3d1～P3d3）在 `uv-nautilus` 测试通过。
- **活动订单安全恢复（P3e）**：实现 `ActiveOrderSnapshot` 与严格对账比对，要求柜台全量快照与本地已知订单逐项一致才放行，未知/变化订单自动闭闸，P3e1～P3e5 验证通过。

### 2. P4 第一轮：Binance 权威活动订单读取（P4-BN）
- **实现 `BinanceHttpActiveOrderReader` 与 `BinanceNativeOpenOrdersBinding`**：
  - 必须由调用方注入本次向 Binance HTTP `open-orders` 端点查询全市场合约的异步方法，在 TradingNode 事件循环上调度；
  - 严禁将原生 `generate_order_status_reports()` 作为权威空快照来源（防止错误被捕获后吞错为空列表）；
  - 绑定全市场普通订单与 Algo 订单查询，查到任何活动 Algo 订单立即保持禁单闭闸，绝不把普通订单空列表误判为清空；
  - **验收情况**：`python tests/run_p4_binance_orders.py` 在 `uv-nautilus` 环境中运行，P4-BN2a 及 P4-BN1a/b 全部通过。[来源 (line 2018)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:2018)

### 3. P4 第一轮：原生 CTP TraderApi 报单与传输层（P4-CTP）
- **CTP 报单契约与前置强校验 (`make_ctp_order_insert`)**：
  - 固定 TraderApi 报单字段，只接受显式限价、GFD 和投机订单；
  - AUTO 开平、非整数手数、市价单和 reduce-only 开仓一律严正拒绝；
  - `python tests/run_p4_ctp_native_order.py` 验证通过。
- **CTP 原生驱动与底层传输状态机 (`CtpNativeTraderDriver` & `CtpTraderTransport`)**：
  - 底层传输只有严格完成“前置连接 → 认证 → 登录 → 结算确认”才能返回 `CtpTraderSession`；
  - 仓位、资金和活动订单查询必须等待本次柜台完整回报，错误绝不转为空快照；
  - Driver 默认禁单；仅测试可显式报单并验证 OrderRef 关联、部分/全部成交、TradeID 去重及断线闭闸；
  - **验收情况**：`python tests/run_p4_ctp_driver.py` 在 `uv-nautilus` 环境中运行，P4-CTP2a～c 全部通过。[来源 (line 2037)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:2037)
- **原生 TdApi 薄绑定与分片回报聚合 (`CtpTdApiTransport`)**：
  - 通过原生 TdApi 薄绑定完成认证、登录、结算确认、本次 ReqQry 分片回报聚合及订单/成交/断线推送；
  - 缺少最终回报、错误码、账户字段或早到未归属推送均失败闭闸，不回退缓存；
  - 传输在 Driver 建立会话与回报映射后调用 `activate()` 才放行推送，杜绝连接返回瞬间的推送竞态；
  - **验收情况**：`python tests/run_p4_ctp_td_transport.py` 在 `uv-nautilus` 环境中运行，P4-CTP3a～d 全部通过，P4-CTP2a～c 回归通过。[来源 (line 2046)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:2046)
- **异步拒单与撤单失败闭闸（P4-CTP4）**：
  - 异步 `OnRspOrderInsert`/`OnErrRtnOrderInsert` 拒单映射与双回调去重；
  - `OnRspOrderAction`/`OnErrRtnOrderAction` 撤单失败只闭闸，不伪造已撤单；
  - P4-CTP4a～c 代码已实现，等待 `uv-nautilus` 验收。[来源 (line 2051)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:2051)

---

## 五、下一步工作

1. **优先完成多策略真实数据回测验收**：
   - 回归第一类 EMA 的增强审计（固定基准 Bar/Tick）；
   - 使用指定真实 Feather 数据完成第二类截面策略（example03/04）多品种回测；
   - 运行第四类跨换月多日回测（example06/07）与复权因子的核对；
   - 运行第五类黑色系跨品种策略（example08/09/10）目标与成交链路的远程验收。
2. **推进复杂策略数据与期权机制**：
   - 准备历史主次力映射表、真实期权主数据和行情文件；
   - 实现第三类（example05）三腿协调、裸腿限制与异常补偿，并扩展第六类至混合资产回测。[来源 (line 1789)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:1789)、[(line 1855)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:1855)
3. **完成 CTP 柜台完整联调与真实环境适配**：
   - 在服务器端运行 `uv-nautilus` 验收 P4-CTP4（异步报单拒绝与撤单失败闭闸）；
   - 筹备真实 SimNow 柜台连接，验证网络环境下的双向今昨仓、跨重启订单对账与真实推送时序。[来源 (line 2055)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:2055)
4. **受控恢复 Binance 在线联调**：
   - 准备就绪时运行 DEMO 只读探针，验证原生 HTTP 实际连接、多币种全量查询与授权闭环，再进行最小 DEMO 订单验收。[来源 (line 2014)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:2014)
5. **扩展交易端及高保真规则**：
   - 推进滑点模型、涨跌停保护、交易时段划分和各品种实际手续费率规则；
   - 为后续规划的 Bomber 执行端及 vn.py 客户端保留一致契约适配。[来源 (line 924)](/Users/kerry/work/bomber1/pro/doc/architecture/STRATEGY_ARCHITECTURE.md:924)

---

**阶段结论：**  
项目在统一回测框架与风控闭环的基础上，在线执行层取得了决定性进展：不仅完成了 Binance 端的受控样板、账户读取与全市场活动订单权威读取（P3～P4-BN），更打通了原生 CTP TraderApi 从报单规范、会话握手、分片聚合到推送防竞态的完整传输体系（P4-CTP1～3），且全部在无网络沙箱中通过了状态机与断线闭闸测试。当前系统架构已具备对接真实柜台（SimNow / DEMO）的前置条件，下一步重心将双轨推进“未验收策略的多品种正式数据回测”与“CTP / Binance 真实测试环境连接验收”。
