"""Read-only source views over prepared Arrow partitions, without a DataFrame."""
from functools import cached_property


class ArrowBarFrame:
    """A selected source's immutable columns; pandas is materialized only on request.

    The datetime column remains available for the existing entry-clock checks.
    This is an explicit alternative representation, not a DataFrame replacement.
    """

    def __init__(self, table, *, attrs=None, duplicate_labels=True):
        self.table = table
        self.attrs = attrs or {}
        self.duplicate_labels = duplicate_labels

    def __len__(self):
        return len(self.table)

    @property
    def empty(self):
        return len(self) == 0

    def array(self, name):
        return self.table.column(name).combine_chunks().to_numpy(zero_copy_only=False)

    @cached_property
    def datetime(self):
        return self.table.column("datetime").to_pandas()

    @property
    def event_ns(self):
        # Compatibility with BarLoadResult.first_ns/last_ns; not a whole frame.
        return self.table.column("event_ns").to_pandas()

    def to_pandas(self):
        frame = self.table.to_pandas()
        frame.attrs = self.attrs.copy()
        frame.flags.allows_duplicate_labels = self.duplicate_labels
        return frame
