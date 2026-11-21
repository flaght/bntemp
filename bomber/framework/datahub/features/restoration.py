"""Shared observation restoration: Python reference or optional columnar Cython."""
from decimal import Decimal
from functools import lru_cache
from importlib import import_module

from bomber.framework.market.frames import FrameObservation


@lru_cache(maxsize=1)
def native_restoration_module():
    name = __package__+"._restoration_native"
    try:
        return import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
        return None


def _dictionary(column):
    """Materialize only distinct strings; keep row indices in a contiguous array."""
    import pyarrow as pa
    import pyarrow.compute as pc
    array = column.combine_chunks()
    if not pa.types.is_dictionary(array.type):
        array = array.dictionary_encode()
    if not (pa.types.is_string(array.dictionary.type) or pa.types.is_large_string(array.dictionary.type)
            or pa.types.is_null(array.dictionary.type)):
        raise ValueError("Prepared observation dictionaries must contain strings")
    values = array.dictionary.to_pylist()
    indices = array.indices.cast(pa.int32(), safe=True)
    if indices.null_count:
        indices = pc.fill_null(indices, len(values))
        values.append(None)
    return values, indices.to_numpy(zero_copy_only=True)


class ColumnarObservations:
    """Own the Arrow-backed arrays and shared immutable dictionary values."""
    def __init__(self, table, native, *, clock=None, seconds=None):
        import pyarrow as pa
        started = clock() if clock else None
        self.keys, self.key_codes = _dictionary(table.column("key"))
        self.sources, self.source_codes = _dictionary(table.column("source_id"))
        price_strings, self.price_codes = _dictionary(table.column("price"))
        clocks = []
        for field in ("source_ns", "received_ns"):
            column = table.column(field).combine_chunks()
            if column.null_count or not pa.types.is_integer(column.type):
                raise ValueError("Prepared observation clocks must be non-null integers")
            clocks.append(column.cast(pa.int64(), safe=True).to_numpy(zero_copy_only=True))
        self.source_ns, self.received_ns = clocks
        self.rows, self.native = len(table), native
        if clock:
            seconds["prepared_observations_columnar_prepare"] += clock() - started
            started = clock()
        # String dictionary identity preserves Decimal exponent and signed zero.
        self.prices = [Decimal(value) for value in price_strings]
        if clock:
            seconds["prepared_observation_decimal"] += clock() - started

    def restore(self, offset, count):
        return self.native.build_market(self.keys, self.key_codes, self.sources, self.source_codes,
            self.prices, self.price_codes, self.source_ns, self.received_ns, offset, count, FrameObservation)


def prepare_observation_columns(table, *, backend="auto", clock=None, seconds=None):
    if backend not in {"auto", "python", "cython"}:
        raise ValueError("Observation restore backend must be auto/python/cython")
    native = None if backend == "python" else native_restoration_module()
    if native is None and backend == "cython":
        raise RuntimeError("Cython帧恢复扩展不可用，请先运行bomber/framework/datahub/features/setup.py build_ext --inplace")
    if native is not None:
        return ColumnarObservations(table, native, clock=clock, seconds=seconds)
    started = clock() if clock else None
    columns = table.to_pydict()
    if clock:
        seconds["prepared_observations_to_python"] += clock() - started
    return columns


def restore_market_observations(columns, offset, count, *, clock=None, seconds=None):
    """Restore a caller-validated range of aligned columns.

    clock/seconds are a paired optional diagnostic interface. seconds aggregates
    batch durations locally so the outer service publishes each timing once.
    """
    if isinstance(columns, ColumnarObservations):
        started = clock() if clock else None
        market = columns.restore(offset, count)
        if clock:
            seconds["prepared_observation_objects"] += clock() - started
        return market
    end = offset + count
    started = clock() if clock else None
    prices = [Decimal(value) for value in columns["price"][offset:end]]
    if clock:
        seconds["prepared_observation_decimal"] += clock() - started
        started = clock()
    market = {columns["key"][index]: FrameObservation(
        price, columns["source_id"][index], columns["source_ns"][index], columns["received_ns"][index])
        for index, price in zip(range(offset, end), prices)}
    if len(market) != count:
        raise ValueError("Prepared market observation keys are duplicated")
    if clock:
        seconds["prepared_observation_objects"] += clock() - started
    return market
