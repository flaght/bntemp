# 函数说明索引

本目录集中说明函数的用途、参数、输入文件、返回结果和使用约束。按模块分类，每个函数单独一篇文档，文件名使用函数名，方便按名称和参数检索。

## 函数列表

| 函数 | 所属模块 | 用途 | 查询关键词 |
| --- | --- | --- | --- |
| [prepare_role_research](dataprep/prepare_role_research.md) | `bomber.framework.dataprep.scenarios.role_futures` | 准备期货合约角色、行情时间范围和累计复权因子快照 | 主力、次主力、角色表、分钟时间标签、复权、factor_availability |

## 目录约定

```text
doc/functions/
├── README.md
└── dataprep/
    └── prepare_role_research.md
```

新增说明时按实际模块建立分类，例如 `dataprep`、`datahub`、`market`、`trader`；同名函数来自不同模块时，在所属模块的分类目录内区分，不把多份解释堆在同一个文件中。

## 如何查询

在项目根目录执行：

```bash
rg --files doc/functions
rg -n 'prepare_role_research|factor_availability' doc/functions
```

每篇说明应包含：

1. 完整导入路径、源码链接及核对日期。
2. 函数用途和适用场景。
3. 完整签名、参数类型、默认值、参数之间的约束。
4. 输入数据格式与时间约定。
5. 返回结果及调用示例。
6. 常见错误、容易混淆的概念及相关调用入口。

修改函数行为时同步更新对应说明，新增文档时同步更新本索引。示例声明的假设、源码默认值和某个策略入口的默认值应分别写明。

[返回项目文档导航](../README.md)
