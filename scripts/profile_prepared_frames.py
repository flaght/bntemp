"""Measure the shared feature-frame restoration service without running a backtest."""
import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bomber.framework.dataprep.feature_diagnostics import profile_prepared_frames


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-inputs", type=Path, required=True)
    parser.add_argument("--feature-config", type=Path, required=True, help="匹配原产物的公共特征数据集JSON声明")
    parser.add_argument("--verify", choices=("metadata", "full"), default="metadata")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--restore-backend", choices=("auto", "python", "cython"), default="auto",
        help="auto有扩展则使用Cython，否则Python；cython缺少扩展时明确报错")
    parser.add_argument("--output", type=Path, help="另存诊断JSON，必须在只读产物目录之外")
    args = parser.parse_args(argv)
    try:
        if args.output is not None and args.output.resolve().is_relative_to(args.replay_inputs.resolve()):
            raise ValueError("诊断输出不能写入只读产物目录")
        result = profile_prepared_frames(args.replay_inputs, args.feature_config, verify=args.verify,
                                        repeat=args.repeat, backend=args.restore_backend)
        content = json.dumps(result, ensure_ascii=False, indent=2)+"\n"
        if args.output is not None:
            if args.output.resolve() in {args.feature_config.resolve(), Path(result["feature_file"]).resolve(),
                                        Path(result["feature_file"]+".manifest.json").resolve()}:
                raise ValueError("诊断输出不能覆盖特征输入或声明")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
        print(content, end="", flush=True)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
