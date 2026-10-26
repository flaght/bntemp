# input_session

用途：为一次数据准备或回测入口建立输入会话，在嵌套调用之间共享读取缓存、文件版本、覆盖范围和输入诊断。

- 导入路径：`bomber.framework.dataprep.session.input_session`；也可从 `bomber.framework.dataprep` 导入。
- [函数及会话源码](../../../bomber/framework/dataprep/session.py)
- [诊断与覆盖范围结构](../../../bomber/framework/dataprep/contracts.py)
- 核对日期：2026-10-26，Asia/Shanghai。

## 1. 这个函数负责什么

`input_session` 是装饰器，通常放在 `main`、`run_case` 等同步入口函数上：

1. 调用入口时，如果没有活动会话，创建并进入一个 `InputSession`。
2. 如果已经有活动会话，复用该会话，内外层共享缓存和诊断。
3. 从本次调用的指定参数中记录请求日期范围。
4. 执行被装饰函数，原样返回它的结果。
5. 被装饰函数抛出 `Exception` 时，按可识别的报告目录尝试写失败诊断，再抛出原异常。
6. 创建会话的最外层调用结束时，恢复原上下文并清空会话缓存；正常返回和异常退出都会清理。

装饰函数或导入模块时不会执行函数体。会话只管理输入生命周期，不创建策略、账户或运行器；成功报告需要调用方主动写出。

## 2. 函数签名

```python
from bomber.framework.dataprep.session import input_session

def input_session(function):
    ...
```

返回的包装函数使用 `wrapped(*args, **kwargs)` 接收调用参数，并通过 `functools.wraps` 保留原函数的名称、文档等元信息。

```python
@input_session
def run_case(*, start_day, end_day, report_dir):
    ...
```

等价于 `run_case = input_session(run_case)`。当前装饰器没有配置参数，应使用 `@input_session`，不能写成 `@input_session()`。

## 3. 参数速查

装饰器本身只有一个参数：

| 参数 | 常用类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `function` | 同步可调用对象 | 必填 | 要包装的数据准备或运行入口 |

以下字段属于**被装饰函数的调用参数**，不是装饰器参数：

| 字段 | 常用类型 | 识别位置 | 用途 |
| --- | --- | --- | --- |
| `start_day` | `datetime.date` / `None` | 显式关键字，或首个位置参数的同名属性 | 请求区间起点 |
| `end_day` | `datetime.date` / `None` | 与 `start_day` 同一来源 | 请求区间终点 |
| `report_dir` | `str` / `Path` / `None` | 显式关键字，或首个位置参数的同名属性 | 失败报告的父目录 |

装饰器不会绑定原函数签名、补齐函数默认值、转换日期字符串或验证日期先后。日期通常传 `datetime.date`，包含边界的交易日筛选由下游数据准备函数执行。

## 4. 日期范围与报告目录如何识别

### start_day / end_day：请求区间

识别顺序如下：

1. 如果实际传入的关键字中有 `start_day`，记录 `(kwargs.get("start_day"), kwargs.get("end_day"))`。
2. 否则，如果首个位置参数有 `start_day` 属性，记录 `(args[0].start_day, args[0].end_day)`；该对象也必须有 `end_day` 属性。
3. 都不满足时，保持会话原来的请求区间。新会话初始为 `(None, None)`。

因此，`run_case(start_day=start, end_day=end)` 和 `run_case(args)` 都可以识别；后者的 `args` 可为含对应属性的 `argparse.Namespace`。普通位置调用 `run_case(start, end)` 不会按形参名称识别日期；仅传 `end_day=` 也不会触发区间更新。

嵌套调用会直接覆盖共享会话的 `coverage.requested`，返回外层时不会自动恢复旧区间。一个会话顺序运行多个日期区间时，覆盖信息和诊断可能累计，不能把它理解为每个子调用各有一份独立报告。

