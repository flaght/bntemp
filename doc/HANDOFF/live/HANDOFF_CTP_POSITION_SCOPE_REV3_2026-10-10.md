# 04范围外持仓回归测试生命周期修正（PF2026101037 REV3）

远程REV2部署后，cross-section组62项只剩一个错误：范围外持仓测试在
runner.stop()之后读取transport._api.sent。CtpTdApiTransport.close()会将
_api置为None；这是测试访问已释放引用，不是策略归属校验失败。

本次仅修改tests/test_cross_section_live.py：停机前保存假API对象引用，
停机和恢复后通过该对象核对完整委托记录；前后都断言只有两份组合委托，
并保留没有范围外au2612委托、恢复后范围外仓位未归属的断言。继续执行
实际runner.stop()及manager.restore()，不修改生产停机或仓位逻辑。

本地测试文件编译通过。尝试执行该测试仍在加载阶段失败：本机无法导入
bomber.framework.fixes.Bar，未执行原生测试正文；62项结果需要远程复测。
日志中的过期Bar跳过是已有测试场景，不是此次异常来源。

此前已完成REV2同步；本次仅同步测试文件和本交接，新摘要为
doc/evidence/checksums/PF2026101037_REV3_SHA256.txt。历史清单保留。

```bash
sha256sum -c doc/evidence/checksums/PF2026101037_REV3_SHA256.txt
python -u -m tests.run_cross_section_acceptance --only cross-section
```
