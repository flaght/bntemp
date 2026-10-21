# Windows LXGBN 调用项目 SDK

LXGBN（DXBM-LXG）是 Windows 交易宿主，本项目是供其内嵌 Python 调用的 SDK。入口为 `bomber.framework.lxgbn`。本目录没有独立交易进程、HTTP 服务或厂商 SDK 加载器。

交给 LXGBN 开发者的完整接入步骤、接口字段和实机测试清单见 [SDK 接入与测试指南](../../../doc/integration/LXGBN_SDK_INTEGRATION_GUIDE_2026-10-21.md)。[sdk_acceptance_example.py](sdk_acceptance_example.py) 提供八个可直接运行的无柜台示例；在已安装 wheel 的 Windows Python 中以 `python -I 实际脚本路径` 运行，不注入项目源码目录。

CTP 已有实际链路继续复用；BN 本期延后。首期仅测试环境，一个策略、一份绑定、一个合约，普通限价 Normal 与投机。实际 Windows/柜台联调尚未完成。

## Python 3.12 已确认后的第一步

先运行 [environment_probe.py](environment_probe.py)，再决定构建范围。探针本身只用标准库，记录系统、完整 Python 版本、解释器位数、二进制后缀和实际模块路径，分开检查契约、Bomber 内核、market/basic 扩展和完整会话工厂。只做导入，不构造会话、不调用报撤单端口；通过也不代表原生 ABI/数值、回调线程或真实交易已验收。

下面假设本期源码位于 `D:\bomber-pro`，报告保存到 `D:\lxgbn-test`。先在匹配宿主版本和位数的 Windows Python 3.12 环境运行：

```powershell
cd D:\bomber-pro
python scripts\integration\lxgbn\environment_probe.py --project-root D:\bomber-pro --output D:\lxgbn-test\standalone-environment.json
```

这里的 `python` 必须是选定的 3.12 解释器；不要根据系统默认命令推断它与宿主相同。没有独立 Python 时，可直接做下面的内嵌检查。

**还需在 LXGBN 实际内嵌 Python 内运行一次**，它的 DLL 搜索路径、已加载模块和 Python 搜索路径可能与独立 Python 不同。通过宿主现有脚本入口执行下面的代码，不另起解释器：

```python
import runpy

probe = runpy.run_path(r'D:\bomber-pro\scripts\integration\lxgbn\environment_probe.py')
report = probe['collect_report'](project_root=r'D:\bomber-pro')
probe['write_report'](report, r'D:\lxgbn-test\embedded-environment.json')
```

探针会临时加入显式源码目录，导入后恢复 `sys.path`；不会清除宿主已加载的模块。如果 framework 实际来自另一个目录，报告会阻止通过，应先统一部署路径并正常重启测试宿主，不在运行中强行替换原生模块。不要从 Linux/macOS 复制扩展二进制。

| 报告结果 | 下一步 |
| --- | --- |
| `sdk_contract` 失败 | 检查本期源码是否部署完整、实际加载路径是否正确 |
| `bomber_engine` 失败 | 先提供匹配 Windows/CPython 3.12/位数的 Bomber 内核；已有兼容产物复用，没有才构建内核 |
| 内核通过、`market_basic` 失败 | 按具体错误核对 `.pyd` 和依赖；没有兼容产物才构建两个扩展 |
| 前面通过、`session_factory` 失败 | 根据报告中的具体依赖/导入错误处理，不能把契约可导入当成 SDK 全部可用 |
| 内嵌报告 `windows_imports_ready=true` | 做原生行情对象量价检查，再接 HostPort/字段复制及只读绑定；尚不授权实单 |

需要构建 market/basic 时，在匹配宿主的 Python 3.12 构建环境、项目根目录执行：

```powershell
python bomber\framework\market\basic\setup.py build_ext --inplace
```

