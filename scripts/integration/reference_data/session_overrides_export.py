"""将标准七字段日期例外CSV导出为Feather；不连接数据库或柜台，不覆盖旧文件。"""
import argparse
from pathlib import Path

from bomber.framework.calendars.overrides import normalize_overrides, OVERRIDE_COLUMNS
from bomber.framework.dataprep.sources import ReferenceDataset
from bomber.framework.dataprep.sources.base import ReferenceBatch


def export_overrides(input_path, output_path):
    import pandas as pd
    source, destination = Path(input_path).expanduser().resolve(), Path(output_path).expanduser().resolve()
    if destination.suffix.lower() != ".feather" or destination.exists():
        raise ValueError("日期例外导出须使用新的Feather路径")
    frame = pd.read_csv(source, dtype="string", keep_default_na=False)
    rows = normalize_overrides(frame)
    batch = ReferenceBatch(ReferenceDataset.SESSION_OVERRIDES, rows, OVERRIDE_COLUMNS, str(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        batch.frame().to_feather(stream)
    return {"status": "passed", "source": str(source), "output": str(destination),
            "rows": len(rows), "codes": sorted({row["code"] for row in rows}),
            "sha256": batch.fingerprint}


def main(argv=None):
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = export_overrides(args.input, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


if __name__ == "__main__":
    main()
