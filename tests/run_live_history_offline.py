"""公共历史协调离线入口；不连接数据库/柜台，不构造原生 EMA 数值算法。"""
import sys
import unittest
from importlib import import_module

# 复用上一个模块的 SDK 导入夹具；实际历史服务、策略和协调器均参与运行。
from tests import run_live_continuous_offline as fixtures

# 为现有 SDK 夹具补齐真实 Python 分钟聚合器的公共导出。
stream = sys.modules["bomber.framework.market.stream"]
aggregation = import_module(stream.__name__ + ".aggregation")
for name in ("TradeTickBarFeed", "ReceiveTimeTradeTickBarFeed"):
    setattr(stream, name, getattr(aggregation, name))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        "tests.test_live_history",
        "tests.test_calendar_spread_history",
        "tests.test_cross_section_history",
        "tests.test_hybrid_history_source",
        "tests.test_history_session_factors",
        "tests.test_calendar_spread_live.LiveAssemblyTests.test_recording_history_hook_and_buffer_do_not_submit_until_next_live_frame",
        "tests.test_calendar_spread_live.LiveAssemblyTests.test_failed_history_local_lifecycle_keeps_decisions_closed",
        "tests.test_cross_section_live.CrossSectionLiveTests.test_cffex_recording_history_builds_without_td_and_keeps_uppercase_symbols",
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
