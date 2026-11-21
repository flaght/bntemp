"""Immutable, certified native-input and decision-frame artifacts.

This is a derivative of a prepared snapshot, not a replacement for its audited
normalization data. Publishing requires the installed native SDK; ordinary runs
only restore certified arrays and the frame selections computed by that SDK.
"""
from collections import defaultdict
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from .contracts import BarLoadResult, HistoricalInputBundle, InputContext, SourceSpec
from .columnar import CertifiedArrowFrame
from .prepared import _artifact, _sha, _json, _source_id, _measure, _count
from .session import current_session

FORMAT = "bomber-replay-inputs-v1"
FIELDS = ("open", "high", "low", "close", "volume", "event_ns", "available_ns")


def source_key(key, spec):
    return f"{key.asset_kind}:{key.symbol}:{key.trading_day}:{spec.bar_spec}"


@lru_cache(maxsize=1)
def implementation_identity():
    base = Path(__file__).resolve().parents[1]
    names = ("dataprep/replay_inputs.py", "dataprep/columnar.py", "datahub/features/prepared_frames.py",
             "datahub/features/restoration.py", "datahub/features/_restoration_native.pyx",
             "market/replay/native_batch.py", "market/basic/base.py", "datahub/features/frames.py",
             "market/replay/parsers/bar/fixed.py", "market/replay/parsers/base.py", "datahub/temporal.py",
             "market/frames.py", "market/replay/base.py",
             "datahub/features/replay.py", "datahub/features/store.py", "datahub/features/alignment.py",
             "datahub/features/contracts.py", "datahub/features/schema.py",
             "dataprep/features/file.py", "dataprep/features/base.py", "dataprep/features/codec.py")
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        digest.update((base/name).read_bytes())
    from bomber.framework.datahub.features.restoration import native_restoration_module
    native = native_restoration_module()
    if native is not None:
        digest.update(b"compiled-observation-restoration")
        digest.update(_sha(Path(native.__file__)).encode())
    return digest.hexdigest()


@lru_cache(maxsize=1)
def native_identity():
    """Once per process; binary hashes bind the actual conversion implementation."""
    from importlib import import_module
    from bomber.framework.market.basic.base import Bar, Price, Quantity
    modules = sorted({Bar.__module__, Price.__module__, Quantity.__module__})
    binaries = {}
    for name in modules:
        path = Path(import_module(name).__file__)
        binaries[name] = {"filename": path.name, "sha256": _sha(path)}
    return {"binaries": binaries, "price_unit_raw": str(Price.from_str("1").raw),
            "quantity_unit_raw": str(Quantity.from_str("1").raw)}


def artifact_context(dataset, plan, specs, feature_identity, frame_rules, frame_implementation):
    with _measure("replay_input_identity"):
        parent = dataset.identity
        keys = sorted(source_key(key, specs[key.asset_kind]) for key in plan.required)
        return {"parent_snapshot_id": parent["snapshot_id"], "parent_manifest_sha256": parent["manifest_sha256"],
                "requested": [str(day) for day in plan.requested],
                "source_keys_sha256": hashlib.sha256(_json(keys).encode()).hexdigest(),
                "source_count": len(keys), "feature_input": feature_identity, "frame_rules": frame_rules,
                "frame_implementation": frame_implementation, "implementation": implementation_identity(),
                "native": native_identity(), "source_order": "trading-day-symbol-v1"}


