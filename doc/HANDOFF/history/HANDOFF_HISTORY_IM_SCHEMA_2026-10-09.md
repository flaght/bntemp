# 历史行情先对接IM股指：Schema记录

2026-10-09，用户要求“先用股指”，截图查询Code like IM%，展示IM2611。首先对接IM，不把截图中的IM2611自动认定为当前主力。

已由截图确认：数据库`dfs://min_bar`，表`cffex_1min`；合约字段Code，日期date，分钟时间minTime；价格open/high/low/close；累计量相关volume/turnover，持仓量open_interest。截图不能证明DolphinDB实际字段类型或历史因子口径。

本地已有[读取/导出代码](../../../examples/opts2/kline_task.py)和[导出说明](../../../examples/opts2/EXPORT.md)，同一表按date+minTime拼接北京时间；源volume/turnover按交易日累计值差分。不能把盘中首条累计量直接当作该分钟增量，须取得此前基准。内部执行合约统一CFFEX；已有导出文件使用资料编码CCFX，须显式标准化，不能原样当作执行Venue。

用户随后已确认minTime=09:30标识09:30—09:31开始，先验证固定合约原始行情，再接fut_adjustment_factors。PF2026100912新增date/time只读适配及真实配置，详见[操作说明](HANDOFF_HISTORY_IM_READONLY_2026-10-09.md)。不假设因子为1，这不等于连续EMA已完成预热。

PF0911时DolphinDbHistoryProvider仅处理单时间戳列；PF0912新增date+minTime及北京时间转UTC完整分钟末源码，待远程验收。run_live历史窗口/持续日历仍限定CU，入口Venue限定SHFE/INE；CFFEX股指会话日历和入口待后续接入。不能把CU会话/规格/金额预算直接用于IM，也不因同一CTP通道就宣称实际CFFEX执行已验收。

本轮仅读取代码、记录用户选择及确认项，不修改公共源码，不运行本机Python，不连接库/柜台。已有PF0911离线证据保留；后续公共修改须另登记并远程验证。
