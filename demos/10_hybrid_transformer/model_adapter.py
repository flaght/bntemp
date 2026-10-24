"""把 已有逐 bar Workflow 接到 Bomber；不重写特征或信号算法。"""

from __future__ import annotations

from importlib import import_module
from math import isfinite
from pathlib import Path
from typing import Mapping
from . import flow as live_flow



class MizarWorkflowAdapter:
    """使用  的特征、标准化、三 seed 融合及  信号函数。"""
    def __init__(self, *, release_path: str | Path,
                 directions_file: str | Path, code: str, signal_id: str,
                 signal_method: str, signal_params: Mapping[str, object],
                 history_bars: int = 1500, min_raw_bars: int = 300,
                 device: str = "auto") -> None:
        
        self.code = str(code).strip().upper()
        self.signal_id = str(signal_id).strip()
        self.signal_method = str(signal_method).strip()
        if not self.code.isalpha() or not self.signal_id or not self.signal_method:
            raise ValueError("品种、信号编号和信号函数名不能为空")
        if not isinstance(signal_params, Mapping):
            raise ValueError("signal_params 须为参数字典")
        self.signal_params = dict(signal_params)
        if self.signal_method not in live_flow.SIGNAL_FUNCTIONS:
            raise ValueError(f"未注册信号函数: {self.signal_method}")
        
        self.workflow = live_flow.LiveFlow(
            release_path=str(release_path),
            directions_file=str(directions_file),
            code=self.code,
            device=device,
            history_bars=history_bars,
            min_raw_bars=min_raw_bars,
            signals_method=[{"signal_method": self.signal_method,
                             "signal_params": dict(self.signal_params)}],
        )

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
        # Workflow.on_bar 返回按品种展开的宽表；同时兼容显式 signal 列。
        signal_column = "signal" if "signal" in result.columns else self.code
        if signal_column not in result.columns:
            raise ValueError(f"Mizar 返回结果缺少 signal 或 {self.code} 列")
        z, signal = float(row["predicted_z"]), float(row[signal_column])
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