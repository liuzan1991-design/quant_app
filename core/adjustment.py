# -*- coding: utf-8 -*-
"""复权辅助：成交仍用真实价格，技术信号可使用前复权序列，并标记公司行为日。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from core import data_fetch

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _factor_file(code: str) -> Path:
    return DATA_DIR / f"factor_{code}.csv"


def load_backward_factor(code: str, refresh: bool = False) -> pd.Series:
    """读取后复权因子。接口不可用时抛出异常，由调用方降级并暂停异常日信号。"""
    target = _factor_file(code)
    if target.exists() and not refresh:
        cached = pd.read_csv(target)
        if len(cached) and {"date", "factor"}.issubset(cached.columns):
            return pd.Series(cached["factor"].astype(float).values,
                             index=pd.to_datetime(cached["date"]).dt.normalize())
    ad = data_fetch._login()
    raw = ad.BaseData().get_backward_factor([data_fetch.code_to_ad(code)], is_local=False)
    obj = raw.get(data_fetch.code_to_ad(code)) if isinstance(raw, dict) else raw[data_fetch.code_to_ad(code)]
    if isinstance(obj, pd.DataFrame):
        numeric = [c for c in obj.columns if pd.api.types.is_numeric_dtype(obj[c])]
        if not numeric:
            raise ValueError("复权因子没有数值列")
        series = obj[numeric[0]]
    else:
        series = pd.Series(obj)
    series = pd.to_numeric(series, errors="coerce").dropna()
    series.index = pd.to_datetime(series.index).normalize()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    pd.DataFrame({"date": series.index, "factor": series.values}).to_csv(
        target, index=False, encoding="utf-8-sig")
    return series


def prepare_signal_prices(df: pd.DataFrame, code: str = "") -> tuple[pd.DataFrame, list, str]:
    """返回供指标使用的数据、公司行为日、口径说明；OHLC成交列不被覆盖。"""
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"])
    dates = out["time"].dt.normalize()
    try:
        factor = load_backward_factor(code)
        aligned = factor.reindex(dates).ffill().bfill().to_numpy(dtype=float)
        if not len(aligned) or np.isnan(aligned).all():
            raise ValueError("复权因子无法对齐")
        latest = float(pd.Series(aligned).dropna().iloc[-1])
        ratio = aligned / latest
        for col in ("open", "high", "low", "close"):
            out[f"signal_{col}"] = out[col].astype(float).to_numpy() * ratio
        day_factor = pd.Series(aligned, index=dates).groupby(level=0).last()
        events = [d.date() for d, changed in day_factor.pct_change().abs().gt(1e-8).items() if changed]
        out.attrs.update(df.attrs)
        out.attrs["adjustment"] = "真实价成交 + 前复权信号"
        out.attrs["corporate_action_dates"] = events
        return out, events, out.attrs["adjustment"]
    except Exception:
        for col in ("open", "high", "low", "close"):
            out[f"signal_{col}"] = out[col].astype(float)
        daily = out.groupby(dates)["close"].last()
        events = list(daily.pct_change().abs().loc[lambda s: s > 0.095].index.date)
        out.attrs.update(df.attrs)
        out.attrs["adjustment"] = "不复权降级（异常跳变日暂停信号）"
        out.attrs["corporate_action_dates"] = events
        return out, events, out.attrs["adjustment"]