前提是已经提供匹配的 Bomber 内核及其 `.pxd`、Cython/NumPy/setuptools 和可用的 Windows C 编译工具。若内核尚缺失，不先执行这条命令。详见 [market/basic 构建说明](../../../bomber/framework/market/basic/README.md)。

本期不要求因此重编译整个 LXGBN；只有需要修改其原生接口/回调转发或绑定时才涉及宿主构建。当前仍需由宿主接入 `send_order/cancel_order/query_binding` 和原生字段复制函数，并核对三个回调参数、线程/GIL、通用 CLOSE 及快照水位。Python 3.12 版本一致本身不能代替这些联调。

## 内核通过后：单独构建 market/basic

2026-10-21 Windows 报告已确认 `D:\bomber\bomber` 下 SDK 契约及三个内核模块可导入；`fast_factory` 缺失阻止 market_basic 和 session_factory。此时只需编译两个 framework 扩展，不重编内核。

先将本期 `bomber/framework/market/basic/setup.py` 同步至 Windows 对应路径。更新后的脚本补齐内核头文件搜索目录，并重新生成 C 文件，避免沿用此前错误嵌套目录下生成的模块。保留当前已初始化的 x64 Developer CMD，执行：

```cmd
"%BOMBER_PYTHON%" -I -m pip install "Cython==3.2.6" "setuptools>=82" wheel
cd /d D:\bomber\bomber
"%BOMBER_PYTHON%" -I "D:\bomber\bomber\bomber\framework\market\basic\setup.py" build_ext --inplace
```

若旧脚本报 `Multiple top-level packages discovered in a flat-layout`，更新该 setup.py 后重试相同编译命令。脚本显式设置 `packages=[]` 和 `py_modules=[]`，仅构建声明的两个扩展，禁止 setuptools 自动扫描内核工程的 assets/crates 等目录；无需移动这些目录。Cython 的 IF 弃用及许可证元数据弃用警告不是这次失败的原因。

NumPy 已安装时无需重装。内核构建隔离环境中的 Cython/setuptools 不代表已安装进运行用的 Conda 环境，因此这里显式补齐。使用该目录刚构建的标准精度内核 `.pxd` 及 `.h`，不复制 macOS 接口或二进制覆盖它们。编译成功后重新运行：

```cmd
"%BOMBER_PYTHON%" -I "D:\bomber\scripts\integration\lxgbn\environment_probe.py" --project-root "D:\bomber\bomber"
```

四项全部通过后，再做原生行情数值检查及 LXGBN 内嵌环境探针。这里的独立进程导入成功不表示交易联调完成。

## 导入通过后：行情数值及内嵌检查

2026-10-21 01:59 UTC，用户提供的 Windows CPython 3.12.15 / AMD64 报告中，四项导入检查全部通过，`windows_imports_ready=true`；两个 market 扩展均来自 `D:\bomber\bomber\bomber\framework\market\basic`。这仅确认该独立进程的构建及导入。

将 [native_value_probe.py](native_value_probe.py) 放到 Windows 的 `D:\bomber\scripts\integration\lxgbn`，与 environment_probe.py 同目录，再运行：

```cmd
"%BOMBER_PYTHON%" -I "D:\bomber\scripts\integration\lxgbn\native_value_probe.py" --project-root "D:\bomber\bomber"
```

脚本检查三个不同量价精度的真实 TradeTick、QuoteTick、批量 TradeTick 和 CustomBar 的字段、因子及纳秒时间戳；不创建会话、不连接交易端口。四项数值检查通过且 `native_values_ready=true` 后，在 LXGBN 的实际内嵌脚本入口再执行：

```python
import runpy

probe = runpy.run_path(r'D:\bomber\scripts\integration\lxgbn\environment_probe.py')
report = probe['collect_report'](project_root=r'D:\bomber\bomber')
probe['write_report'](report, r'D:\lxgbn-test\embedded-environment.json')

values = runpy.run_path(r'D:\bomber\scripts\integration\lxgbn\native_value_probe.py')
report = values['collect_report'](project_root=r'D:\bomber\bomber')
probe['write_report'](report, r'D:\lxgbn-test\embedded-native-values.json')
```

