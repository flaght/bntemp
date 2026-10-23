from __future__ import annotations

import argparse,json,sys
from dataclasses import asdict, is_dataclass
from datetime import date, timedelta
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from bomber.framework.dataprep.session import input_session, read_feather, write_input_reports


@input_session
def run_case(*, product: str, start_day: date, end_day: date,
             bars_dir: Path, contract_struct: Path, fut_basic: Path,
             release_path: Path, directions_file: Path,
             report_dir: Path, factors_path: Path | None = None,
             factor_availability: str = "aligned",
             bar_timestamp: str = "end", value_column: str = "value",
             open_interest_column: str = "openint", max_contracts: int = 1,
             rebalance_minutes: int = 1, history_bars: int = 1500,
             min_raw_bars: int = 300, device: str = "auto",
             max_notional: Decimal = Decimal("1000000"),
             commission_per_contract: Decimal = Decimal("1"),
             require_fills: bool = False) -> Path:

    
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", choices=("P", "RB"), required=True)
    parser.add_argument("--start-day", type=date.fromisoformat, required=True)
    parser.add_argument("--end-day", type=date.fromisoformat, required=True)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--contract-struct", type=Path, required=True)
    parser.add_argument("--fut-basic", type=Path, required=True)
    parser.add_argument("--release-path", type=Path, required=True)
    parser.add_argument("--directions-file", type=Path, required=True)
    parser.add_argument("--factors", type=Path)
    parser.add_argument("--factor-availability", choices=("aligned", "explicit"),
                        default="aligned")
    parser.add_argument("--bar-timestamp", choices=("start", "end"), default="end")
    parser.add_argument("--value-column", default="value")
    parser.add_argument("--open-interest-column", default="openint")
    parser.add_argument("--max-contracts", type=int, default=1)
    parser.add_argument(
        "--rebalance-minutes", type=int, default=1,
        help="目标仓位更新间隔；默认每分钟。训练标签的 5 分钟预测期、评估的 5 分钟重采样均不限制交易频率",
    )
    parser.add_argument("--history-bars", type=int, default=1500)
    parser.add_argument("--min-raw-bars", type=int, default=300)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-notional", type=Decimal, default=Decimal("1000000"))
    parser.add_argument("--commission-per-contract", type=Decimal,
                        default=Decimal("1"))
    parser.add_argument("--require-fills", action="store_true")
    parser.add_argument("--report-dir", type=Path,
                        default=Path(__file__).resolve().parent / "results")
    args = parser.parse_args()
    for path in (args.bars_dir, args.contract_struct, args.fut_basic,
                 args.release_path, args.directions_file):
        if not path.exists():
            parser.error(f"输入不存在: {path}")
    if args.factors is not None and not args.factors.exists():
        parser.error(f"输入不存在: {args.factors}")


if __name__ == "__main__":
    main()
