"""公共启动/恢复/停机及检查点接入回归；无外部连接，不构造原生 EMA。"""
import sys
import unittest

from tests import run_live_history_offline as fixtures


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromNames([
        "tests.test_live_lifecycle",
        "tests.test_live_recovery",
        "tests.test_live_common",
        "tests.test_live_channels",
        "tests.test_execution_composition",
        "tests.test_live_readiness",
        "tests.test_option_vega_assembly",
        "tests.test_shared_account",
        "tests.test_option_shared_rollover",
        "tests.test_shared_continuous",
    ])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
