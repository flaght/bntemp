"""比较旧、新 fills_output.csv：逐时间成交与逐日日终净仓位。"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from zoneinfo import ZoneInfo


def read_fills(path):
    result = []
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        required = {"timestamp", "instrument_id", "side", "qty", "price", "commission"}
        if required - set(reader.fieldnames or ()):
            raise ValueError(f"{path} 缺少列: {sorted(required - set(reader.fieldnames or ()))}")
        for row in reader:
            stamp = datetime.fromisoformat(row["timestamp"])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            stamp = stamp.astimezone(ZoneInfo("Asia/Shanghai"))
            symbol = row["instrument_id"].upper()
            if "." not in symbol:
                symbol += ".CFFEX"
            values = [Decimal(row[key]) for key in ("qty", "price", "commission")]
            if row["side"].upper() not in {"BUY", "SELL"} or any(not value.is_finite() for value in values):
                raise ValueError(f"无效成交: {row}")
            if values[0] <= 0 or values[1] <= 0 or values[2] < 0:
                raise ValueError(f"成交数量、价格及费用无效: {row}")
            result.append((stamp, symbol, row["side"].upper(), *values))
    return sorted(result)


def compare(reference, candidate, output, *, tolerance=Decimal("0.000001")):
    if not tolerance.is_finite() or tolerance < 0:
        raise ValueError("tolerance 须为有限非负数")
    left, right = read_fills(reference), read_fills(candidate)

    def aggregate(fills):
        result = defaultdict(lambda: [Decimal(0), Decimal(0), Decimal(0)])
        for stamp, symbol, side, qty, price, commission in fills:
            values = result[(stamp, symbol, side)]
            values[0] += qty
            values[1] += qty * price
            values[2] += commission
        return result

    first, second = aggregate(left), aggregate(right)
    differences = []
    for key in sorted(first.keys() | second.keys()):
        a, b = first.get(key, [Decimal(0)] * 3), second.get(key, [Decimal(0)] * 3)
        price_a = a[1] / a[0] if a[0] else Decimal(0)
        price_b = b[1] / b[0] if b[0] else Decimal(0)
        if a[0] != b[0] or abs(price_a - price_b) > tolerance or abs(a[2] - b[2]) > tolerance:
            differences.append({"timestamp": key[0].isoformat(), "instrument_id": key[1], "side": key[2],
                "reference_qty": a[0], "candidate_qty": b[0],
                "reference_vwap": a[1] / a[0] if a[0] else "", "candidate_vwap": b[1] / b[0] if b[0] else "",
                "reference_commission": a[2], "candidate_commission": b[2]})
    days = sorted({row[0].date() for row in left + right})
    symbols = sorted({row[1] for row in left + right})

    def end_positions(fills):
        daily = defaultdict(lambda: defaultdict(Decimal))
        for stamp, symbol, side, qty, _, _ in fills:
            daily[stamp.date()][symbol] += qty if side == "BUY" else -qty
        current, result = defaultdict(Decimal), {}
        for day in days:
            for symbol, quantity in daily[day].items():
                current[symbol] += quantity
            result[day] = dict(current)
        return result

    first_pos, second_pos = end_positions(left), end_positions(right)
    position_rows = [{"date": str(day), "instrument_id": symbol,
                      "reference_qty": first_pos[day].get(symbol, Decimal(0)),
                      "candidate_qty": second_pos[day].get(symbol, Decimal(0))}
                     for day in days for symbol in symbols
                     if first_pos[day].get(symbol, Decimal(0)) != second_pos[day].get(symbol, Decimal(0))]
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name, records, columns in (
        ("fill_differences.csv", differences, ("timestamp", "instrument_id", "side", "reference_qty", "candidate_qty",
             "reference_vwap", "candidate_vwap", "reference_commission", "candidate_commission")),
        ("daily_position_differences.csv", position_rows, ("date", "instrument_id", "reference_qty", "candidate_qty")),
    ):
        with (output / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(records)
    summary = {"reference": str(Path(reference).resolve()), "candidate": str(Path(candidate).resolve()),
               "reference_fills": len(left), "candidate_fills": len(right),
               "fill_difference_groups": len(differences), "daily_position_differences": len(position_rows),
               "fills_equal": not differences, "daily_positions_equal": not position_rows,
               "passed": not differences and not position_rows,
               "scope": "exact timestamp/side traded volume, VWAP, costs; zero initial positions assumed; no PnL comparison",
               "nonempty": bool(left or right)}
    (output / "comparison.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tolerance", type=Decimal, default=Decimal("0.000001"))
    args = parser.parse_args()
    result = compare(args.reference, args.candidate, args.output, tolerance=args.tolerance)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
