"""Prepare 08 native arrays and minute-frame matches using the exact backtest CLI.

All remaining arguments are forwarded to run_backtest, including config-file/id.
"""
import argparse
from importlib import import_module
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="新的只读回放输入版本目录")
    selected, remaining = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    forwarded = ["prepare_replay_inputs.py", *remaining, "--data-mode", "prepared",
                 "--bar-parser", "native_batch", "--prepare-replay-inputs", str(selected.output)]
    previous = sys.argv
    try:
        sys.argv = forwarded
        import_module("demos.08_option_vega.run_backtest").main()
    finally:
        sys.argv = previous


if __name__ == "__main__":
    main()
