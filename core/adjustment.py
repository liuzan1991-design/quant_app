# -*- coding: utf-8 -*-
"""复权辅助：成交仍用真实价格，技术信号可使用前复权序列，并标记公司行为日。"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd

from core import data_fetch

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# 因子表按交易日逐行记录，末行落后今日超过该自然日数即视为过期（覆盖春节等长假）。
_STALE_MAX_LAG_DAYS = 10
# 进程内每 (code, 当日) 最多尝试刷新一次：live 每轮都会调用本函数，若不做节流
# 会在过期条件下反复走 AD 登录，触发 H37/H44 的失败即重登链路。
_REFRESH_ATTEMPTED: set[tuple[str, str]] = set()
# 告警去重，避免 live 每轮刷日志。
_WARNED: set[tuple[str, str]] = set()


def _factor_file(code: str) -> Path:
    return DATA_DIR / f"factor_{code}.csv"


def _warn_once(key: tuple[str, str], message: str) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(message, flush=True)


def _read_cache(target: Path) -> pd.Series | None:
    """读取本地因子缓存；格式不符或为空时返回 None。"""
    if not target.exists():
        return None
    try:
        cached = pd.read_csv(target)
    except Exception:
        return None
    if not len(cached) or not {"date", "factor"}.issubset(cached.columns):
        return None
    series = pd.Series(cached["factor"].astype(float).values,
                       index=pd.to_datetime(cached["date"]).dt.normalize())
    return series[~series.index.duplicated(keep="last")].sort_index()


def _cache_is_stale(series: pd.Series | None, today: pd.Timestamp | None = None) -> bool:
    if series is None or not len(series):
        return True
    today = pd.Timestamp.now().normalize() if today is None else today
    return bool((today - series.index[-1]).days > _STALE_MAX_LAG_DAYS)


def _fetch_factor(code: str) -> pd.Series:
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
    return series[~series.index.duplicated(keep="last")].sort_index()


def _write_cache(target: Path, series: pd.Series) -> None:
    """先写临时文件再原子替换，避免写到一半中断留下损坏缓存。"""
    tmp = target.with_name(target.name + ".tmp")
    pd.DataFrame({"date": series.index, "factor": series.values}).to_csv(
        tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, target)


def load_backward_factor(code: str, refresh: bool = False) -> pd.Series:
    """读取后复权因子。

    缓存过期会自动尝试刷新一次（进程内每 code 每天最多一次）；刷新失败时回退旧
    缓存并告警，不中断调用方。仅当既无可用缓存、取数又失败时抛异常，由
    prepare_signal_prices 降级并暂停异常日信号。
    """
    target = _factor_file(code)
    cached = _read_cache(target)
    if not refresh and not _cache_is_stale(cached):
        return cached
    key = (code, pd.Timestamp.now().strftime("%Y-%m-%d"))
    if not refresh and key in _REFRESH_ATTEMPTED:
        return cached
    _REFRESH_ATTEMPTED.add(key)
    try:
        series = _fetch_factor(code)
    except Exception as exc:
        if refresh or cached is None:
            raise
        _warn_once((code, "refresh"),
                   f"[WARN] 复权因子刷新失败（{type(exc).__name__}: {exc}），"
                   f"回退缓存，末日 {cached.index[-1].date()}")
        return cached
    _write_cache(target, series)
    return series


def prepare_signal_prices(df: pd.DataFrame, code: str = "") -> tuple[pd.DataFrame, list, str]:
    """返回供指标使用的数据、公司行为日、口径说明；OHLC成交列不被覆盖。"""
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"])
    dates = out["time"].dt.normalize()
    try:
        factor = load_backward_factor(code)
        # 不能写 reindex(dates).ffill()：reindex 先把索引裁成 dates，结果集里不再包含
        # 因子末日那一行；当 dates 全部晚于因子末日（live 每轮只喂当日窗口）时整列
        # NaN，ffill 无源可填 ⇒ 必然降级。用 method="ffill" 直接从源索引取值，
        # 目标索引可重复/非单调；早于因子起点时仍以 bfill 兜底（与旧行为一致）。
        series = factor.reindex(pd.DatetimeIndex(dates), method="ffill")
        # 不做 bfill：早于因子起点的行拿不到因子，bfill 等于拿"未来"的复权因子
        # 回填到过去（H16 指出的泄漏模式）。当前数据起点 2023-01 晚于因子起点
        # 2012-07，恒为空操作；一旦真的越界，宁可降级并告警（下面会 WARN），
        # 也不静默用未来值算出一份看起来正常的信号。
        aligned = series.to_numpy(dtype=float)
        if not len(aligned) or np.isnan(aligned).any():
            raise ValueError("复权因子未覆盖全部日期（存在早于因子起点的 bar）")
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
    except Exception as exc:
        _warn_once((code or "?", "degrade"),
                   f"[WARN] 复权信号降级为不复权（{type(exc).__name__}: {exc}）")
        for col in ("open", "high", "low", "close"):
            out[f"signal_{col}"] = out[col].astype(float)
        daily = out.groupby(dates)["close"].last()
        events = list(daily.pct_change().abs().loc[lambda s: s > 0.095].index.date)
        out.attrs.update(df.attrs)
        out.attrs["adjustment"] = "不复权降级（异常跳变日暂停信号）"
        out.attrs["corporate_action_dates"] = events
        return out, events, out.attrs["adjustment"]
