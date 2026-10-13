#!/usr/bin/env python3
"""兼容旧入口；实现已迁至 scripts.integration.ctp.td_readonly。"""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.integration.ctp.td_readonly import build_readonly_driver, main

__all__ = ["build_readonly_driver", "main"]


if __name__ == "__main__":
    main()
