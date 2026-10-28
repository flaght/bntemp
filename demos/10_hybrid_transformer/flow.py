import torch, json, os, itertools
import pandas as pd
import numpy as np
from pathlib import Path

from ultron.sentry.api import *

os.environ.setdefault("IMPLUSE_VERSION", "all")
import lumina.env as lumina_env
from lumina.formual.iactuator import Iactuator
from lumina.evolution.fusion.actuator import Actuator
from lumina.genetic.signal.method.linear import linear_signal
from lumina.genetic.signal.method.linear_threshold import linear_threshold_signal
from lumina.genetic.signal.method.threshold import threshold_signal

from .model import HybridTransformerRegressor

lumina_env.g_format = 2
BAR_COLUMNS = ("trade_time", "code", "close", "high", "value", "low", "open",
               "volume", "vwap", "openint")
RAW_COLUMNS = BAR_COLUMNS[2:]
MODEL_VERSION = "hybrid_transformer_loss_v1"
SIGNAL_FUNCTIONS = {
    "linear_signal": linear_signal,
    "linear_threshold_signal": linear_threshold_signal,
    "threshold_signal": threshold_signal,
}


def _run_dirs(release_path):
    root = Path(release_path)
    if not root.is_dir():
        raise FileNotFoundError(root)
    result = {}
    for seed_dir in sorted(root.iterdir()):
        if seed_dir.is_dir() and seed_dir.name.isdigit():
            candidates = [p for p in seed_dir.iterdir() if p.is_dir()]
            if len(candidates) != 1:
                raise ValueError(f"{seed_dir} 应恰好包含一个 run_id 目录")
            result[int(seed_dir.name)] = candidates[0]
    if len(result) < 2:
        raise ValueError("等权融合至少需要两个 seed")
    return result


def _checkpoint(path):
    try:
        return torch.load(path, map_location="cpu", weights_only=False)
    except TypeError:  # PyTorch 1.7
        return torch.load(path, map_location="cpu")


def _factors_infos(path, features):
    table = pd.read_csv(path, encoding="utf-8-sig")
    if not {"formula", "direction"}.issubset(table.columns):
        raise ValueError("方向文件必须包含 formula、direction")
    if table["formula"].duplicated().any():
        raise ValueError("方向文件存在重复 formula")
    mapping = dict(zip(table.formula, table.direction))
    if set(features) - set(mapping):
        raise ValueError(f"方向文件缺少特征: {sorted(set(features) - set(mapping))}")
    infos = []
    for name in features:
        direction = float(mapping[name])
        if direction not in (-1.0, 1.0):
            raise ValueError(f"方向不是 ±1: {name}")
        infos.append({"formula": name, "direction": int(direction)})
    return infos


