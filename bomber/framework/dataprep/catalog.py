"""公共文件索引，文件是否存在不会改变交易候选范围。"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import os
import re

from .contracts import ASSET_KINDS, BarFileKey, CoverageReport
from .session import current_session, fail


def symbol(value):
    return str(value).strip().split(".")[0].upper()


def _feather_names(root):
    """Same POSIX rglob scope/order, without constructing/sorting every Path.

    walk does not descend through directory symlinks; matching directory entries
    and file symlinks are still included, just as with rglob('*.feather').
    Tuple sorting preserves Path's component order (string sorting does not).
    """
    if os.name != "posix":
        return [str(path) for path in sorted(Path(root).rglob("*.feather"))]
    names = []
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            if name.endswith(".feather"):
                names.append(os.path.join(parent, name))
    prefix = len(root.rstrip(os.sep)) + 1
    names.sort(key=lambda name: tuple(name[prefix:].split(os.sep)))
    return names


def scan_bar_files(root, asset_kind="future", start_day=None, end_day=None):
    if asset_kind not in ASSET_KINDS:
        fail("INVALID_ASSET", f"Unknown asset kind {asset_kind}")
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    if start_day and end_day and end_day < start_day:
        fail("INVALID_RANGE", "End date precedes start date")
    session = current_session()
    cache_key = ("inventory", str(root))
    if session is not None and cache_key in session.cache:
        inventory = session.cache[cache_key]
    else:
        inventory = {}
        days, symbols = {}, {}
        for name in _feather_names(str(root)):
            parent, basename = os.path.split(name)
            code, separator, label = basename[:-len(".feather")].rpartition("_")
            if not separator or not re.fullmatch(r"\d{8}", label):
                continue
            day = days.get(label)
            if day is None:
                try:
                    day = date.fromisoformat(f"{label[:4]}-{label[4:6]}-{label[6:]}")
                except ValueError:
                    fail("INVALID_TIMESTAMP", "Invalid file trading day", source=Path(name))
                days[label] = day
            parent_name = os.path.basename(parent)
            if parent_name != label and re.fullmatch(r"\d{8}", parent_name):
                fail("IDENTITY_MISMATCH", "Directory and filename trading days differ", source=Path(name))
            if code not in symbols:
                symbols[code] = symbol(code)
            key = BarFileKey(asset_kind, symbols[code], day)
            path = Path(name)
            if key in inventory:
                fail("DUPLICATE_FILE", f"Duplicate identity: {key}; first={inventory[key]}", source=path)
            inventory[key] = path
        if session is not None:
            session.cache[cache_key] = inventory
            session.counters["directory_scans"] += 1
    return {(k if k.asset_kind == asset_kind else BarFileKey(asset_kind, k.symbol, k.trading_day, k.venue)): v
            for k, v in inventory.items()
            if (start_day is None or k.trading_day >= start_day)
            and (end_day is None or k.trading_day <= end_day)}


def inventory(root, start_day, end_day, predicate, asset_kind="option"):
    return {(key.trading_day, key.symbol): path
            for key, path in scan_bar_files(root, asset_kind, start_day, end_day).items()
            if predicate(key.symbol)}


def select_bar_files(index, required, optional=(), requested=(None, None)):
    required, optional = set(required), set(optional)
    # 交易所由基础资料确定，不根据供应商文件名推断。
    by_identity = {(k.asset_kind, k.symbol, k.trading_day): p for k, p in index.items()}
    found = {k: by_identity[(k.asset_kind, k.symbol, k.trading_day)]
             for k in required | optional if (k.asset_kind, k.symbol, k.trading_day) in by_identity}
    report = CoverageReport(requested=requested,
        actual_days=tuple(sorted({k.trading_day for k in found})),
        required_missing=tuple(sorted(required - found.keys())),
        optional_missing=tuple(sorted(optional - found.keys())))
    session = current_session()
    if session is not None:
        session.coverage.required_missing += report.required_missing
        session.coverage.optional_missing += report.optional_missing
    if report.required_missing:
        fail("MISSING_REQUIRED_BAR", f"Missing required files: {report.required_missing[:10]}")
    return found, report


def bar_paths(root, required, optional=None):
    required_keys = {BarFileKey("future", code, day) for day, code in required}
    optional_keys = {BarFileKey("future", code, day) for day, code in optional or ()}
    found, _ = select_bar_files(scan_bar_files(root), required_keys, optional_keys)
    return {(day, code): found[BarFileKey("future", code, day)]
            for day, code in set(required) | set(optional or ())
            if BarFileKey("future", code, day) in found}


def bar_path(root, code, day):
    return bar_paths(root, {(day, code)})[(day, code)]


def files_for_symbol(root, code, start_day, end_day):
    found = {k.trading_day: p for k, p in scan_bar_files(root, "future", start_day, end_day).items()
             if k.symbol == symbol(code)}
    if not found:
        raise FileNotFoundError(f"{root}: {code} has no bars in {start_day}..{end_day}")
    return found


def bar_files(root, symbols, start_day, end_day):
    return {code.lower(): tuple(files_for_symbol(root, code, start_day, end_day).values())
            for code in sorted(symbols)}


def product_inventory(root, product, start_day, end_day):
    result = {}
    pattern = re.compile(re.escape(product) + r"\d+", re.IGNORECASE)
    for key in scan_bar_files(root, "future", start_day, end_day):
        if pattern.fullmatch(key.symbol):
            result.setdefault(key.trading_day, set()).add(key.symbol.lower())
    return result
