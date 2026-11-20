"""Versioned, offline-validated Feather partitions; no native engine dependency.

Only this module's publisher creates snapshots. Readers use their explicit immutable
version, not an automatically refreshed view of the original market directories.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4

from .bars import read_bar_frame
from .catalog import scan_bar_files, select_bar_files
from .contracts import (BarFileKey, BarLoadResult, BarReadSpec, HistoricalInputBundle,
                        InputContext, InputIssue, SourceSpec)
from .session import InputSession, current_session, fail, json_value, profile_input, record_issue

FORMAT = "bomber-prepared-bars-v1"
SYMBOL_COLUMN = "__prepared_symbol"
INDEX_COLUMNS = ("asset_kind", "product", "symbol", "trading_day", "spec_id", "data_file",
                 "offset", "rows", "first_ns", "last_ns", "source_path", "source_size",
                 "source_mtime_ns", "source_sha256", "columns", "attrs", "duplicate_labels", "assumptions")
ISSUE_COLUMNS = ("source_id", "issue_json")


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalization_fingerprint():
    """Conservatively invalidate snapshots when the normalization contract changes."""
    root = Path(__file__).parent
    return hashlib.sha256(b"".join((root / name).read_bytes()
                                 for name in ("bars.py", "contracts.py"))).hexdigest()


def _spec_value(spec):
    return json.loads(_json(asdict(spec)))


def _source_id(row):
    return f"{row['spec_id']}:{row['symbol']}:{row['trading_day']}"


def _measure(name):
    session = current_session()
    return session.performance.measure(name) if session else nullcontext()


def _count(name, value=1):
    session = current_session()
    if session:
        session.performance.count(name, value)


def _artifact(root, name):
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        fail("INVALID_PREPARED_DATA", "Artifact path must stay inside snapshot", source=name)
    result = (root / path).resolve()
    if not result.is_relative_to(root):
        fail("INVALID_PREPARED_DATA", "Artifact symlink escapes snapshot", source=name)
    return result


@dataclass(frozen=True)
class PreparedSource:
    asset_kind: str
    root: Path
    product: str
    spec: BarReadSpec

    def __post_init__(self):
        if self.asset_kind not in {"future", "option"}:
            raise ValueError("Prepared bars support future/option sources")
        product = self.product.strip().upper()
        if re.fullmatch(r"[A-Z]+", product) is None:
            raise ValueError("Product must contain ASCII letters")
        if self.spec.value_policy != "execution_strict":
            raise ValueError("Prepared snapshots require execution_strict normalization")
        object.__setattr__(self, "product", product)
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    @property
    def spec_id(self):
        return f"{self.asset_kind}:{self.product}:{self.spec.bar_spec}"


def _write_table(path, table):
    import pyarrow.feather as feather
    path.parent.mkdir(parents=True, exist_ok=True)
    feather.write_feather(table, path, compression="lz4", version=2)
    return {"size": path.stat().st_size, "sha256": _sha(path)}


def build_prepared_dataset(sources, output, *, start_day, end_day, base_snapshot=None,
                           progress=None):
    """Publish a new snapshot atomically; originals and existing snapshots stay intact.

    An optional base reuses unchanged daily partitions. Source contents are hashed
    during preparation, including incremental preparation; this cost is separate
    from backtests. One InputSession per original file bounds raw-frame cache RAM.
    """
    import pyarrow as pa
    sources = tuple(sources)
    if not sources or end_day < start_day:
        raise ValueError("Declare sources and a valid preparation date range")
    if len({s.spec_id for s in sources}) != len(sources):
        raise ValueError("Duplicate prepared source product/period")
    output = Path(output).expanduser().resolve()
    if any(output == s.root or output.is_relative_to(s.root) for s in sources):
        raise ValueError("Prepared output must be outside original market directories")
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.parent / (output.name + ".prepare.lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    stage = None
    try:
        os.close(descriptor)
        if output.exists():
            raise FileExistsError(f"Choose a new snapshot version: {output}")
        stage = Path(tempfile.mkdtemp(prefix="." + output.name + "-", dir=output.parent))
        base = PreparedDataset(base_snapshot, verify="full") if base_snapshot else None
        if base is not None:
            base.verify_all()
        specs = {s.spec_id: _spec_value(s.spec) for s in sources}
        partitions = defaultdict(list)
        for source in sources:
            with InputSession():
                inventory = scan_bar_files(source.root, source.asset_kind, start_day, end_day)
            pattern = re.compile(re.escape(source.product) + r"\d")
            matches = [(key, path) for key, path in inventory.items() if pattern.match(key.symbol)]
            if not matches:
                raise FileNotFoundError(f"No {source.product} bars in {source.root} for requested dates")
            for key, path in matches:
                partitions[(source.spec_id, key.trading_day)].append((source, key, path))

        snapshot_id = uuid4().hex
        index, issues, artifacts = [], [], {}
        reused = 0
        for number, ((spec_id, day), entries) in enumerate(sorted(partitions.items()), 1):
            entries.sort(key=lambda item: item[1])
            fingerprints = {}
            for _, key, path in entries:
                before = path.stat()
                content = _sha(path)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    fail("SOURCE_CHANGED", "Input changed during preparation", source=path)
                fingerprints[key] = dict(source_path=str(path), source_size=after.st_size,
                    source_mtime_ns=after.st_mtime_ns, source_sha256=content)
            old_rows = ([] if base is None else [row for row in base.rows
                if row["spec_id"] == spec_id and row["trading_day"] == day.isoformat()])
            old_by_symbol = {r["symbol"]: r for r in old_rows}
            reusable = (bool(old_rows) and base.manifest["specs"].get(spec_id) == specs[spec_id]
                and set(old_by_symbol) == {key.symbol for _, key, _ in entries}
                and all(all(old_by_symbol[key.symbol][name] == value
                            for name, value in fingerprints[key].items()) for _, key, _ in entries))
            if reusable:
                for name in sorted({r["data_file"] for r in old_rows}):
                    target = _artifact(stage, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(_artifact(base.root, name), target)
                    artifacts[name] = base.manifest["artifacts"][name]
                index.extend(old_rows)
                selected_ids = {_source_id(r) for r in old_rows}
                issues.extend(row for row in base.issue_rows if row["source_id"] in selected_ids)
                reused += 1
            else:
                groups = {}
                for source, key, path in entries:
                    before = fingerprints[key]
                    with InputSession() as session:
                        result = read_bar_frame(path, source.spec, key, copy_frame=False)
                        table = pa.Table.from_pandas(result.frame, preserve_index=False)
                        if SYMBOL_COLUMN in table.column_names:
                            raise ValueError(f"Reserved prepared field: {SYMBOL_COLUMN}")
                        # Schemas are not promoted: a float column in a sibling
                        # file must never truncate another file's large integers.
                        schema_id = hashlib.sha256(table.schema.serialize().to_pybytes()).hexdigest()
                        group = groups.setdefault(schema_id, [])
                        row = dict(asset_kind=key.asset_kind, product=source.product, symbol=key.symbol,
                            trading_day=day.isoformat(), spec_id=spec_id, rows=len(result.frame),
                            first_ns=result.first_ns, last_ns=result.last_ns, **before,
                            columns=_json(list(result.frame.columns)), attrs=_json(result.frame.attrs),
                            duplicate_labels=result.frame.flags.allows_duplicate_labels,
                            assumptions=_json(session.coverage.assumptions))
                        group.append((row, table.append_column(SYMBOL_COLUMN,
                            pa.array([key.symbol] * len(table), type=pa.string()))))
                        issues.extend(dict(source_id=_source_id(row), issue_json=_json(json_value(issue)))
                                      for issue in session.issues)
                    after = path.stat()
                    if (after.st_size, after.st_mtime_ns) != (before["source_size"], before["source_mtime_ns"]):
                        fail("SOURCE_CHANGED", "Input changed during preparation", source=path)
                for shard, (schema_id, group) in enumerate(sorted(groups.items())):
                    source = entries[0][0]
                    folder = "fut" if source.asset_kind == "future" else "opt"
                    suffix = "" if shard == 0 else "." + schema_id[:12]
                    name = f"{folder}/{source.product}/{source.spec.bar_spec}/{day:%Y%m%d}{suffix}.feather"
                    table = pa.concat_tables([item[1] for item in group])
                    metadata = dict(table.schema.metadata or {})
                    metadata.update({b"prepared_format": FORMAT.encode(),
                                     b"prepared_partition_id": snapshot_id.encode()})
                    table = table.replace_schema_metadata(metadata)
                    artifact = _write_table(_artifact(stage, name), table)
                    artifact.update(partition_id=snapshot_id, rows=len(table),
                        schema_sha256=hashlib.sha256(table.schema.serialize().to_pybytes()).hexdigest())
                    artifacts[name] = artifact
                    offset = 0
                    for row, _ in group:
                        row.update(data_file=name, offset=offset)
                        offset += row["rows"]
                        index.append(row)
            if progress:
                progress(f"[准备进度] partitions={number}/{len(partitions)} date={day} "
                         f"sources={len(index)} reused_partitions={reused}")

        index.sort(key=lambda r: (r["asset_kind"], r["symbol"], r["trading_day"]))
        artifacts["index.feather"] = _write_table(stage / "index.feather", pa.table({
            name: [row[name] for row in index] for name in INDEX_COLUMNS}))
        artifacts["issues.feather"] = _write_table(stage / "issues.feather", pa.table({
            name: pa.array([row[name] for row in issues], type=pa.string()) for name in ISSUE_COLUMNS}))
        manifest = dict(format=FORMAT, snapshot_id=snapshot_id,
            created_at=datetime.now(timezone.utc).isoformat(), normalizer=normalization_fingerprint(),
            requested=[start_day.isoformat(), end_day.isoformat()], specs=specs, artifacts=artifacts,
            source_files=len(index), rows=sum(r["rows"] for r in index),
            partitions=len(partitions), reused_partitions=reused,
            base_snapshot=None if base is None else base.identity,
            integrity="index/issues SHA256; partition size/schema/identity on read; optional full SHA256",
            snapshot_semantics="Explicit immutable dataset; raw source updates require a new preparation version")
        (stage / "manifest.json").write_text(_json(manifest) + "\n", encoding="utf-8")
        os.rename(stage, output)
        stage = None
        return manifest
    finally:
        if stage is not None:
            shutil.rmtree(stage)
        lock.unlink(missing_ok=True)


class PreparedDataset:
    """Read only selected partitions, then restore each source frame without normalization."""
    @profile_input("prepared_snapshot_open")
    def __init__(self, root, *, verify="metadata"):
        import pyarrow.feather as feather
        if verify not in {"metadata", "full"}:
            raise ValueError("prepared verification must be metadata/full")
        self.root = Path(root).expanduser().resolve()
        self.verify = verify
        manifest_path = self.root / "manifest.json"
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if self.manifest.get("format") != FORMAT:
            fail("INVALID_PREPARED_DATA", "Unsupported prepared format", source=manifest_path)
        if self.manifest.get("normalizer") != normalization_fingerprint():
            fail("PREPARED_VERSION_MISMATCH", "Normalizer changed; rebuild prepared snapshot", source=manifest_path)
        for name in ("index.feather", "issues.feather"):
            self._check_artifact(name, full=True)
        with _measure("prepared_index_read"):
            self.rows = feather.read_table(self.root / "index.feather").to_pylist()
            self.issue_rows = feather.read_table(self.root / "issues.feather").to_pylist()
        self._by_key = {}
        intervals = defaultdict(list)
        for row in self.rows:
            if set(row) != set(INDEX_COLUMNS):
                fail("INVALID_PREPARED_DATA", "Prepared index schema differs", source=manifest_path)
            key = BarFileKey(row["asset_kind"], row["symbol"], date.fromisoformat(row["trading_day"]))
            identity = (row["spec_id"], key)
            if identity in self._by_key or row["rows"] < 0 or row["offset"] < 0:
                fail("INVALID_PREPARED_DATA", "Duplicate source or invalid row range", source=manifest_path)
            artifact = self.manifest["artifacts"][row["data_file"]]
            if row["offset"] + row["rows"] > artifact["rows"]:
                fail("INVALID_PREPARED_DATA", "Source range exceeds partition", source=manifest_path)
            intervals[row["data_file"]].append((row["offset"], row["offset"] + row["rows"]))
            _artifact(self.root, row["data_file"])
            self._by_key[identity] = row
        for name, spans in intervals.items():
            ordered = sorted(spans)
            if (ordered[0][0] != 0 or ordered[-1][1] != self.manifest["artifacts"][name]["rows"]
                    or any(left[1] != right[0] for left, right in zip(ordered, ordered[1:]))):
                fail("INVALID_PREPARED_DATA", "Partition source ranges overlap or have gaps", source=name)
        self._issues = defaultdict(list)
        for row in self.issue_rows:
            item = json.loads(row["issue_json"])
            if item.get("trading_day"):
                item["trading_day"] = date.fromisoformat(item["trading_day"])
            self._issues[row["source_id"]].append(InputIssue(**item))
        if len(self.rows) != self.manifest["source_files"] or sum(r["rows"] for r in self.rows) != self.manifest["rows"]:
            fail("INVALID_PREPARED_DATA", "Prepared index totals differ", source=manifest_path)
        session = current_session()
        if session:
            session.fingerprint(manifest_path)
            session.paths.append({"prepared_dataset": self.identity})

    @property
    def identity(self):
        return dict(root=str(self.root), snapshot_id=self.manifest["snapshot_id"],
                    manifest_sha256=_sha(self.root / "manifest.json"), verification=self.verify)

    def _check_artifact(self, name, *, full=False):
        artifact = self.manifest["artifacts"].get(name)
        if artifact is None:
            fail("INVALID_PREPARED_DATA", "Missing artifact declaration", source=name)
        path = _artifact(self.root, name)
        if path.stat().st_size != artifact["size"]:
            fail("PREPARED_INTEGRITY", "Prepared artifact size differs", source=path)
        if full or self.verify == "full":
            if _sha(path) != artifact["sha256"]:
                fail("PREPARED_INTEGRITY", "Prepared artifact SHA256 differs", source=path)
        session = current_session()
        if session:
            session.fingerprint(path)
        return path

    def validate_spec(self, asset_kind, product, spec):
        spec_id = f"{asset_kind}:{product.upper()}:{spec.bar_spec}"
        if self.manifest["specs"].get(spec_id) != _spec_value(spec):
            fail("PREPARED_POLICY_MISMATCH", f"Prepared reading policy differs: {spec_id}", source=self.root)
        return spec_id

    def inventory(self, asset_kind, product, spec, start_day, end_day, predicate=lambda _: True):
        spec_id = self.validate_spec(asset_kind, product, spec)
        return {(key.trading_day, key.symbol): _artifact(self.root, row["data_file"])
                for (identifier, key), row in self._by_key.items() if identifier == spec_id
                and start_day <= key.trading_day <= end_day and predicate(key.symbol)}

    def load(self, plan, *, specs, products, context=None, copy_frames=True, frame_format="pandas", replay_inputs=None):
        import pyarrow.feather as feather
        if frame_format not in {"pandas", "arrow"}:
            raise ValueError("Prepared frame format must be pandas/arrow")
        identifiers = {kind: self.validate_spec(kind, products[kind], spec) for kind, spec in specs.items()}
        indexed = {key: _artifact(self.root, row["data_file"])
                   for (identifier, key), row in self._by_key.items()
                   if identifier == identifiers.get(key.asset_kind)}
        files, coverage = select_bar_files(indexed, plan.required, plan.optional, plan.requested)
        selected = {key: self._by_key[(identifiers[key.asset_kind],
                    BarFileKey(key.asset_kind, key.symbol, key.trading_day))] for key in files}
        if replay_inputs is not None:
            if frame_format != "arrow":
                raise ValueError("Certified replay inputs require Arrow source views")
            return replay_inputs.load_selected(self, selected, coverage, plan, specs, context)
        by_partition = defaultdict(list)
        for key, row in selected.items():
            by_partition[row["data_file"]].append((key, row))
        results = {}
        with _measure("prepared_load"):
            for name, sources in sorted(by_partition.items()):
                path = self._check_artifact(name)
                artifact = self.manifest["artifacts"][name]
                with _measure("prepared_partition_read"):
                    table = feather.read_table(path, memory_map=True)
                _count("prepared_partition_files_read")
                _count("prepared_partition_rows_read", len(table))
                schema_hash = hashlib.sha256(table.schema.serialize().to_pybytes()).hexdigest()
                metadata = table.schema.metadata or {}
                if (len(table) != artifact["rows"] or schema_hash != artifact["schema_sha256"]
                        or metadata.get(b"prepared_format") != FORMAT.encode()
                        or metadata.get(b"prepared_partition_id") != artifact["partition_id"].encode()):
                    fail("PREPARED_INTEGRITY", "Prepared partition schema/identity differs", source=path)
                for key, row in sources:
                    with _measure("prepared_frame_restore" if frame_format == "pandas" else "prepared_arrow_restore"):
                        columns = json.loads(row["columns"])
                        selected_table = table.slice(row["offset"], row["rows"]).select(columns)
                        if frame_format == "arrow":
                            from .columnar import ArrowBarFrame
                            frame = ArrowBarFrame(selected_table, attrs=json.loads(row["attrs"]),
                                                  duplicate_labels=row["duplicate_labels"])
                            _count("prepared_arrow_sources")
                        else:
                            frame = selected_table.to_pandas()
                            frame.attrs = json.loads(row["attrs"])
                            frame.flags.allows_duplicate_labels = row["duplicate_labels"]
                            if copy_frames:
                                frame = frame.copy(deep=True)
                    issues = tuple(self._issues[_source_id(row)])
                    result = BarLoadResult(key, path, frame, specs[key.asset_kind], issues)
                    results[key] = SourceSpec(result)
                    self._record(row, result)
        session = current_session()
        if session is not None:
            start, end = plan.requested
            actual, dependency = set(session.coverage.actual_days), set(session.coverage.dependency_days)
            for key in results:
                target = dependency if ((start is not None and key.trading_day < start)
                    or (end is not None and key.trading_day > end)) else actual
                target.add(key.trading_day)
            session.coverage.actual_days = tuple(sorted(actual))
            session.coverage.dependency_days = tuple(sorted(dependency))
        return HistoricalInputBundle(context or InputContext(), tuple(results[key] for key in sorted(results)),
                                     plan.bindings, coverage)

    def _record(self, row, result):
        session = current_session()
        if session is None:
            return
        for issue in result.issues:
            record_issue(issue)
        session.coverage.assumptions += tuple(item for item in json.loads(row["assumptions"])
            if item not in session.coverage.assumptions)
        _count("prepared_sources_loaded")
        _count("prepared_rows_loaded", row["rows"])
        spec = result.spec
        session.coverage.files[row["source_path"]] = dict(identity=str(result.key), rows=row["rows"],
            first_ns=row["first_ns"], last_ns=row["last_ns"], timestamp_label=spec.timestamp_label,
            value_policy=spec.value_policy, available_policy=spec.available_column or "completed_at_end",
            gap_check="interval_gaps_not_equivalent_to_missing_trading_minutes",
            prepared_path=str(result.path), source_sha256=row["source_sha256"],
            specifications=[dict(fields=list(spec.required_fields), timezone=spec.timezone,
                timestamp_label=spec.timestamp_label, value_policy=spec.value_policy,
                available_column=spec.available_column, trading_day_policy=spec.trading_day_policy,
                interval_seconds=spec.interval_seconds, bar_spec=spec.bar_spec,
                ohlc_policy=spec.ohlc_policy, zero_price_policy=spec.zero_price_policy)])

    def verify_all(self):
        """Publication/transfer audit; backtests verify only selected partitions."""
        for name in self.manifest["artifacts"]:
            self._check_artifact(name, full=True)
        return self.identity