保留两份报告。若宿主没有使用这套部署目录，按报告路径核对并统一部署，不热卸载原生模块。数值检查也不覆盖完整 ABI、GIL/回调线程、HostPort 快照水位或真实委托；通过后继续宿主只读绑定联调。

## 已观测的 Windows 阻塞及内核构建入口

2026-10-20 的独立 Windows 探针结果：CPython 3.12.15 / AMD64 / 64 位，契约加载成功，但 `bomber.model` 和 NumPy 缺失。market_basic 与 session_factory 在 NumPy 导入处停止，不能据此认定其余依赖或 `.pyd` 已经检查完成。

先在该 Python 3.12 环境补 NumPy：

```powershell
python -m pip install "numpy>=1.26.4"
```

随后需要本项目的 Windows Bomber 原生内核。`pro/bomber` 是开发覆盖层，不包含完整 `bomber.model` 内核；已有匹配 Windows/CPython 3.12/x64 的项目 wheel 时直接安装该文件。不要根据包名到其他索引猜测安装源，也不将普通 NautilusTrader wheel 当成本项目 `bomber.*` 内核。

**先完成源码传输。** `D:\bomber-core` 是目标目录示例，并不是安装 NumPy/build 后会自动创建的目录。只有 `bomber-pro` 的 Git checkout 仍然缺少真正的内核工程。`cd` 若失败，不要继续执行 build；普通 PowerShell 的目录错误可能不终止后续命令，从而仍在 pro 里构建。

已在本机准备 `pro/dist/windows-core-20261020/bomber-core-source-20261020.zip` 和 SHA256 校验文件。ZIP 根目录为 `bomber-core/`，包含内核 Rust/Cython 源码、`.cargo`、patches 与构建配置，排除了既有原生二进制及缓存；内部 LICENSE/README 符号链接已保存为普通文件，便于 Windows 解压。ZIP 是源码包，未在 Windows 编译验收，也不包含本期 pro 的替代版本。

把 ZIP 传到 Windows，例如 `D:\bomber-core-source-20261020.zip`，先解压：

```powershell
$ErrorActionPreference = "Stop"
Expand-Archive -LiteralPath D:\bomber-core-source-20261020.zip -DestinationPath D:\
Test-Path D:\bomber-core\pyproject.toml
Test-Path D:\bomber-core\bomber\model\data.pyx
```

两个检查都应为 True。保留现有 `D:\bomber-pro`。包内 `BUILD_WINDOWS.ps1` 会以脚本自身目录为构建根，检查源码及工具，并在失败时停止；工具准备好后可运行 `& D:\bomber-core\BUILD_WINDOWS.ps1`。手动构建也必须先确认目标目录已经存在。

本地内核源码在同级工程 `bomber1/code/bomber`，其根目录包含 `pyproject.toml`、`build.py`、`Cargo.toml`、`Cargo.lock`、`.cargo/`、`crates/`、`bomber/model/`。没有兼容 wheel 时，将该工程源码完整准备到 Windows（以下示例目录为 `D:\bomber-core`）；保留隐藏构建配置，使用干净构建目录，不沿用 Linux/macOS 的 `.so`、Rust target 或编译缓存。内核仓库自带的 framework 可能早于本期版本，运行探针时继续显式指向 `D:\bomber-pro`。

构建前准备 MSVC x64、Windows SDK 及仓库 README 要求的 Clang，使用已初始化这些工具的 Developer PowerShell。Rust 工具链按内核配置准备；当前 `Cargo.toml` 的最低 Rust 为 1.96.0，构建脚本使用 stable。使用同一个 Python 3.12 / x64 环境完成构建和安装。

