# Bomber framework 开发与发布

核心组件统一位于 `bomber/framework`，开发代码和发布代码使用同一套导入路径：

```python
from bomber.framework.datahub import DataHub
from bomber.framework.dataprep.paths import resolve_paths
from bomber.framework.market.basic.base import DataType
from bomber.framework.trader import UnifiedStrategyRunner
```

当前组件为 datahub、dataprep、market、trader 和 fixes.py。当前源码中没有 ba；以后新增的组件、资源文件会随 framework 整体同步。

## 文档

设计、实现状态、数据契约和交接记录见 [项目文档导航](doc/README.md)。文档已按主题分类；旧路径查 [迁移对照](doc/MIGRATION_2026-10-10.md)。

## 开发

从 pro 根目录运行，使用已安装 Bomber 内核和项目运行依赖的 Python 环境：

```bash
export PYTHONPATH=.
python demos/01_main_ema/run_backtest.py --help
```

`pro/bomber/__init__.py` 只用于开发：优先使用本地 framework，同时扩展包搜索路径以复用已安装的 Bomber 内核，并执行内核原有初始化。发布只复制 framework，目标 Bomber 的 `__init__.py` 保持其原有内容。

如果把 demos 与 framework 复制到独立的策略项目（例如 pro1），并复用 Python 环境中已安装的 Bomber 内核，首次还需将本项目的 `bomber/__init__.py` 放到策略项目的 `bomber/__init__.py`。这个加载文件只需配置一次，后续更新仍只同步 framework。它不在 dist 中；dist 用于复制到完整 Bomber 包内部。

直接运行 `python demos/.../run_backtest.py` 时，入口会自动加入其所在项目的根目录。其他启动方式仍可使用 `export PYTHONPATH="$PWD"`；单独执行 `PYTHONPATH=.` 通常只设置 shell 变量，不会传给后续 Python 进程。

可以核对实际加载位置，避免使用另一份旧 framework：

```bash
python - <<'PY'
import bomber
import bomber.framework
print('engine initializer:', bomber.__file__)
print('package paths:', list(bomber.__path__))
print('framework:', bomber.framework.__file__)
PY
```

datahub 与基础 dataprep 工具可以独立导入；交易运行时需要 Bomber 内核和编译完成的 market/basic 扩展。构建步骤见 [market/basic](bomber/framework/market/basic/README.md)。CTP MD/TD 扩展按 [CTP binding](bomber/framework/market/native/ctp/binding/README.md) 在 Linux 部署环境单独构建安装。

数据路径由业务项目决定。相对路径默认以运行时工作目录为基准，不以 framework 安装位置为基准；库调用方可通过 `resolve_paths(..., project_root=...)`、`resolve_futures_args(..., project_root=...)` 或 `resolve_option_args(..., project_root=...)` 显式指定基准。

## 同步与发布

默认只报告差异；有差异时退出码为 1，完全一致时为 0，配置或源码错误时为 2：

```bash
python scripts/sync-bomber-framework.py --diff
python scripts/sync-bomber-framework.py --apply
```

默认输出为 `pro/dist/bomber/framework`。同步到其他项目时，将 `--dist-root` 指向包含真实 Bomber 包的项目根目录：

```bash
python scripts/sync-bomber-framework.py --dist-root /path/to/project --diff
python scripts/sync-bomber-framework.py --dist-root /path/to/project --apply
```

目标已经有旧 framework 且没有同步标记时，首次可加 `--adopt-existing`；先用同一参数做 dry-run，查看将被替换、删除的文件。后续正常同步即可。源目录与目标目录不能重叠。

同步保留源码、文档、Cython 文件、setup.py、CTP C++/头文件/SDK 动态库与构建配置，排除缓存和 market/basic 的编译产物。脚本不再改写 import；发现旧顶层导入时直接报错。每次同步重建目标副本并清理过期文件，避免留下已经删除的模块。

目标项目仍需匹配的 Bomber 内核、Python 依赖和原生扩展。源码同步完成后，按目标环境完成构建或随 Bomber 主库打包。demos、examples 和 tests 固定使用 `bomber.framework.*`，发布时不再修改它们的导入。

## 验证

CTP SimNow＋Binance DEMO的双端联合运行、账户写入保护及故障闭闸入口见 [联合联调说明](scripts/integration/ctp_binance/README.md)。PF2026100905首期代码及计划内离线回归完成，joint 24项、四组关联回归、Binance活动订单及Live Backend E9全部通过；当前双端柜台验收待完成，GAP-10尚未关闭。

```bash
PYTHONPATH=. python -m unittest discover -s tests -p test_framework_layout.py -v
PYTHONPATH=. python tests/run_offline_regression.py
```

结构测试覆盖资源完整性、源码导入、开发包加载、相对数据路径、重复同步、旧文件清理和目标保护。完整离线回归还需要可运行的 Bomber 内核与 Cython 扩展；这些测试不会连接实盘柜台。
