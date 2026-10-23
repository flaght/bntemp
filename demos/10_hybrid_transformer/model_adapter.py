"""把 Mizar 已有逐 bar Workflow 接到 Bomber；不重写特征或信号算法。"""

from __future__ import annotations

from importlib import import_module
from math import isfinite
from pathlib import Path
from typing import Mapping


# 两组研究期已选的信号口径；未来版本必须显式新增编号和参数。
SIGNAL_PRESETS = {
    "1101": ("linear_signal", {"roll_num": 0, "threshold": 0.05}),
    "1301": ("threshold_signal", {"roll_num": 0, "threshold": 0.02}),
}


class MizarWorkflowAdapter:
    """使用 Mizar 的特征、标准化、三 seed 融合及 Lumina 信号函数。"""

    def __init__(self, *, release_path: str | Path,
                 directions_file: str | Path, code: str, signal_id: str,
                 history_bars: int = 1500, min_raw_bars: int = 300,
                 device: str = "auto") -> None:
        self.code = str(code).upper()
        self.signal_id = str(signal_id)
        if self.signal_id not in SIGNAL_PRESETS:
            raise ValueError(f"未固化的信号编号: {self.signal_id}")
        if self.code not in {"P", "RB"}:
            raise ValueError("本适配器首版只支持已验证的 P、RB")
        expected = {"P": "1101", "RB": "1301"}[self.code]
        if self.signal_id != expected:
            raise ValueError(f"{self.code} 当前固化信号编号为 {expected}")
        method, params = SIGNAL_PRESETS[self.signal_id]
        try:
            workflow_module = import_module("lib.nnl005.workflow")
        except ImportError as exc:
            raise ImportError(
                "请将 Mizar 项目根目录加入 PYTHONPATH，并安装其 torch/Lumina/Ultron 依赖"
            ) from exc
        self.workflow = workflow_module.Workflow(
            release_path=str(release_path),
            directions_file=str(directions_file),
            code=self.code,
            device=device,
            history_bars=history_bars,
            min_raw_bars=min_raw_bars,
            signals_method=[{"signal_method": method,
                             "signal_params": dict(params)}],
        )
        self.signal_method = method
        self.signal_params = dict(params)

    def on_bar(self, bar: Mapping[str, object]) -> dict | None:
        if str(bar.get("code", "")).upper() != self.code:
            raise ValueError("模型 bar 品种不匹配")
        result = self.workflow.on_bar(dict(bar))
        if result is None:
            return None
        if len(result) != 1:
            raise ValueError("单品种、单信号配置应恰好返回一条记录")
        row = result.iloc[0]
        if str(row["method"]) != self.signal_method:
            raise ValueError("Mizar 返回的信号函数与配置不一致")
        z, signal = float(row["predicted_z"]), float(row["signal"])
        if not isfinite(z) or not isfinite(signal) or abs(signal) > 1:
            raise ValueError("Mizar 返回非有限或越界预测/信号")
        return {
            "trade_time": bar["trade_time"],
            "code": self.code,
            "predicted_z": z,
            "signal": signal,
            "signal_id": self.signal_id,
            "signal_method": self.signal_method,
        }
