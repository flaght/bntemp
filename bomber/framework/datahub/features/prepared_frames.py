"""Columnar persistence for already-computed EXACT index decision outcomes.

No feature lookup or market regrouping is performed when restoring. This codec
preserves decision/as-of clocks, revisions, READY/SKIPPED outcomes and Decimal
representations, not only an event-time join of numeric feature values.
"""
from .alignment import FeatureSnapshot
from .contracts import FeatureQuery
from .frames import FrameOutcome, StrategyFrame
from .restoration import restore_market_observations


def encode_frames(outcomes, deadlines, records, binding):
    import pyarrow as pa
    record_index = {record.identity: index for index, record in enumerate(records)}
    if len(record_index) != len(records):
        raise ValueError("Frame preparation requires unique feature record identities")
    rows = []
    observations = {name: [] for name in ("key", "price", "source_id", "source_ns", "received_ns")}
    fingerprint = binding.spec.fingerprint
    for outcome in outcomes:
        offset = len(observations["key"])
        row = {"frame_id": outcome.frame_id, "event_ns": outcome.event_ns,
               "deadline_ns": deadlines[outcome.event_ns], "status": outcome.status, "reason": outcome.reason,
               "offset": offset, "rows": 0, "feature_index": -1,
               "decision_ns": 0, "as_of_ns": 0, "generation": 0}
        if outcome.frame is not None:
            frame, snapshot = outcome.frame, outcome.frame.features
            if (set(snapshot.values) != {"index"} or len(snapshot.values["index"]) != 1
                    or snapshot.schema_fingerprints != {"index": fingerprint}
                    or snapshot.query.event_ns != outcome.event_ns or frame.event_ns != outcome.event_ns):
                raise ValueError("Prepared frames support one EXACT index feature with matching clocks")
            record = snapshot.latest("index")
            position = record_index.get(record.identity)
            if position is None or records[position] != record:
                raise ValueError("Selected feature record differs from the bound input")
            row.update(feature_index=position, decision_ns=frame.decision_ns,
                       as_of_ns=snapshot.query.as_of_ns, generation=snapshot.generation)
            for key, observation in frame.market.items():
                observations["key"].append(key)
                observations["price"].append(str(observation.price))
                observations["source_id"].append(observation.source_id)
                observations["source_ns"].append(observation.source_ns)
                observations["received_ns"].append(observation.received_ns)
            row["rows"] = len(observations["key"])-offset
        elif outcome.status == "READY":
            raise ValueError("READY outcome must contain a frame")
        rows.append(row)
    if not rows:
        raise ValueError("Cannot publish empty frame outcomes")
    frame_table = pa.Table.from_pylist(rows)
    market_table = pa.table({name: pa.array(values, type=pa.int64() if name.endswith("_ns") else pa.string())
                            for name, values in observations.items()})
    return frame_table, market_table


def decode_frames(frame_table, observation_table, records, binding, *, performance=None):
    clock = performance.clock if performance is not None else None
    started = clock() if clock else None
    rows = frame_table.to_pylist()
    if clock:
        performance.add("prepared_frames_to_python", clock() - started)
        started = clock()
    observations = observation_table.to_pydict()
    if clock:
        performance.add("prepared_observations_to_python", clock() - started)
    seconds = dict.fromkeys(("prepared_frame_reference_checks", "prepared_observation_decimal",
        "prepared_observation_objects", "prepared_frame_objects"), 0.0)
    outcomes, deadlines = [], {}
    fingerprint = binding.spec.fingerprint
    consumed = 0
    previous_event = -1
    restored_observations, ready = 0, 0
    for row in rows:
        started = clock() if clock else None
        event, offset, count = row["event_ns"], row["offset"], row["rows"]
        if (event <= previous_event or row["deadline_ns"] < event or offset != consumed
                or count < 0 or offset+count > len(observation_table)):
            raise ValueError("Prepared frame order/range/deadline differs")
        previous_event, consumed = event, offset+count
        deadlines[event] = row["deadline_ns"]
        frame = None
        if row["status"] == "READY":
            position = row["feature_index"]
            if not 0 <= position < len(records):
                raise ValueError("Prepared feature reference exceeds input")
            record = records[position]
            if (record.ts_event != event or record.visible_ns > row["as_of_ns"]
                    or not event <= row["as_of_ns"] <= row["decision_ns"] <= row["deadline_ns"]):
                raise ValueError("Prepared feature/frame visibility clocks differ")
            if clock:
                seconds["prepared_frame_reference_checks"] += clock() - started
            market = restore_market_observations(observations, offset, count, clock=clock, seconds=seconds)
            restored_observations += count
            ready += 1
            started = clock() if clock else None
            snapshot = FeatureSnapshot(FeatureQuery(event, row["as_of_ns"]), row["generation"],
                                       {"index": (record,)}, {"index": fingerprint})
            frame = StrategyFrame(row["frame_id"], event, row["decision_ns"], market, snapshot)
        elif row["status"] != "SKIPPED" or count or row["feature_index"] != -1:
            raise ValueError("Invalid prepared frame outcome")
        elif clock:
            seconds["prepared_frame_reference_checks"] += clock() - started
            started = clock()
        outcomes.append(FrameOutcome(row["frame_id"], event, row["status"], frame, row["reason"]))
        if clock:
            seconds["prepared_frame_objects"] += clock() - started
    if consumed != len(observation_table):
        raise ValueError("Unreferenced prepared market observations")
    if performance is not None:
        for name, elapsed in seconds.items():
            performance.add(name, elapsed)
        for name, value in (("prepared_frame_rows", len(rows)), ("prepared_observation_rows", len(observation_table)),
                            ("prepared_observations_restored", restored_observations),
                            ("prepared_frames_ready", ready), ("prepared_frames_skipped", len(rows)-ready)):
            performance.count(name, value)
    return tuple(outcomes), deadlines