```powershell
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath D:\bomber-core
if (-not (Test-Path .\pyproject.toml)) { throw "Bomber 内核源码不完整，停止构建" }
python -m pip install build
$env:HIGH_PRECISION = "false"
$env:BUILD_MODE = "release"
$env:PYO3_PYTHON = (python -c "import sys; print(sys.executable)")
python -I -m build --wheel .
```

**必须保留 `-I`。** 内核根目录自身有 `build.py`，普通 `python -m build` 会优先命中这个源码文件，而不是已安装的 PyPA build 前端；此时可能首先报缺 Cython。隔离模式会忽略当前目录/PYTHONPATH 的隐式搜索入口，使用所选解释器已安装的前端。正常前端随后按 pyproject.toml 在构建隔离环境中安装 `cython==3.2.6` 等依赖。

在本轮 Windows CMD 已设置 BOMBER_PYTHON 的情况下，直接重试：

```cmd
"%BOMBER_PYTHON%" -I -m pip install build
"%BOMBER_PYTHON%" -I -m build --wheel "D:\bomber\bomber"
```

这两行使用用户当前实际内核路径，不依赖之前的 `D:\bomber-core` 目录示例；同一 CMD 内保留已加载的 x64 工具、PYO3_PYTHON、精度及镜像设置。

构建后从 `dist` 选择本次生成的 `bomber-…-cp312-…-win_amd64.whl`，在该 Python 环境执行 `python -m pip install <实际wheel路径>`。该命令中的路径是占位说明，不是可直接粘贴的文件名。构建前后均可用 `python -c "import sys; print(sys.executable)"` 核对解释器。这里不启用 PYO3_ONLY；框架需要 Cython 的 `bomber.model`，只编 PyO3 不足以满足当前依赖。

退出内核源码目录，在部署目录复查，避免把源码目录能导入误当成安装成功：

```powershell
cd D:\bomber-pro
python -c "import bomber.model.data as m; print(m.__file__)"
python scripts\integration\lxgbn\environment_probe.py --project-root D:\bomber-pro
```

只有内核检查通过后，再按前节处理 market/basic 的实际错误。需要单独构建该扩展时，在相同环境准备内核 `.pxd`、`Cython==3.2.6`、`setuptools>=82`、wheel、NumPy 和 C 编译器，再执行其 setup.py。如果报告只是缺 NumPy 等依赖，先补依赖后重测，不立即归因为二进制缺失。

以上构建命令根据本地内核构建文件整理，尚未在远程 Windows 执行。独立 Conda 环境通过后仍需在 LXGBN 内嵌 Python 中运行同一探针；宿主的包和 DLL 搜索路径不能由 Conda 结果代替。

## Cargo 下载超时：工程内配置依赖镜像

Rustup 的 RUSTUP_DIST_SERVER / RUSTUP_UPDATE_ROOT 只覆盖工具链下载，不替换 Cargo 的 crates.io 依赖源。若停在 Updating crates.io index，出现 HTTP/2 stream reset，可在当前 CMD 设置 `set "CARGO_HTTP_MULTIPLEXING=false"` 后重试。

如果随后仍反复出现 30 秒传输 0 字节，先 Ctrl+C 停止，在 CMD 打开工程配置：

```cmd
notepad "D:\bomber\bomber\.cargo\config.toml"
```

保留原有 [build]、[target...] 配置，在文件末尾追加以下段落；若同名段已存在，只修改其值，不重复定义：

```toml
[source.crates-io]
replace-with = "rsproxy-sparse"

[source.rsproxy-sparse]
registry = "sparse+https://rsproxy.cn/index/"
```

保存后在同一 CMD 重试：

```cmd
set "CARGO_HTTP_MULTIPLEXING=false"
"%BOMBER_PYTHON%" -I -m build --wheel "D:\bomber\bomber"
```

