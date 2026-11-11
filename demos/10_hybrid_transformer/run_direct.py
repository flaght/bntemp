"""10进程内运行原RB/P策略并直连CTP；接续既有成交归属和信号。"""
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from demos.direct_ctp import main

if __name__ == '__main__':
    main('hybrid_transformer')
