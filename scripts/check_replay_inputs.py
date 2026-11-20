"""Full artifact integrity audit; current data/config/SDK binding is checked by backtest."""
import argparse
import json
from pathlib import Path
import sys
from time import perf_counter

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bomber.framework.dataprep.replay_inputs import ReplayInputs, implementation_identity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-inputs", type=Path, required=True)
    args = parser.parse_args(argv)
    started = perf_counter()
    try:
        manifest = json.loads((args.replay_inputs/"manifest.json").read_text(encoding="utf-8"))
        if manifest["context"]["implementation"] != implementation_identity():
            raise ValueError("回放准备代码版本已改变，须重新生成产物")
        artifact = ReplayInputs(args.replay_inputs, manifest["context"], verify="full")
        for name in manifest["artifacts"]:
            artifact.read_table(name, full=True)
        print(json.dumps({"status": "passed", "seconds": round(perf_counter()-started, 3),
            "artifact": artifact.identity, "sources": manifest["sources"], "rows": manifest["rows"],
            "scope": "完整文件SHA256、Schema、行数；实际行情/特征/参数/SDK匹配在回测入口核对"},
            ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
