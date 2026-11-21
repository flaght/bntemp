"""One batch factory over dictionary codes and integer clocks; no engine SDK."""
from libc.stdint cimport int32_t, int64_t


def build_market(list keys, const int32_t[::1] key_codes,
                 list sources, const int32_t[::1] source_codes,
                 list prices, const int32_t[::1] price_codes,
                 const int64_t[::1] source_ns, const int64_t[::1] received_ns,
                 Py_ssize_t offset, Py_ssize_t count, object factory):
    cdef Py_ssize_t index, end
    cdef int32_t key, source, price
    cdef dict market = {}
    if (offset < 0 or count < 0 or offset > key_codes.shape[0]
            or count > key_codes.shape[0] - offset):
        raise ValueError("Prepared observation range exceeds columns")
    end = offset + count
    if (source_codes.shape[0] != key_codes.shape[0] or price_codes.shape[0] != key_codes.shape[0]
            or source_ns.shape[0] != key_codes.shape[0] or received_ns.shape[0] != key_codes.shape[0]):
        raise ValueError("Prepared observation column lengths differ")
    for index in range(offset, end):
        key, source, price = key_codes[index], source_codes[index], price_codes[index]
        if (key < 0 or key >= len(keys) or source < 0 or source >= len(sources)
                or price < 0 or price >= len(prices)):
            raise ValueError("Prepared observation dictionary code exceeds values")
        market[keys[key]] = factory(prices[price], sources[source], source_ns[index], received_ns[index])
    if len(market) != count:
        raise ValueError("Prepared market observation keys are duplicated")
    return market
