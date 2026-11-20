"""Shared market-observation restoration; no dataset or strategy identifiers.

The decimal and object batches are separate measurement boundaries for a future
native backend. Clocks are sampled per batch, never per observation.
"""
from decimal import Decimal

from bomber.framework.market.frames import FrameObservation


def restore_market_observations(columns, offset, count, *, clock=None, seconds=None):
    """Restore a caller-validated range of aligned columns.

    clock/seconds are a paired optional diagnostic interface. seconds aggregates
    batch durations locally so the outer service publishes each timing once.
    """
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
