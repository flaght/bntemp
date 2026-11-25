"""核算教学案例、题答对应、离线HTML链接及已核对源码指纹。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from decimal import Decimal as D
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
EXPECTED_SOURCES = {
    "demos/08_option_vega/strategy.py": "385bcf352a52a1431d50297d6af32295e650e3ab033d42a7fd9ed9a4a1e0f337",
    "demos/08_option_vega/vega_signal.py": "7407614b19422773acff714e65508bf668445c1dc1d567c40c68ef775001ccfe",
    "demos/08_option_vega/config/vega_backtest.json": "643fc631d4baa6ca3a2c4fb4613c07f49d9f3069fb02ec86e123d1c76081b488",
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class HTMLCheck(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.links = []
        self.questions = []
        self.answers = 0
        self.answer_open = 0
        self.textareas = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "a" and "href" in a:
            self.links.append(a["href"])
        if tag == "section" and "data-qid" in a:
            self.questions.append(a["data-qid"])
        if tag == "details" and a.get("class") == "answer":
            self.answers += 1
            self.answer_open += "open" in a
        self.textareas += tag == "textarea"


def main():
    fingerprints = {}
    for rel, expected in EXPECTED_SOURCES.items():
        path = REPO / rel
        actual = sha(path)
        assert actual == expected, f"源码已变动，请重新核对文档：{rel}"
        fingerprints[rel] = {"path": str(path), "sha256": actual}
    spec = importlib.util.spec_from_file_location("learning_signal_20261119", REPO / "demos/08_option_vega/vega_signal.py")
    signal = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = signal
    spec.loader.exec_module(signal)
    case = json.loads((HERE / "综合案例数据.json").read_text())
    m, M = D(case["option_multiplier"]), D(case["future_multiplier"])
    g = case["greeks"]
    G = {s: signal.Greeks(.25, d["delta"], d["gamma"], d["vega"]) for s, d in g.items()}
    inc = signal.allocate_short([(s, G[s], m) for s in ["A", "B"]], case["delta_targets"],
                                case["cash_vega_budget_per_sigma"], 100, 200)
    assert inc == {"A": -10, "B": -20}
    s0 = {s: D(x["signed_quantity"]) for s, x in case["s0"]["options"].items()}
    s1 = {s: s0[s] + inc[s] for s in s0}
    assert s1 == {"A": D(-14), "B": D(-28)}
    qty = dict(s0)
    proceeds = {s: -s0[s] * D(case["s0"]["options"][s]["entry_price"]) * m for s in s0}
    for f in case["new_option_fills"]:
        q = D(f["signed_quantity"])
        qty[f["symbol"]] += q
        proceeds[f["symbol"]] -= q * D(f["price"]) * m
    assert qty == {"A": D(-9), "B": D(-18)}
    assert proceeds == {"A": D(42000), "B": D(17400)}
    dF = D(case["s2_market"]["future"]) - D(case["near_future"])
    delta = {s: D(str(g[s]["delta"])) + D(str(g[s]["gamma"])) * dF for s in g}
    exposure = sum(qty[s] * m * delta[s] for s in qty)
    assert exposure == D(-675)
    h = round(-exposure / M)
    assert h == 3
    initial_h = D(case["s0"]["future"]["signed_quantity"])
    delta_h = D(h) - initial_h
    future_basis = initial_h * D(case["s0"]["future"]["entry_price"]) + delta_h * D(case["s2_future_fill_price"])
    assert future_basis == D(18080)
    option_pnl = {s: proceeds[s] + qty[s] * D(case["s2_market"][s]) * m for s in qty}
    future_pnl = (D(h) * D(case["s2_market"]["future"]) - future_basis) * M
    assert option_pnl == {"A": D(-21000), "B": D(-11400)}
    assert future_pnl == D(14000)
    s2_pnl = sum(option_pnl.values()) + future_pnl
    assert s2_pnl == D(-18400)
    shock = case["hypothetical_shock"]
    option_shock = sum(qty[s] * m * (delta[s] * D(shock["future_change"]) +
                        D(g[s]["vega"]) * D(str(shock["sigma_change"]))) for s in qty)
    future_shock = D(h) * M * D(shock["future_change"])
    assert option_shock == D(-5850) and future_shock == D(1200)
    assert option_shock + future_shock == D(-4650)
    strikes = {x["symbol"]: D(x["strike"]) for x in case["candidates"]}
    settlement = case["independent_settlement_ending"]
    payoff = {s: max(D(settlement["index"]) - strikes[s], D(0)) for s in qty}
    terminal_option = {s: proceeds[s] + qty[s] * payoff[s] * m for s in qty}
    terminal_future = (D(h) * D(settlement["future"]) - future_basis) * M
    terminal_net = sum(terminal_option.values()) + terminal_future
    assert terminal_option == {"A": D(-93000), "B": D(17400)}
    assert terminal_future == D(134000) and terminal_net == D(58400)
    flatten = case["independent_flatten_ending"]
    close_option = {s: proceeds[s] + qty[s] * D(flatten[s]) * m for s in qty}
    close_future = (D(h) * D(flatten["future"]) - future_basis) * M
    close_net = sum(close_option.values()) + close_future
    assert close_option == {"A": D(-48000), "B": D(10200)}
    assert close_future == D(104000) and close_net == D(66200)
    ga = signal.Greeks(.25, .30, .002, 300)
    gb = signal.Greeks(.25, .05, .001, 100)
    assert signal.allocate_short([("A", ga, m), ("B", gb, m)], (.20, .35), 500000, 100, 200) == {"A": -17}
    # 用源码配仓函数独立复核取整与同比缩放示例。
    ra = signal.Greeks(.25, .20, 2, 1)
    rb = signal.Greeks(.25, .35, 3, 1)
    assert signal.allocate_short([("A", ra, m), ("B", rb, m)], (.20, .35), 400, 100, 3) == {"A": -1, "B": -1}
    assert (round(D("2.5")), round(D("3.5")), round(D("1.5"))) == (2, 4, 2)
    # 真实反解只验证价格回代，不声称教学Greeks由案例报价精确校准。
    iv_checks = []
    for symbol in ["A", "B"]:
        premium = next(x["premium"] for x in case["candidates"] if x["symbol"] == symbol)
        iv = signal.source_iv(6000, float(strikes[symbol]), 5 / 252, .02, premium)
        repriced = signal.call_price(6000, float(strikes[symbol]), 5 / 252, .02, iv)
        assert abs(repriced - premium) < 1e-7
        iv_checks.append({"symbol": symbol, "iv": iv, "price_error": repriced - premium})
    handbook = (HERE / "策略流程学习手册.md").read_text()
    answers = (HERE / "习题答案与推导.md").read_text()
    qids = re.findall(r"^### ([BQR]\d{2}) ", handbook, re.M)
    aids = re.findall(r"^## ([BQR]\d{2}) ", answers, re.M)
    assert len(qids) == len(set(qids)) == 34
    assert set(qids) == set(aids)
    parsed = HTMLCheck()
    parsed.feed((HERE / "策略流程学习手册.html").read_text())
    assert len(parsed.ids) == len(set(parsed.ids)), "HTML存在重复锚点"
    assert parsed.answers == parsed.textareas == len(qids)
    assert not parsed.answer_open, "参考答案应默认折叠"
    assert set(parsed.questions) == set(qids)
    checked_links = 0
    for link in parsed.links:
        u = urlsplit(link)
        if u.scheme or u.netloc:
            continue
        if not u.path:
            assert not u.fragment or unquote(u.fragment) in parsed.ids, link
        else:
            assert (HERE / unquote(u.path)).resolve().exists(), link
        checked_links += 1
    result = {
        "document_date_shanghai": "2026-11-19",
        "source_fingerprints": fingerprints,
        "question_count": len(qids), "answer_count": len(aids),
        "local_html_links_checked": checked_links,
        "numeric_validation_passed": True,
        "source_allocate_short_checked": True,
        "iv_price_roundtrip_checks": iv_checks,
        "teaching_greeks_are_independent_synthetic_inputs": True,
        "key_case_results": {
            "new_signed_option_lots": inc,
            "s1_option_goals": {s: str(q) for s, q in s1.items()},
            "s2_actual_options": {s: str(q) for s, q in qty.items()},
            "s2_actual_future": h,
            "s2_mark_to_market_pnl": str(s2_pnl),
            "shock_pnl": str(option_shock + future_shock),
            "static_settlement_ending_net": str(terminal_net),
            "independent_flatten_ending_net": str(close_net),
        },
        "browser_visual_qa": "另见阅读版检查记录.json；此脚本只校验数据及HTML结构",
    }
    (HERE / "来源与校验.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
