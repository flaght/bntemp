"""Compare prepared frames against originals by daily partition, without a trading engine."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date
import json
from pathlib import Path
import sys
from time import perf_counter

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bomber.framework.dataprep.bars import read_bar_frame
from bomber.framework.dataprep.contracts import BarFileKey, BarReadSpec, InputPlan
from bomber.framework.dataprep.prepared import PreparedDataset, _sha
from bomber.framework.dataprep.session import InputSession


def compare(dataset, *, start_day=None, end_day=None, progress=None):
    """Bound memory to one date/product/period; assertion work is not a loading benchmark."""
    import pandas as pd
    grouped = defaultdict(list)
    for row in dataset.rows:
        day = date.fromisoformat(row["trading_day"])
        if (start_day is None or day >= start_day) and (end_day is None or day <= end_day):
            grouped[(row["spec_id"], day)].append(row)
    if not grouped:
        raise ValueError("No prepared sources in comparison range")
    files = rows = 0
    for number, ((identifier, day), entries) in enumerate(sorted(grouped.items()), 1):
        values = dict(dataset.manifest["specs"][identifier])
        values["required_fields"] = tuple(values["required_fields"])
        values["field_mapping"] = tuple(tuple(item) for item in values["field_mapping"])
        spec = BarReadSpec(**values)
        kind, product = entries[0]["asset_kind"], entries[0]["product"]
        keys = tuple(BarFileKey(kind, row["symbol"], day) for row in entries)
        prepared = dataset.load(InputPlan(keys, requested=(day, day)), specs={kind: spec},
                                products={kind: product}, copy_frames=False)
        originals = {row["symbol"]: row for row in entries}
        for source in prepared.sources:
            row = originals[source.result.key.symbol]
            if _sha(row["source_path"]) != row["source_sha256"]:
                raise ValueError(f"Raw source changed since snapshot: {row['source_path']}")
            with InputSession():
                expected = read_bar_frame(row["source_path"], spec, source.result.key, copy_frame=False)
                pd.testing.assert_frame_equal(expected.frame, source.result.frame, check_exact=True)
                if (expected.issues != source.result.issues or expected.frame.attrs != source.result.frame.attrs
                        or expected.frame.flags.allows_duplicate_labels != source.result.frame.flags.allows_duplicate_labels):
                    raise AssertionError(f"Prepared diagnostics/metadata differ: {row['source_path']}")
            files += 1
            rows += source.result.frame.shape[0]
        if progress:
            progress(f"[对照进度] partitions={number}/{len(grouped)} sources={files} rows={rows}")
    return dict(source_files_compared=files, rows_compared=rows, equal=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared-root", type=Path, required=True)
    parser.add_argument("--start-day", type=date.fromisoformat)
    parser.add_argument("--end-day", type=date.fromisoformat)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    started = perf_counter()
    try:
        if args.start_day and args.end_day and args.end_day < args.start_day:
            raise ValueError("End date precedes start date")
        dataset = PreparedDataset(args.prepared_root, verify="full")
        result = compare(dataset, start_day=args.start_day, end_day=args.end_day,
                         progress=lambda text: print(text, flush=True))
        result.update(status="passed", seconds=round(perf_counter()-started, 3), dataset=dataset.identity)
        text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            # Comparison evidence is a new artifact, never replace earlier evidence.
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(text)
        print(text, end="")
    except (ValueError, OSError, AssertionError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
