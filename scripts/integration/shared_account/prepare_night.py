"""准备CU主力/CU跨期与CU/AU/AG/SC夜盘联调；只生成配置，不连接或改写账本。"""

import argparse
from contextlib import redirect_stdout
from decimal import Decimal, ROUND_CEILING
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import re
import shlex
from tempfile import NamedTemporaryFile, TemporaryDirectory

from .config import read_config
from .prepare_session import main as prepare_session, observed_budget


PRODUCTS = ("CU", "AU", "AG", "SC")


def rounded(value):
    return (value / Decimal(100000)).to_integral_value(rounding=ROUND_CEILING) * Decimal(100000)


def prepare(observations, root, *, seconds):
    """复用标准生成器，在临时目录组装，核验完成前不写最终配置。"""
    observed = json.loads(observations.read_text())
    if tuple(observed.get("cross_products", ())) != PRODUCTS:
        raise ValueError("观察须完整声明CU,AU,AG,SC；请用同样的cross-products重新discover")
    routes = observed["strategy_contracts"]
    selections = {"main_ema": {"CU": ("main",)}, "calendar_spread": {"CU": ("secondary", "far")},
                  "cross_section": {p: ("main",) for p in PRODUCTS}}
    for kind, products in selections.items():
        for product, roles in products.items():
            venue = "INE" if product == "SC" else "SHFE"
            for role in roles:
                if not re.fullmatch(product.lower() + r"\d{4}\." + venue, routes[kind][product][role]):
                    raise ValueError("观察中的逐策略真实合约与声明品种/交易所不符")
    if routes["calendar_spread"]["CU"]["secondary"] == routes["calendar_spread"]["CU"]["far"]:
        raise ValueError("CU跨期双腿不能为同一合约")
    with TemporaryDirectory(prefix="bomber-night-prepare-") as directory:
        staged = Path(directory) / "session.json"
        with redirect_stdout(StringIO()):
            prepare_session(["--output", str(staged), "--environment", "realtime",
                "--execution-mode", "independent", "--profile", "commodity", "--cross-products", ",".join(PRODUCTS),
                "--existing-position-policy", "preserve_external", "--observations", str(observations),
                "--use-observed-budget", "--ctp-future-buffer-ms", "100", "--seconds", str(seconds)])
        config = json.loads(staged.read_text())
        budget = observed_budget(observed["contracts"], observed["instrument_specs"], observed["quotes"], PRODUCTS,
                                 strategy_contracts=routes)
        tag = root.name.rsplit("-", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9_]{1,24}", tag):
            tag = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        config["account"].update(client_id=f"night-cu-{tag}", max_session_orders=200,
            max_gross_quantity=str(budget["quantity"]),
            max_gross_notional=str(max(Decimal(3000000), Decimal(budget["rounded_notional_limit"]))))
        config.update(account_state_file=str(root / "account.json"), report_dir=str(root / "reports"))
        names = {"main_ema": "ema-cu", "calendar_spread": "calendar-cu", "cross_section": "cross-cu-au-ag-sc"}
        leg_names = {"main_ema": "ema", "calendar_spread": "calendar", "cross_section": "cross"}
        for row in config["strategies"]:
            kind, argv = row["kind"], row["argv"]
            row.update(strategy_id=f"{names[kind]}-n{tag}", state_file=str(root / f"{kind}.json"))
            if any(arg.split("=", 1)[0] in {"--history-config", "--history-file", "--history-minutes"} for arg in argv):
                raise ValueError("基础商品配置含意外历史参数，停止准备")
            argv += ["--history-minutes", "0"]
            legs = [Decimal(leg["notional"]) for leg in budget["legs"] if leg["strategy"] == leg_names[kind]]
            argv[argv.index("--max-notional") + 1] = str(max(Decimal(1000000), rounded(max(legs))))
            if kind == "main_ema":
                argv += ["--expected-instrument", routes[kind]["CU"]["main"]]
            elif kind == "calendar_spread":
                for role in ("secondary", "far"):
                    argv += ["--expected-instrument", f"{role}={routes[kind]['CU'][role]}"]
            else:
                argv[argv.index("--target-notional") + 1] = str(max(Decimal(1000000), rounded(2 * max(legs))))
                for product in PRODUCTS:
                    argv += ["--expected-instrument", f"{product}={routes[kind][product]['main']}"]
        staged.write_text(json.dumps(config))
        read_config(staged)
    return config, tag


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path("/tmp/bomber-night-cu-au-ag-sc.env"))
    parser.add_argument("--seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    root, observations = args.output_dir.resolve(), args.observations.resolve()
    try:
        env_file = args.env_file.resolve()
        if env_file == observations or env_file.is_relative_to(root):
            raise ValueError("env-file须位于会话目录外，不能覆盖配置或观察文件")
        config, tag = prepare(observations, root, seconds=args.seconds)
        out = root / "session.json"
        if out.exists():
            if read_config(out) != config:
                raise ValueError("session.json已存在且与本次配置不同；保留原文件，请核对原会话")
            state = [config["account_state_file"], *(row["state_file"] for row in config["strategies"])]
            if any(Path(path).exists() for path in state):
                raise ValueError("本会话已有检查点；不能按新会话启动，请使用原配置及resume恢复")
            action = "配置已存在且一致，保持原文件"
        else:
            if any(Path(path).exists() for path in [config["account_state_file"], *(r["state_file"] for r in config["strategies"])]):
                raise ValueError("目标目录已有检查点，不能生成新会话覆盖其归属")
            root.mkdir(parents=True, exist_ok=True)
            with out.open("x", encoding="utf-8") as output:
                json.dump(config, output, ensure_ascii=False, indent=2)
                output.write("\n")
            action = "新配置已生成"
        values = {"NIGHT_DIR": str(root), "NIGHT_TAG": tag, "NIGHT_CONFIG": str(out),
            "NIGHT_SOCKET": f"/tmp/bomber-night-{tag}/exec.sock", "NIGHT_KEYS": f"/tmp/bomber-night-{tag}/keys",
            "CTP_MD_ADDRESS": "tcp://182.254.243.31:30011", "CTP_TD_ADDRESS": "tcp://182.254.243.31:30001",
            "CTP_PRODUCTION_MODE": "true"}
        with NamedTemporaryFile(mode="w", dir=env_file.parent, prefix=".bomber-night-", delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write("".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items()))
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, env_file)
            finally:
                temporary.unlink(missing_ok=True)
        print(f"{action}: {out}；未连接、未报单；历史加载关闭")
        print(f"环境文件: {env_file}；交易日={config['trading_day']}；账户预算={config['account']}")
        for row in config["strategies"]:
            print(f"策略: {row['strategy_id']}；参数: {row['argv']}")
    except (ValueError, KeyError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
