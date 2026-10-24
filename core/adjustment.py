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


# ── P1-a：可切换的第二因子源（QMT），默认仍走 AD ────────────────────────────
# 开关：环境变量 QUANT_FACTOR_SOURCE=ad|qmt（进程级，便于小范围试跑）。
# 约定：未设置/非法值 ⇒ ad（**默认行为与接线前逐字节一致**）。
_FACTOR_SOURCE_ENV = "QUANT_FACTOR_SOURCE"


def _factor_qmt_file(code: str) -> Path:
    """QMT 口径因子缓存，**与 AD 缓存分开命名**，避免互相污染。"""
    return DATA_DIR / f"factor_qmt_{code}.csv"


def _qmt_code(code: str) -> str:
    code = code.strip().upper()
    return code if "." in code else f"{code}.SZ"


def rebuild_factor_from_events(event_dates: list, drs: list,
                               dates) -> "pd.Series":
    """QMT 逐事件 `dr` → 与 AD **同向**的累计因子：`f(t) = 1 / Π{dr : event_date > t}`。

    方向说明（H53 验证时踩过）：AD 因子是 `Π{事件≤t}`（随 t 递增，末值 6.61）；
    QMT 原始重建是 `Π{事件>t}`（随 t 递减，末值 1.0）。下游只用比值
    `f(t)/f(末)`，因此取倒数即可与 AD 完全等价，且保持**递增方向**——
    这点很关键：`corporate_actions.verify_against_factor` 靠 `pct_change()>0`
    找跳变日，方向反了会静默失效。
    """
    dates = pd.DatetimeIndex(dates)
    out = []
    for d in dates:
        prod = 1.0
        for ed, dr in zip(event_dates, drs):
            if ed > d:
                prod *= float(dr)
        out.append(1.0 / prod)
    return pd.Series(out, index=dates)


def _fetch_factor_qmt(code: str) -> pd.Series:
    """从 QMT 取逐事件复权信息并重建因子；日期骨架取 AD 缓存（保证两条源可比）。

    只调 `xtdata.get_divid_factors`（行情通道），**不 import xttrader**。
    """
    skeleton = _read_cache(_factor_file(code))
    if skeleton is None or not len(skeleton):
        raise ValueError("缺少 AD 因子缓存作为日期骨架，无法重建 QMT 因子")
    from xtquant import xtdata  # 延迟 import：纯 AD 路径不加载 xtquant

    q = xtdata.get_divid_factors(_qmt_code(code))
    if q is None or len(q) == 0:
        _warn_once((code, "qmt_no_event"),
                   f"[WARN] {code} QMT 未返回复权事件 ⇒ 因子恒 1.0（等同不复权）")
        return pd.Series(1.0, index=skeleton.index)
    if "dr" not in q.columns:
        raise ValueError("QMT 返回缺少 dr 列，无法重建因子")
    dt = (pd.to_datetime(q["time"], unit="ms").dt.tz_localize("UTC")
          .dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize())
    order = dt.argsort()
    return rebuild_factor_from_events(list(dt.iloc[order]), list(q["dr"].iloc[order]),
                                      skeleton.index)


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


def _load_factor_qmt(code: str, refresh: bool = False) -> pd.Series:
    """QMT 口径因子的加载/刷新/缓存；**任何失败一律回退 AD**（绝不降级为不复权）。"""
    target = _factor_qmt_file(code)
    cached = _read_cache(target)
    if not refresh and not _cache_is_stale(cached):
        return cached
    key = (code, "qmt:" + pd.Timestamp.now().strftime("%Y-%m-%d"))
    if not refresh and key in _REFRESH_ATTEMPTED:
        return cached if cached is not None else load_backward_factor(code, refresh=refresh, source="ad")
    _REFRESH_ATTEMPTED.add(key)
    try:
        series = _fetch_factor_qmt(code)
    except Exception as exc:  # noqa: BLE001
        _warn_once((code, "qmt_refresh"),
                   f"[WARN] QMT 复权因子获取失败（{type(exc).__name__}: {exc}），"
                   f"本次回退 AD 口径")
        return load_backward_factor(code, refresh=refresh, source="ad")
    _write_cache(target, series)
    return series


def load_backward_factor(code: str, refresh: bool = False,
                         source: str | None = None) -> pd.Series:
    """读取后复权因子。

    `source`（或环境变量 `QUANT_FACTOR_SOURCE`）可选 `ad`（默认）/`qmt`（P1-a 接线）。
    QMT 分支的失败一律回退 AD，不会静默变成"不复权"。

    缓存过期会自动尝试刷新一次（进程内每 code 每天最多一次）；刷新失败时回退旧
    缓存并告警，不中断调用方。仅当既无可用缓存、取数又失败时抛异常，由
    prepare_signal_prices 降级并暂停异常日信号。
    """
    src = (source or os.environ.get(_FACTOR_SOURCE_ENV, "ad")).strip().lower()
    if src == "qmt":
        return _load_factor_qmt(code, refresh=refresh)
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
        # 目标索引可重复/非单调。
        series = factor.reindex(pd.DatetimeIndex(dates), method="ffill")
        # ⚠️ 早于因子起点的行**不做 bfill**：bfill 等于拿"未来"的复权因子回填到过去
        # （H16 指出的泄漏模式）。当前行情起点 2023-01 晚于因子起点 1990-12 ⇒ 恒为
        # 空操作；一旦真的越界，宁可抛错降级 + WARN，也不静默用未来值算出"看起来正常"的信号。
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