@contextmanager
def publish_replay_inputs(output, context, *, parent_root):
    """New output only, single publisher, atomic installation; no partial package."""
    output = Path(output).expanduser().resolve()
    parent_root = Path(parent_root).resolve()
    if output == parent_root or output.is_relative_to(parent_root):
        raise ValueError("Replay artifacts must be outside the immutable prepared snapshot")
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.parent/(output.name+".prepare.lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    stage = None
    try:
        os.close(descriptor)
        if output.exists():
            raise FileExistsError(f"Choose a new replay-input version: {output}")
        stage = Path(tempfile.mkdtemp(prefix="."+output.name+"-", dir=output.parent))
        writer = ReplayInputWriter(stage, context)
        yield writer
        writer.finish()
        os.rename(stage, output)
        stage = None
    finally:
        if stage is not None:
            shutil.rmtree(stage)
        lock.unlink(missing_ok=True)


class ReplayInputWriter:
    def __init__(self, root, context):
        self.root, self.context = root, context
        self.artifacts = {}
        self.native_rows = []
        self.frames_written = False
        self.package_id = uuid4().hex

    def write_table(self, name, table):
        import pyarrow.feather as feather
        path = _artifact(self.root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        feather.write_feather(table, path, compression="lz4", version=2)
        self.artifacts[name] = {"size": path.stat().st_size, "sha256": _sha(path), "rows": len(table),
            "schema_sha256": hashlib.sha256(table.schema.serialize().to_pybytes()).hexdigest()}

    @staticmethod
    def bar_signature(bar):
        return (str(bar.bar_type), bar.ts_event, bar.ts_init,
                *((getattr(bar, name).raw, getattr(bar, name).precision) for name in FIELDS[:5]))

    def write_native(self, sources, instruments, get_meta, *, progress=None):
        """Round/check once and certify every row against the old native factory."""
        import pyarrow as pa
        from bomber.framework.market.replay.native_batch import NativeBarBatchParser
        from bomber.framework.market.replay.parsers.bar.fixed import PreparedBarParser
        from bomber.framework.market.replay.parsers.base import ParserContext
        groups = defaultdict(list)
        for source in sources:
            result = source.result
            groups[(result.key.asset_kind, result.spec.bar_spec, result.spec.timezone,
                    result.key.trading_day)].append(result)
        by_source = {}
        for number, (_, results) in enumerate(sorted(groups.items()), 1):
            tables, offset = [], 0
            name = f"native/{number:06d}.feather"
            for result in sorted(results, key=lambda r: (r.key.trading_day, r.key.symbol)):
                instrument = instruments[result.key.symbol]
                context = ParserContext(result.path, 1, get_meta)
                parser = NativeBarBatchParser(instrument, result.spec.bar_spec, result.spec.timezone)
                arrays, reason = parser.prepare_arrays(result.frame, context)
                if reason is not None:
                    raise ValueError(f"Cannot certify {result.key}: {reason}; use ordinary native_batch")
                from bomber.framework.market.basic.base import Bar
                meta = get_meta(instrument)
                bars = Bar.from_raw_arrays_to_list(parser._bar_type, meta.price_precision, meta.size_precision, *arrays)
                if len(bars) != len(result.frame):
                    raise ValueError(f"Native row count differs: {result.key}")
                original = [result.frame.array(field) for field in FIELDS]
                old = PreparedBarParser(instrument, bar_spec=result.spec.bar_spec)
                for line, (values, bar) in enumerate(zip(zip(*original), bars), 1):
                    row = {field: value.item() if hasattr(value, "item") else value
                           for field, value in zip(FIELDS, values)}
                    expected = tuple(old.parse(row, ParserContext(result.path, line, get_meta)))
                    if len(expected) != 1 or self.bar_signature(expected[0].payload) != self.bar_signature(bar):
                        raise ValueError(f"Native conversion differs: {result.path}:{line}")
                table = pa.table(dict(zip(FIELDS, (pa.array(array) for array in arrays))))
                # Preserve completed times for the existing entry-quote check.
                table = table.append_column("datetime", table.column("event_ns").cast(pa.int64()).cast(
                    pa.timestamp("ns", tz=result.spec.timezone)))
                tables.append(table)
                self.native_rows.append({"source_key": source_key(result.key, result.spec), "file": name,
                    "offset": offset, "rows": len(table), "instrument_id": str(instrument),
                    "bar_spec": result.spec.bar_spec, "price_precision": meta.price_precision,
                    "size_precision": meta.size_precision})
                offset += len(table)
                by_source[(result.key.trading_day, result.key.symbol)] = bars
            self.write_table(name, pa.concat_tables(tables))
            if progress:
                progress(f"[原生输入准备] partitions={number}/{len(groups)} sources={len(self.native_rows)}")
        self.write_table("native-index.feather", pa.Table.from_pylist(self.native_rows))
        events = [bar for key in sorted(by_source) for bar in by_source[key]]
        # Stable sort is the existing ts_init + source-registration sequence rule.
        events.sort(key=lambda bar: bar.ts_init)
        return tuple(events)

    def write_frames(self, outcomes, deadlines, records, binding):
        from bomber.framework.datahub.features.prepared_frames import encode_frames
        frames, observations = encode_frames(outcomes, deadlines, records, binding)
        self.write_table("frames.feather", frames)
        self.write_table("observations.feather", observations)
        self.frames_written = True

    def finish(self):
        if not self.native_rows or not self.frames_written:
            raise ValueError("Native inputs and frame outcomes must both be prepared")
        if len(self.native_rows) != self.context["source_count"]:
            raise ValueError("Prepared source count differs from its bound loading plan")
        manifest = {"format": FORMAT, "package_id": self.package_id, "context": self.context,
                    "artifacts": self.artifacts, "sources": len(self.native_rows),
                    "rows": sum(row["rows"] for row in self.native_rows),
                    "certification": "Every native OHLCV raw value, precision and both clocks compared with row parser"}
        (self.root/"manifest.json").write_text(_json(manifest)+"\n", encoding="utf-8")


class ReplayInputs:
    def __init__(self, root, expected_context, *, verify="metadata"):
        if verify not in {"metadata", "full"}:
            raise ValueError("Replay-input verification must be metadata/full")
        self.root, self.verify = Path(root).expanduser().resolve(), verify
        with _measure("replay_inputs_open"):
            manifest_path = self.root/"manifest.json"
            session = current_session()
            if session:
                session.fingerprint(manifest_path)
            content = manifest_path.read_bytes()
            self.manifest_digest = hashlib.sha256(content).hexdigest()
            self.manifest = json.loads(content)
            if self.manifest.get("format") != FORMAT or self.manifest.get("context") != expected_context:
                raise ValueError("Replay-input data/precision/feature/configuration version changed; prepare a new package")
            index = self.read_table("native-index.feather", full=True).to_pylist()
            self.by_source = {row["source_key"]: row for row in index}
            if len(index) != len(self.by_source) or len(index) != self.manifest["sources"]:
                raise ValueError("Replay-input index count or identity differs")
            if sum(row["rows"] for row in index) != self.manifest["rows"]:
                raise ValueError("Replay-input index row count differs")
            ranges = defaultdict(list)
            for row in index:
                if row["offset"] < 0 or row["rows"] < 0:
                    raise ValueError("Negative replay-input source range")
                ranges[row["file"]].append((row["offset"], row["offset"]+row["rows"]))
            for name, spans in ranges.items():
                end = 0
                for start, stop in sorted(spans):
                    if start != end:
                        raise ValueError("Replay-input source ranges overlap or have gaps")
                    end = stop
                if end != self.manifest["artifacts"][name]["rows"]:
                    raise ValueError("Replay-input source ranges do not cover partition")
            if session:
                session.fingerprint(manifest_path)

    def read_table(self, name, *, full=False):
        import pyarrow.feather as feather
        artifact = self.manifest["artifacts"][name]
        path = _artifact(self.root, name)
        session = current_session()
        if session:
            session.fingerprint(path)
        if path.stat().st_size != artifact["size"]:
            raise ValueError(f"Replay-input file size differs: {path}")
        if (full or self.verify == "full") and _sha(path) != artifact["sha256"]:
            raise ValueError(f"Replay-input SHA256 differs: {path}")
        table = feather.read_table(path, memory_map=True)
        if (len(table) != artifact["rows"] or
                hashlib.sha256(table.schema.serialize().to_pybytes()).hexdigest() != artifact["schema_sha256"]):
            raise ValueError(f"Replay-input schema/row count differs: {path}")
        if session:
            session.fingerprint(path)
        return table

    @property
    def identity(self):
        return {"root": str(self.root), "package_id": self.manifest["package_id"],
                "manifest_sha256": self.manifest_digest, "verification": self.verify}

    def load_selected(self, dataset, selected, coverage, plan, specs, context):
        groups = defaultdict(list)
        wanted = set()
        for key, parent_row in selected.items():
            identity = source_key(key, specs[key.asset_kind])
            wanted.add(identity)
            if identity not in self.by_source:
                raise ValueError(f"Replay inputs missing required source: {identity}")
            row = self.by_source[identity]
            if row["rows"] != parent_row["rows"]:
                raise ValueError(f"Replay-input source row count differs: {identity}")
            groups[row["file"]].append((key, parent_row, row))
        if wanted != set(self.by_source):
            raise ValueError("Replay-input source universe changed; prepare a new package")
        results = {}
        with _measure("native_ready_load"):
            for name, rows in sorted(groups.items()):
                table = self.read_table(name)
                _count("native_ready_files_read")
                for key, parent_row, row in rows:
                    if row["offset"] < 0 or row["rows"] < 0 or row["offset"]+row["rows"] > len(table):
                        raise ValueError("Replay-input source range exceeds partition")
                    certificate = {field: row[field] for field in
                        ("instrument_id", "bar_spec", "price_precision", "size_precision")}
                    frame = CertifiedArrowFrame(table.slice(row["offset"], row["rows"]), certificate=certificate)
                    result = BarLoadResult(key, _artifact(self.root, name), frame, specs[key.asset_kind],
                                           tuple(dataset._issues[_source_id(parent_row)]))
                    dataset._record(parent_row, result)
                    results[key] = SourceSpec(result)
        session = current_session()
        if session:
            actual, dependency = set(session.coverage.actual_days), set(session.coverage.dependency_days)
            for key in results:
                target = dependency if ((plan.requested[0] is not None and key.trading_day < plan.requested[0])
                    or (plan.requested[1] is not None and key.trading_day > plan.requested[1])) else actual
                target.add(key.trading_day)
            session.coverage.actual_days, session.coverage.dependency_days = tuple(sorted(actual)), tuple(sorted(dependency))
        return HistoricalInputBundle(context or InputContext(), tuple(results[key] for key in sorted(results)),
                                     plan.bindings, coverage)

    def load_frames(self, records, binding, *, backend="auto"):
        from bomber.framework.datahub.features.prepared_frames import decode_frames
        with _measure("prepared_frames_load"):
            with _measure("prepared_frames_table_read"):
                frames = self.read_table("frames.feather")
            with _measure("prepared_observations_table_read"):
                observations = self.read_table("observations.feather")
            session = current_session()
            with _measure("prepared_frames_decode"):
                return decode_frames(frames, observations, records, binding,
                                     performance=None if session is None else session.performance, backend=backend)