请求区间供下游读取逻辑区分 `actual_days` 与 `dependency_days` 等覆盖信息使用，不会自动限制读取文件范围，也不决定分钟标签或夜盘归属。分钟时间约定仍由行情读取参数控制。

### report_dir：失败报告位置

先取实际传入的 `report_dir=`；值为 `None` 或未传时，再尝试首个位置参数的 `report_dir` 属性。仍为 `None` 时，不自动写失败报告。

例如，函数声明 `report_dir=Path("reports")`，调用时省略该参数，装饰器不会读取这个签名默认值。在无参 `main()` 内部才解析出的 `args.report_dir` 也不可直接识别；应将目录显式传给嵌套的被装饰函数。

## 5. 输入文件与缓存约定

装饰器本身不读取输入文件，也不要求特定行情字段。实际资料格式由 `read_feather`、`read_bar_frame`、`prepare_role_research` 等下游函数决定。只有接入当前会话的数据准备函数才使用这些共享能力。

| 会话内容 | 行为 |
| --- | --- |
| `cache` | 保存目录清单、原始 Feather 帧及下游准备结果；同次运行复用，退出时清空 |
| `manifest` | 记录被 `fingerprint` 检查的文件绝对路径、大小和 `mtime_ns` |
| `coverage` | 保存请求区间、实际日期、依赖日期、缺失项及假设等 |
| `issues` | 收集 `InputIssue`，包括下游校验问题及运行失败诊断 |
| `paths` | 收集接入会话的路径解析记录 |
| `counters` | 记录 `directory_scans` 和 `feather_reads` 次数 |

`read_feather(path, columns=None)` 按绝对路径、文件大小、修改时间及列投影缓存。已有完整帧时可直接选列，返回深拷贝，调用方修改结果不会污染缓存；直接使用 `pandas.read_feather` 不会自动经过这层缓存。

`fingerprint` 再次检查同一路径时，如果大小或 `mtime_ns` 改变，会抛出 `SOURCE_CHANGED`。这是文件元数据检查，不是内容哈希或后台监控；同一根目录的清单在会话内复用，也不会持续发现新增文件。不同次最外层调用使用独立缓存，没有跨运行持久缓存或容量上限。

退出时只显式清空 `cache` 并恢复上下文；如果调用方持有会话对象，`manifest`、`coverage`、`issues` 等记录仍保留。

## 6. 返回结果与报告

调用 `input_session(function)` 返回包装后的可调用对象；调用被装饰函数时，返回值与原函数一致，不会自动返回 `InputSession` 或报告路径。

需要取得会话时，可以在函数体内调用 `current_session()`，或手动使用 `with InputSession() as session:`。手动创建并嵌套另一个 `InputSession` 会切换到独立会话，不会复用外层缓存；同一实例在活动状态下不能重入。

成功时，在会话退出前调用 `write_input_reports(directory)`，会写出：

| 文件 | 内容 |
| --- | --- |
| `input_manifest.json` | `schema_version="inputs-v1"`、文件版本、路径来源、计数器及元数据证据声明 |
| `input_coverage.json` | 请求范围、实际与依赖日期、缺失文件、假设及日历来源等 |
| `input_issues.json` | 输入问题列表 |

目录不存在时自动创建。`write_input_reports` 在没有活动会话时不写文件；需要退出后导出时，应保留会话对象并调用 `session.write_reports(directory)`。

被装饰函数抛出 `Exception` 且能找到 `report_dir` 时，会尝试写入 `report_dir/input_failed_<12位随机标识>/`，文件仍为上述三份。非 `InputError` 异常会追加 `RUN_FAILED`；`InputError` 不会在此重复追加，通常由下游 `fail` 在抛出前记录。

失败报告写入中的 `OSError`、`TypeError`、`ValueError` 会被忽略，以继续抛出原异常。`SystemExit`、`KeyboardInterrupt` 等不属于 `Exception`，不会触发该失败报告逻辑。嵌套装饰器逐层处理同一异常时，可能生成多份失败报告并重复追加 `RUN_FAILED`。

