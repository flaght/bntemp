"""共享原生CTP组件的受控故障验收；传输为替身，不连接或操作柜台。"""

import argparse
import hashlib
import json
from pathlib import Path
import time
import unittest


CASES = (
    "test_native_shared_partial_duplicate_cancel_and_peer_replan",
    "test_native_shared_rejection_notifies_both_owners_and_closes_account",
    "test_native_shared_order_cancelled_by_allocation_owner_and_other_target_replanned",
    "test_host_stop_delivers_fill_already_queued_before_unbinding",
    "test_host_stop_waits_for_late_partial_fill_and_cancel_before_unbinding",
    "test_host_stop_timeout_keeps_runner_bound_and_rejects_new_target",
    "test_callback_failure_keeps_event_pending_and_closes_account",
)
SOURCES = (
    "tests/run_shared_account_fault_acceptance.py", "tests/test_shared_account.py",
    "bomber/framework/trader/execution/shared.py",
    "bomber/framework/trader/execution/attribution.py",
    "bomber/framework/trader/execution/ctp/native_driver.py",
    "bomber/framework/trader/execution/live/client.py",
    "bomber/framework/trader/execution/live/controlled.py",
    "bomber/framework/trader/execution/builders.py",
    "bomber/framework/trader/runtime/live/shared.py",
    "bomber/framework/trader/runtime/live/channels/ctp_shared.py",
)


class CaseResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed_cases = set()

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed_cases.add(test.id())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--report-dir", type=Path, default=root / "tests/results")
    args = parser.parse_args(argv)
    path = args.report_dir / f"shared-account-faults-{time.time_ns()}"
    path.mkdir(parents=True, exist_ok=False)
    print("受控传输故障验收：实际共享账户/CTP组件及Runner；SDK类型与柜台传输为替身，不连接柜台", flush=True)
    # 在完整名称上运行，避免把网络测试或其他demo工作混入本专项。
    names = ["tests.test_shared_account.SharedAccountTests." + name for name in CASES]
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    with (path / "tests.txt").open("w", encoding="utf-8") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=CaseResult).run(suite)
    failed = {test.id() for test, _ in result.failures + result.errors}
    skipped = {test.id() for test, _ in result.skipped}
    passed = result.wasSuccessful() and result.testsRun == len(CASES) and not skipped
    summary = {"status": "passed" if passed else "failed", "verification": "controlled_transport_offline",
        "network_connected": False, "real_broker_orders_submitted": 0,
        "tests_run": result.testsRun,
        "cases": [{"name": name, "status": "failed" if name in failed else
                   "skipped" if name in skipped else "passed" if name in result.passed_cases
                   else "not_run"} for name in names],
        "sources": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCES},
        "live_acceptance_remaining": ["SimNow partial fills", "SimNow shared mother cancellation",
                                      "SimNow rejection; controlled responses do not prove broker behavior"]}
    (path / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print((path / "tests.txt").read_text(encoding="utf-8"), end="")
    print(f"记录目录: {path}")
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
