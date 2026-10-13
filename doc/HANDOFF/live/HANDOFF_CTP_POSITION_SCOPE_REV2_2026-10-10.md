# CTP策略范围部署基线修正（PF2026101037 REV2）

2026-10-10。远程cross-section组执行62项，出现两个错误：恢复仍要求全账户
总仓与保存组合一致；失败恢复停机结果缺少resume_state_preserved。
远程栈指向demos/04_cross_section/recovery.py第52行的旧全账户检查；
本地该文件只有35行，validate_account委托公共validate_ctp_account，
ResumeLifecycle继承公共CtpResumeLifecycle。两个错误对应旧恢复文件未同步。

PF2026101037增量清单假设PF1036恢复基线已经同步，未列出这个必要依赖。
本次补齐清单，不删除归属校验，也不修改PF1037生产范围逻辑。04仍只管理
参考资料绑定的合约；范围外账户事实保留为未归属仓位，不能用于04下单。

## 本次修改与必要同步依赖

- tests/run_cross_section_acceptance.py：测试前确认04 ResumeLifecycle继承公共
  CtpResumeLifecycle，混用旧恢复文件时提前报告部署版本不一致及REV2清单位置。
- tests/test_live_recovery.py：实际加载04恢复包装，检查公共继承关系，并执行
  失败恢复停机，确认resume_state_preserved及不保存、不进入常规停机流程。
- demos/04_cross_section/recovery.py：本轮未修改源码，但必须同步本地35行版本；
  原PF1037增量清单漏列此PF1036依赖。
- 本交接文档及doc/evidence/checksums/PF2026101037_REV2_SHA256.txt：补齐当前
  一致版本的同步摘要，保留PF2026101037原清单。

REV2摘要覆盖原清单12个路径的当前内容，加04恢复文件、验收入口及本交接，
共15个文件。已同步的文件核对摘要即可；未匹配的文件同步后再测。不打包。

## 验证与边界

本机81项纯Python公共回归通过，包含新增的04恢复包装测试；REV2清单中
10个Python文件编译通过。验收入口SOURCES路径存在且不重复，包含04恢复文件
及公共恢复模块。摘要逐项核对通过。

本机原生Bar实体无法加载，尚未执行本次远程62项cross-section组。当前远程
结果不是通过证据；应先统一文件再复测失败组。本轮未连接柜台或报单。

在远程pro目录执行：

```bash
sha256sum -c doc/evidence/checksums/PF2026101037_REV2_SHA256.txt
python -u -m tests.run_cross_section_acceptance --only cross-section
```

失败组通过后，再继续此前被停止的验收组；新81项公共测试可单独运行：

```bash
python -u -m tests.run_cross_section_acceptance --only live-common
```