## 7. 调用示例

### main 与 run_case 共享会话

以下示例假设 `basic_path` 指向可读取的 Feather 基础资料文件；目录和日期均由调用方显式传入。

```python
from datetime import date
from pathlib import Path
from bomber.framework.dataprep.session import (
    current_session,
    input_session,
    read_feather,
    write_input_reports,
)


@input_session
def run_case(*, basic_path, start_day, end_day, report_dir):
    basic = read_feather(basic_path)
    # 在这里准备行情、运行策略；成功后仍在活动会话内写报告。
    write_input_reports(report_dir)
    return basic


@input_session
def main(*, basic_path, start_day, end_day, report_dir):
    outer_session = current_session()
    read_feather(basic_path)
    result = run_case(
        basic_path=basic_path,
        start_day=start_day,
        end_day=end_day,
        report_dir=report_dir,
    )
    assert current_session() is outer_session
    # 同一文件和投影只实际读取一次，返回的 DataFrame 各自独立。
    assert outer_session.counters["feather_reads"] == 1
    return result


if __name__ == "__main__":
    result = main(
        basic_path=Path("/ABS/PATH/TO/fut_basic.feather"),
        start_day=date(2026, 1, 5),
        end_day=date(2026, 1, 6),
        report_dir=Path("reports/example"),
    )
    assert current_session() is None
```

第二次读取复用原始帧缓存；示例只演示会话和报告，不会自动生成行情覆盖记录。实际入口可参考 [主力 EMA 回测](../../../demos/01_main_ema/run_backtest.py) 和 [HybridTransformer 回测](../../../demos/10_hybrid_transformer/run_backtest.py)。

### 通过 Namespace 传递参数

```python
from argparse import Namespace


@input_session
def prepare(args):
    frame = read_feather(args.basic_path)
    write_input_reports(args.report_dir)
    return frame


# 沿用上一示例导入的 date、Path、input_session、read_feather 等。
args = Namespace(
    basic_path=Path("/ABS/PATH/TO/fut_basic.feather"),
    start_day=date(2026, 1, 5),
    end_day=date(2026, 1, 6),
    report_dir=Path("reports/example"),
)
frame = prepare(args)  # Namespace 必须作为首个位置参数传入。
```

## 8. 常见错误与排查

| 现象 | 重点检查 |
| --- | --- |
| `@input_session()` 报错 | 使用不带括号的 `@input_session` |
| 请求区间仍为 `(None, None)` | 是否显式传了 `start_day=`，或把含日期属性的对象放在首个位置参数；签名默认值和普通日期位置参数不会自动识别 |
| `Namespace` 有 `start_day` 却报缺少 `end_day` | 两个属性都应存在；区间提取发生在函数体执行及失败报告捕获之前 |
| 成功运行却没有报告 | 是否在会话内主动调用 `write_input_reports` |
| 失败后没有报告 | `report_dir` 是否实际传入；异常是否属于 `Exception`；写报告是否遇到被忽略的写入错误 |
| 同一异常产生多份失败报告 | 内外层装饰器是否都能找到报告目录并逐层捕获异常 |
| 缓存没有复用 | 是否直接调用了 pandas，是否已经退出最外层会话，或手动嵌套了独立会话 |
| `SOURCE_CHANGED` | 文件是否在会话运行期间被覆盖，大小或修改时间是否变化 |
| 异步函数等待时没有会话 | 包装器是同步的，不会等待协程执行；不要直接用于 `async def` 来管理协程内的输入生命周期 |

报告存在或 `issues` 为空，只表示当前已收集的输入记录，不代表行情完整或策略已通过验收。

相关说明：[prepare_role_research](prepare_role_research.md)、[数据准备 API 用法](../../../bomber/framework/dataprep/API_USAGE.md)。相关回归：[公共输入测试](../../../tests/test_dataprep_common.py)。

[返回函数索引](../README.md) · [返回项目文档导航](../../README.md)