class LiveFlow:

    def __init__(self,
                 release_path,
                 directions_file,
                 code,
                 *,
                 device="auto",
                 history_bars=1500,
                 min_raw_bars=300,
                 signals_method=None):

        self.code = str(code).upper()
        self.history_bars = int(history_bars)
        self.min_raw_bars = int(min_raw_bars)

        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = torch.device(device)
        if self.device.type == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA 不可用")

        self.models, self.run_ids = {}, {}
        reference = None
        for seed, run_dir in _run_dirs(release_path).items():
            config = json.loads(
                (run_dir / "config.json").read_text(encoding="utf-8"))
            checkpoint = _checkpoint(run_dir / "models" / "best_model.pt")
            if int(config["env_config"]["seed"]) != seed:
                raise ValueError(f"{run_dir}: seed 不匹配")
            if checkpoint.get("version") != MODEL_VERSION:
                raise ValueError(f"{run_dir}: checkpoint 版本不匹配")
            for field in ("features", "lookback", "ret_scale_by_code",
                          "loss_config"):
                if checkpoint[field] != config[field]:
                    raise ValueError(f"{run_dir}: {field} 与 checkpoint 不一致")
            if checkpoint["model_config"] != config["architecture"]:
                raise ValueError(f"{run_dir}: 网络结构不一致")
            signature = {
                key: config[key]
                for key in ("features", "lookback", "ret_scale_by_code",
                            "architecture", "loss_config")
            }

            if reference is None:
                reference = signature
            elif signature != reference:
                raise ValueError(f"{run_dir}: 多 seed 配置不一致")
            model = HybridTransformerRegressor(**config["architecture"])
            model.load_state_dict(checkpoint["model_state_dict"], strict=True)
            self.models[seed] = model.to(self.device).eval()
            self.run_ids[seed] = run_dir.name

        self.features = list(reference["features"])
        self.lookback = int(reference["lookback"])
        self.ret_scale = float(reference["ret_scale_by_code"][self.code])
        if not np.isfinite(self.ret_scale) or self.ret_scale <= 0:
            raise ValueError("训练集收益率标准差无效")

        self.factors_infos = _factors_infos(directions_file, self.features)
        self.signals_method = signals_method

        dependencies = set(
            itertools.chain.from_iterable(
                eval(name)._dependency for name in self.features))

        self.impulse = Iactuator(k_split=1)
        self.actuator = Actuator(k_split=1)

        prefixes = {
            name.split("_", 1)[0]
            for name in dependencies if name not in RAW_COLUMNS
        }

        self.impulse.factors = [
            factor for factor in self.impulse.factors
            if factor.name in prefixes
        ]

        if prefixes - {factor.name for factor in self.impulse.factors}:
            raise ValueError("Lumina 未提供全部基础因子计算器")
        self.history = []

    def warm_history(self, rows):
        """批量装入完整研究口径行情；不生成或发布历史目标。"""
        prepared = []
        previous = None
        for bar in rows:
            missing = set(BAR_COLUMNS) - set(bar)
            if missing:
                raise ValueError(f"预热行情缺少字段: {sorted(missing)}")
            row = {name: bar[name] for name in BAR_COLUMNS}
            row["trade_time"] = pd.Timestamp(row["trade_time"])
            row["code"] = str(row["code"]).upper()
            if row["code"] != self.code or (previous is not None and row["trade_time"] <= previous):
                raise ValueError("预热品种不一致或时间重复/倒退")
            previous = row["trade_time"]
            prepared.append(row)
        self.history = prepared[-self.history_bars:]
        return len(self.history)

    def _current_factors(self):
        return self._calculate_factors(pd.DataFrame.from_records(
            self.history))[2]

    def _calculate_factors(self, bars):
        wide = bars.set_index(["trade_time",
                               "code"])[list(RAW_COLUMNS)].unstack("code")
        source = {name: wide[name] for name in RAW_COLUMNS}
        raw = self.impulse.calculate(total_data=source)
        raw = raw.ffill().fillna(0)
        raw.index = raw.index.set_names(["trade_time", "code"])
        raw = raw.reset_index()
        base = bars.merge(raw,
                          on=["trade_time", "code"],
                          how="left",
                          validate="one_to_one")

        factors, normal = self.actuator.calculate(
            factors_infos=self.factors_infos,
            total_data=base,
            method="roll_zscore",
            win=15)
        return base, factors, normal

    def on_bar(self, bar):
        missing = set(BAR_COLUMNS) - set(bar)
        if missing:
            raise ValueError(f"bar 缺少字段: {sorted(missing)}")

        row = {name: bar[name] for name in BAR_COLUMNS}
        row["trade_time"] = pd.Timestamp(row["trade_time"])
        # row["code"] = str(row["code"]).upper()
        self.history.append(row)
        if len(self.history) > self.history_bars:
            self.history.pop(0)
        if len(self.history) < self.min_raw_bars:
            return None

        normal = self._current_factors()

        current = row["trade_time"]
        if (current, self.code) not in normal.index:
            raise ValueError(f"{current}: Sirius 未生成当前标准化特征")

        self.last_features = {
            "trade_time": current,
            "code": self.code,
            **normal.loc[(current, self.code), self.features].to_dict()
        }

        sequence = normal.loc[(slice(None), self.code), self.features].copy()
        sequence.index = sequence.index.get_level_values(0)
        sequence = sequence.loc[:current]

        gaps = sequence.index.to_series().diff().dt.total_seconds().ne(60)
        segment = sequence.iloc[np.flatnonzero(gaps.to_numpy())[-1]:]
        observed = np.nan_to_num(segment.tail(
            self.lookback).to_numpy(dtype=np.float32),
                                 nan=0.0,
                                 posinf=0.0,
                                 neginf=0.0)

        count = len(observed)
        if count == 0:
            raise ValueError("当前时点没有模型输入")
        if count < self.lookback:
            observed = np.vstack([
                np.repeat(observed[:1], self.lookback - count, axis=0),
                observed
            ])

        x = torch.from_numpy(observed[None, :, :]).to(self.device)
        context = getattr(torch, "inference_mode", torch.no_grad)
        with context():
            per_seed = {
                seed: float(model(x).item())
                for seed, model in self.models.items()
            }
        z = float(np.mean(list(per_seed.values())))

        result = {
            "trade_time": current,
            "code": self.code,
            "predicted_z": z,
            "predicted_ret_h": z * self.ret_scale,
            "per_seed_predicted_z": per_seed,
            "history_bars": count,
            "left_padded": count < self.lookback
        }
        factor_data = pd.DataFrame({
            "trade_time": [current],
            "code": [self.code],
            "transformed": [z]
        }).set_index(["trade_time", "code"])

        res = []
        for method in self.signals_method:
            signal_method = method['signal_method']
            signal_params = method['signal_params']
            signal = SIGNAL_FUNCTIONS[signal_method](factor_data=factor_data,
                                                     **signal_params)
            signal['method'] = signal_method
            signal['predicted_z'] = z
            res.append(signal)
        results = pd.concat(res, axis=0)
        return results.reset_index()

    def predict_batch(self, bars, batch_size=256):
        if not isinstance(batch_size, (int, np.integer)) or batch_size <= 0:
            raise ValueError("batch_size 必须是正整数")

        bars = pd.DataFrame(bars)
        missing = set(BAR_COLUMNS) - set(bars.columns)
        if missing:
            raise ValueError(f"bar 缺少字段: {sorted(missing)}")
        bars = bars.loc[:, list(BAR_COLUMNS)].copy()

        bars["trade_time"] = pd.to_datetime(bars["trade_time"], errors="raise")
        bars = bars.sort_values("trade_time").reset_index(drop=True)

        base, factors, normal = self._calculate_factors(bars)

        sequence = normal.loc[normal.index.get_level_values("code") ==
                              self.code,
                              self.features].droplevel("code").sort_index()

        predictions = bars.iloc[max(self.min_raw_bars -
                                    1, 0):][["trade_time",
                                             "code"]].reset_index(drop=True)

        positions = sequence.index.get_indexer(predictions["trade_time"])
        if (positions < 0).any():
            missing_time = predictions.loc[positions < 0, "trade_time"].iloc[0]
            raise ValueError(f"{missing_time}: Sirius 未生成标准化特征")

        values = np.nan_to_num(sequence.to_numpy(dtype=np.float32),
                               nan=0.0,
                               posinf=0.0,
                               neginf=0.0)
        gaps = sequence.index.to_series().diff().dt.total_seconds().ne(60)
        starts = np.maximum.accumulate(
            np.where(gaps.to_numpy(), np.arange(len(sequence)), 0))
        lags = np.arange(self.lookback - 1, -1, -1)
        counts = np.minimum(positions - starts[positions] + 1, self.lookback)
        per_seed = {seed: np.empty(len(positions), dtype=np.float64)
                    for seed in self.models}
        context = getattr(torch, "inference_mode", torch.no_grad)
        with context():
            for left in range(0, len(positions), batch_size):
                chunk = positions[left:left + batch_size]
                windows = np.maximum(chunk[:, None] - lags,
                                     starts[chunk, None])
                x = torch.from_numpy(values[windows]).to(self.device)
                for seed, model in self.models.items():
                    per_seed[seed][left:left + len(chunk)] = (
                        model(x).detach().cpu().numpy().reshape(-1))

        predictions["predicted_z"] = np.mean(list(per_seed.values()), axis=0)
        predictions["predicted_ret_h"] = predictions["predicted_z"] * self.ret_scale
        predictions["history_bars"] = counts
        predictions["left_padded"] = counts < self.lookback
        for seed, values in per_seed.items():
            predictions[f"predicted_z_seed_{seed}"] = values

        factor_data = predictions.set_index(["trade_time", "code"])[
            ["predicted_z"]].rename(columns={"predicted_z": "transformed"})
        signals = []
        if not predictions.empty:
            for method in self.signals_method or []:
                signal_method = method["signal_method"]
                signal = SIGNAL_FUNCTIONS[signal_method](
                    factor_data=factor_data, **method["signal_params"])
                signal = signal.rename_axis(columns="code").stack().rename(
                    "signal").reset_index()
                signal["method"] = signal_method
                signals.append(signal.merge(
                    predictions[["trade_time", "code", "predicted_z"]],
                    on=["trade_time", "code"], validate="one_to_one"))
        return {
            "base": base,
            "factors": factors.reset_index(),
            "normal": normal.reset_index(),
            "predictions": predictions,
            "signals": pd.concat(signals, ignore_index=True) if signals else
                       pd.DataFrame(columns=["trade_time", "code", "signal",
                                             "method", "predicted_z"]),
        }
