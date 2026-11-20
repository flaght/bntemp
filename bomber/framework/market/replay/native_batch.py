"""Explicit prepared-Arrow -> existing Cython Bar batch construction.

No persisted native objects, no engine batching, and no snapshot format changes.
The legacy factory's float -> fixed-decimal text rounding is retained before the
native double-array constructor. Unsafe round trips use the legacy row parser.
"""
from contextlib import nullcontext
from dataclasses import dataclass

import numpy as np

from bomber.framework.dataprep.columnar import ArrowBarFrame, CertifiedArrowFrame
from bomber.framework.market.basic.base import Bar, BarType
from .parsers.base import DataLoadError, ParserContext
from .parsers.bar.fixed import FixedInstrumentBarParser, PreparedBarParser


def require_native_batch():
    if not callable(getattr(Bar, "from_raw_arrays_to_list", None)):
        raise RuntimeError("当前Bomber未提供Bar.from_raw_arrays_to_list；请使用--bar-parser prepared")


def _measure(stats, name):
    return stats.measure(name) if stats is not None else nullcontext()


def _count(stats, name, value):
    if stats is not None:
        stats.count(name, value)


def legacy_rounded_array(values, precision):
    """Match make_bar's Python formatting; guard the subsequent native round.

    Native constructors round float * 10**precision to an integer. Require that
    operation to stay close to the exact formatted integer with ample margin.
    Restrict to ordinary precision/ranges; the row path handles exceptions.
    """
    if not 0 <= precision <= 8:
        return None
    result = np.empty(len(values), dtype=np.float64)
    scale = 10 ** precision
    for index, value in enumerate(values):
        text = f"{float(value):.{precision}f}"
        units = int(text.replace(".", ""))
        rounded = float(text)
        if abs(units) > 2**50 or abs(rounded * scale - units) > .25:
            return None
        result[index] = rounded
    return result


@dataclass
class ArrowBatchReader:
    frame: ArrowBarFrame
    suffixes = frozenset({".feather"})


