"""08新策略独立连接CTP；与原run_live入口并存。"""
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from demos.direct_ctp import main

if __name__ == '__main__':
    main('option_vega')