只改变该工程依赖下载源，无须删除 Cargo.lock、清空 target 或重装编译器。如果再次失败，保留最终 error 段及具体 URL，重复的低速 warning 本身不能定位最终失败地址。配置依据：[RsProxy](https://rsproxy.cn/)、[Cargo source replacement](https://doc.rust-lang.org/cargo/reference/source-replacement.html)、[Cargo HTTP multiplexing](https://doc.rust-lang.org/cargo/reference/config.html#httpmultiplexing)。此配置已与本地工程 TOML 合并解析验证，远程 Windows 的镜像可达性仍需实际重试。

## 宿主调用顺序

[embedded_example.py](embedded_example.py) 展示装配函数。宿主传入现有 `StrategyTemplate` 策略、绑定、`HostPort`、回调字段复制函数、行情绑定、跳价、风控和状态文件。策略算法无需识别厂商。

```python
from bomber.framework.lxgbn import create_session

session = create_session(
    strategy=strategy, binding=binding, host=host, copies=copies,
    data_bindings=data_bindings, target_key='main', price_increment='1',
    risk_limits=risk_limits, state_file=state_file,
)
# 在宿主原生回调对象仍有效时安装下列三个转发，然后只读启动。
session.start(resume=False)  # 已有状态文件必须显式 resume=True。
session.authorize()         # 核对通过后显式开放测试报单。

# 宿主已有事件线程：标准 Bomber 行情对象由宿主转换。
session.on_market('lxg', standard_tick_or_bar)
session.poll()              # 也需在没有行情时由宿主持续调度。

# 对应 C++ override 的完整参数，不要漏掉 clientOrderId/orderMsg。
session.onOrderAck(clientOrderId, orderMsg)
session.onOrderFill(clientOrderId, orderMsg, execMsg)
session.onOrderFinished(clientOrderId, orderMsg)

session.begin_stop()        # 闭闸、向宿主请求撤本策略已知活动单。
# 宿主继续转发回报并 poll；活动单未终结时 finish_stop 会拒绝结束。
session.finish_stop()
# 此后宿主解除回调并确认原生回调线程退出，才能释放 Python 对象/解释器。
```

上述片段表示生命周期各阶段，并非顺序执行一次即可完成的脚本。构造和只读启动不发送交易请求；策略 `on_start` 应只做初始化，授权后才能输出交易目标。反手先平仓，待后续策略目标更新才推进反向开仓，不另加自动重报机制。要恢复指标/信号窗口时，向 `state_components` 传入策略既有的 `snapshot_state/restore_state` 组件；框架只自动恢复目标版本及执行状态。

## 宿主实现的三个端口

- `send_order(OrderCommand) -> SubmitResult`：使用请求中的整数 `client_order_id`，原样转发到三个回调。`queued` 只代表已接收；`rejected` 必须表示确定未发送；超时、未知返回或异常都保留未决订单、禁止重发。普通限价请求包含 `attribute=1`、`hedge_flag='Speculation'`，没有额外 TIF。宿主负责最终交易校验与 OPEN/CLOSE 底层转换，不能把缺失能力静默降级。
- `cancel_order(client_order_id) -> None`：按已登记关联撤单；返回不表示已撤。撤单拒绝回报必须标记 `rejection_of='cancel'`，保留原工作单。
- `query_binding() -> BindingSnapshot`：提供当前策略/绑定的归属、活动单、初始化/完整性、真实执行可用状态及能力声明。它不是账户资金或全量仓位查询。默认未声明能力，尤其 `generic_close`、`acked_is_accepted`，都会阻止授权。

`Binding` 含 account_id、binding_id、strategy_id、trading_day、source_session、instrument_id、environment。source_session 是宿主实际原生 ID 命名空间，不是框架每次启动随机生成的字符串。此版本只接受 `environment='test'`。

`BindingSnapshot.boundary` 和快照中 `delivery_sequence` 共同定义宿主交付流。宿主须在交付时分配连续序号，且查询水位覆盖的事件都已提交给 SDK；同绑定恢复需延续水位或重放原流。它们是新定义的宿主接入契约，**不假定 OrderMsg_t 自带序号，也不能用本地计数假装原生业务顺序/完整快照**。无法建立边界时保持只读。框架不会据“活动单列表为空”推断历史订单已撤。

## 原生对象复制与线程

`CallbackCopies(order=copy_order, fill=copy_fill)` 由宿主接入层提供。复制函数在回调有效期内返回只含值的 `OrderSnapshot` / `FillSnapshot`，并完成：

- 校验原始 AccountId/StrategyId/InstrumentID 等与实际绑定的对应关系。不可直接把配置身份填入所有回报而省略核对。
- 保留稳定原生订单 ID、成交 ID；用明确的枚举映射输出 BUY/SELL、OPEN/CLOSE/CLOSE_TODAY/CLOSE_YESTERDAY 等。除已知 Normal=1 外，不猜测原生枚举整数值。
- 明确固定委托量、单笔成交量和累计量的不同含义，验证单位、字符编码和纳秒时间转换；Finished 不合成成交。
- 区分报单拒绝和撤单拒绝；无法确定时输出 unknown，框架会闭闸。
- 原始订单和成交必须指向同一订单。算法父子单、Market/FAK/FOK 和其他未声明能力不接入本期普通单路径。

三个回调保持 2/3/2 参数，只复制并放入有界队列。返回 False 表示转换/身份/容量故障，宿主必须记录并保留原生事实用于恢复。回调可由其他原生线程调用；宿主负责取得 GIL 和保证复制期间对象一致。生命周期、行情、`poll()` 由同一宿主线程串行调用，发送期间同步回调只能入队，不能重入 `poll()` 执行业务回调。

`on_disconnect(reason)` 在宿主失联/交易不可用时调用，立即锁存故障。SDK 不创建事件线程，也不登录、关闭宿主交易系统。停止期间继续处理实际撤单/成交，全部核对并保存后宿主才可释放绑定。

## 持久化与首期恢复限制

复用现有 JSON 检查点和公共 OMS/归属。发送前写入整数 ID/字符串 ID、PENDING、冻结分配；处理回报后保存。发送结果不确定时保留原关联和在途，不重复报单。重复成交按稳定 ID 去重；终态先到则等待真实成交补齐，终态后新成交闭闸。

同一检查点只允许同一绑定/交易日/原生会话，已知单全部终态、快照水位和归属一致时自动恢复。活动单、丢失成交、未知事件、换日/换会话需宿主提供只读查询/重放恢复后再评审；本版不自动接管。宿主负责一个绑定只有一份活动会话，并为每份绑定使用独立状态文件；本版不新增跨进程锁或通用分布式 fencing。

## Windows 验证边界

契约模块不依赖外部 LXGBN 包，完整策略会话继续依赖项目现有 Bomber 原生引擎。Windows 现场必须已有与内嵌 CPython、CPU 架构、C++/Boost.Python 和运行库兼容的引擎及宿主绑定。标准库契约可导入不能证明原生引擎已完成 Windows 发布；本批没有移植或编译该引擎。

离线测试使用真实 Runner、风控、订单机、归属和 JSON 保存，宿主端口和原生行情类型为受控替身：

```sh
PYTHONPYCACHEPREFIX=/tmp/m02-pycache python3.12 -m unittest \
  tests.test_lxgbn_execution tests.test_shared_account tests.test_execution_composition \
  tests.test_shared_process_cli tests.test_live_common tests.test_live_channels -q
```

实际 Windows 加载、原生对象字段复制与线程规则、快照水位、真实受理/撤单/成交和稳定 ID 仍需现场验证。同一份实际证据关联 M02/D5/GAP10 的 LXGBN 子项，不据离线测试关闭整体审计项。详细依据见[设计](../../../doc/architecture/M02_EXECUTION_CLIENT_DESIGN_2026-10-19.md)与[对接说明](../../../doc/integration/DXBM_LXG_INTEGRATION_SPEC.md)。
