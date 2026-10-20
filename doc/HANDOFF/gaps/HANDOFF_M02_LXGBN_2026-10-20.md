# M02 LXGBN SDK 交接

日期：2026-10-20，Asia/Shanghai。用户已确认 LXGBN 与 DXBM-LXG 是同一 Windows 交易程序，**本项目是供其内嵌 Python 调用的 SDK**。此前将项目放在 Linux、用 HTTPS 调用 Windows 桥的实现方向错误，相关代码和运行入口改动已撤回；此前 227 项离线结果不作为当前架构的交付依据。

当前实现入口：[bomber.framework.lxgbn](../../../bomber/framework/lxgbn.py)。宿主创建会话，注入执行端口和原生字段复制函数，推送标准行情并持续调用 poll；三个回调维持 2/3/2 参数。复用实际 StrategyTemplate、Runner、风险检查、公共订单机、PositionManager/归属及 JSON 检查点，不加载外部 LXGBN SDK，不新建交易进程、网络服务或事件线程。

已经覆盖发送前整数 ID/请求号关联及写前保存、同步回调、逐笔增量和稳定 ID 去重、终态先于成交、撤单拒绝与报单拒绝区分、未知订单/状态/绑定冲突闭闸、发送结果不确定保留在途、保存故障、回调复制、有界队列、宿主线程约束、两阶段停止及受限终态恢复。首期仅测试绑定、单策略/单绑定/单合约、普通限价 Normal/投机，宿主需明确支持通用 CLOSE 及最终底层开平校验。

CTP 复用已验实际链路，仅对公共改动做回归；BN 延后。既有共享 CTP CLI、Unix IPC、原策略算法与 Nautilus 上游未因本期新增 LXGBN 分支。通用预算异常文案、限价 Planner 包装、基础 Backend 类型注解和保存钩子是本次公共改动。

验证：Python 3.12，六组离线模块共 **239 项通过**，其中 LXGBN SDK 场景 **36 项**。构造了实际 SDK 会话、Runner、风险/规划、OMS、归属及持久化；宿主执行端口和原生行情类型使用替身。9 个改动 Python 文件语法检查、独立进程契约导入与 git diff --check 通过。见[结构化证据](../../evidence/live/M02_LXGBN_OFFLINE_20261020.json)及[原始测试日志](../../evidence/live/M02_LXGBN_OFFLINE_20261020.txt)。未连接数据库、行情、Windows 交易端或柜台，未发送真实订单。

Windows 实际内嵌加载、原生引擎及 CPython/CPU/Boost.Python/运行库 ABI、宿主字段转换/线程、交付水位和真实受理/撤单/成交尚未验收。本机没有 bomber.model 原生扩展，完整原生引擎会话未运行。宿主交付序号/快照边界是新接入契约，不能宣称已有 C++ 结构体天然具备这些字段或完整性。需要实现/核对的是宿主对本项目 SDK 的接入和部署环境，不是要求框架寻找另一份 LXGBN SDK。

自动恢复仅支持同一绑定身份且已知订单全终态、水位和归属一致的状态；活动单/未知单、丢失成交、未决发送、换交易日/原生会话不自动接管或重发。策略指标/信号窗口需通过 state_components 接入既有检查点；恢复目标版本不等于恢复完整策略。宿主负责每份绑定只有一个活动会话和独立状态文件，本期没有跨进程 fencing。

使用与后续核对：[宿主调用说明](../../../scripts/integration/lxgbn/README.md)、[内嵌装配示例](../../../scripts/integration/lxgbn/embedded_example.py)、[设计与逐文件改动](../../architecture/M02_EXECUTION_CLIENT_DESIGN_2026-10-19.md)、[对接说明澄清](../../integration/DXBM_LXG_INTEGRATION_SPEC.md)。现场先只读核对绑定、能力、完整性及水位，再显式授权测试单。真实证据关联 M02/D5/GAP10 的 LXGBN 子项；M02 完整厂商矩阵、D5 整包及 GAP10 整体保持开放，整体审计未改为关闭。
