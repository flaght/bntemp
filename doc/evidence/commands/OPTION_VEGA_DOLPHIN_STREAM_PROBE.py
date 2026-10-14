"""Compatibility entry for the read-only DolphinDB minute-push probe."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from scripts.integration.options.dolphin_stream_probe import main

if __name__ == "__main__":
    sys.exit(main())