class NativeBarBatchParser:
    def __init__(self, instrument_id, bar_spec, timezone="Asia/Shanghai"):
        self.instrument_id = instrument_id
        self.bar_spec = bar_spec
        self.timezone = timezone
        self.reset()

    def reset(self):
        self._meta = self._bar_type = None

    @staticmethod
    def _reject(mask, label, context):
        bad = np.flatnonzero(mask)
        if len(bad):
            raise ParserContext(context.path, int(bad[0])+1, context.get_meta).error(label)

    def _fallback(self, frame, context, stats, reason):
        from bomber.framework.dataprep.bars import PreparedFrameReader
        _count(stats, "native_batch_fallback_sources", 1)
        _count(stats, "native_batch_fallback_rows", len(frame))
        _count(stats, "native_batch_fallback_"+reason, len(frame))
        with _measure(stats, "native_batch_fallback"):
            pandas_frame = frame.to_pandas()
            parser = (FixedInstrumentBarParser(self.instrument_id, bar_spec=self.bar_spec,
                          available_nanoseconds="available_ns", timezone=self.timezone) if reason == "submicro_clock"
                      else PreparedBarParser(self.instrument_id, bar_spec=self.bar_spec))
            bars = []
            for line, row in PreparedFrameReader(pandas_frame, copy_frame=False).read(context.path):
                row_context = ParserContext(context.path, line, context.get_meta)
                bars.extend(event.payload for event in parser.parse(row, row_context))
            return bars

    def bind(self, context):
        require_native_batch()
        if self._meta is None:
            self._meta = context.require_meta(self.instrument_id)
        if self._bar_type is None:
            self._bar_type = BarType.from_str(f"{self.instrument_id}-{self.bar_spec}-LAST-EXTERNAL")

    def prepare_arrays(self, frame, context, stats=None):
        """Uncertified path; also used once by the offline artifact publisher."""
        self.bind(context)
        if not isinstance(frame, ArrowBarFrame):
            raise context.error("Native batches require prepared Arrow source views")
        with _measure(stats, "native_batch_array_prepare"):
            numbers = {name: np.asarray(frame.array(name), dtype=np.float64)
                       for name in ("open", "high", "low", "close", "volume")}
            for name, values in numbers.items():
                self._reject(~np.isfinite(values), f"{name} must be finite", context)
                self._reject(values < 0 if name == "volume" else values <= 0,
                             f"Invalid {name}", context)
            self._reject(numbers["high"] < np.maximum.reduce([numbers[n] for n in ("open", "low", "close")]),
                         "high is below an OHLC component", context)
            self._reject(numbers["low"] > np.minimum.reduce([numbers[n] for n in ("open", "high", "close")]),
                         "low is above an OHLC component", context)
            events, available = frame.array("event_ns"), frame.array("available_ns")
            for name, values in (("event_ns", events), ("available_ns", available)):
                if values.dtype.kind not in "iu":
                    raise context.error(f"{name} must be integer nanoseconds")
                self._reject(values < 0, f"{name} must be nonnegative", context)
            self._reject(available < events, "bar availability precedes completed bar time", context)
            submicro = bool((events % 1000 != 0).any())
            # The existing Cython signature requests writable contiguous buffers.
            events = np.array(events, dtype=np.uint64, copy=True)
            available = np.array(available, dtype=np.uint64, copy=True)
        if submicro:
            return None, "submicro_clock"
        with _measure(stats, "native_batch_rounding"):
            rounded = [legacy_rounded_array(numbers[name], self._meta.size_precision if name == "volume"
                                           else self._meta.price_precision) for name in numbers]
        if any(values is None for values in rounded):
            return None, "rounding_range"
        return (*rounded, events, available), None

    def load_bar_batch(self, reader, context, stats=None):
        frame = reader.frame
        self.bind(context)
        if isinstance(frame, CertifiedArrowFrame):
            expected = {"instrument_id": str(self.instrument_id), "bar_spec": self.bar_spec,
                        "price_precision": self._meta.price_precision, "size_precision": self._meta.size_precision}
            if frame.certificate != expected:
                raise context.error("Prepared native input precision/identity changed; prepare a new version")
            with _measure(stats, "native_ready_array_read"):
                arrays = tuple(np.array(frame.array(name), dtype=dtype, copy=True)
                    for name, dtype in (("open", np.float64), ("high", np.float64), ("low", np.float64),
                        ("close", np.float64), ("volume", np.float64),
                        ("event_ns", np.uint64), ("available_ns", np.uint64)))
            _count(stats, "native_ready_sources", 1)
            _count(stats, "native_ready_rows", len(frame))
        else:
            arrays, reason = self.prepare_arrays(frame, context, stats)
            if reason is not None:
                return self._fallback(frame, context, stats, reason)
        with _measure(stats, "native_batch_construct"):
            bars = Bar.from_raw_arrays_to_list(self._bar_type, self._meta.price_precision,
                self._meta.size_precision, *arrays)
        if len(bars) != len(frame):
            raise context.error("Native batch constructor returned a different number of bars")
        _count(stats, "native_batch_sources", 1)
        _count(stats, "native_batch_rows", len(bars))
        return bars


def add_arrow_bar_source(feed, result, instrument_id):
    if not isinstance(result.frame, ArrowBarFrame) or result.spec.value_policy != "execution_strict":
        raise DataLoadError("Native bar batches require execution_strict prepared Arrow sources")
    from bomber.framework.dataprep.catalog import symbol
    if (result.key.symbol != symbol(instrument_id.symbol.value)
            or result.key.venue and result.key.venue != str(instrument_id.venue)):
        raise DataLoadError(f"Prepared source differs from instrument: {result.path}")
    if "exchange" in result.frame.table.column_names:
        from bomber.framework.dataprep.metadata import VENUES
        expected = str(instrument_id.venue).upper()
        if any(VENUES.get(str(value).strip().upper(), str(value).strip().upper()) != expected
               for value in result.frame.table.column("exchange").to_pylist()):
            raise DataLoadError(f"Prepared rows differ from instrument venue: {result.path}")
    feed.add_source(result.path, ArrowBatchReader(result.frame),
                    NativeBarBatchParser(instrument_id, result.spec.bar_spec, result.spec.timezone), "bar")
