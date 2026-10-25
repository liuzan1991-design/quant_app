# -*- coding: utf-8 -*-
"""free-stockdb（本地数据底座）1 分钟线 → 项目标准口径的适配器。

⚠️ 这个模块的存在前提，先说清楚
--------------------------------
free-stockdb **没有**策略容器、回测引擎、下单接口（2026-10-25 核实仓库全量文件清单：
只有 `cpp/` 数据服务、`pybao/` SDK、`调用方式/` 示例、`docs/`，**无 strategy / backtest /
trade / portfolio / broker 任一目录或文件**）。

所以"把策略接进它里面"在字面上做不到。**能做的等价事情是**：
策略仍跑在 quant_app，只把**数据读取**换成它 ⇒ 即这里插一个数据源分支，
与 P1-b 的 `QUANT_KLINE_SOURCE=qmt` 完全同形。**交易侧与它无关**，只能接 QMT xttrader。

为什么要设"校准闸门"
--------------------
同一份行情，不同源的**标签口径 / 成交量单位 / 复权基准**都可能不同，而这三件事
历史上都真出过事：
  - H53：AD 复权因子在 2024-06-06 少算 0.584%；
  - H54：AD 把 14:57 段并入 14:56，QMT 单列 ⇒ 切源直接改策略行为；
  - QMT：标签是 bar 终点、成交量单位是手（×100）。
stockdb 的这三条**我还没有实测数据**（本机未安装）。因此本模块**默认拒绝工作**：
`STOCKDB_CALIBRATED != "1"` 时直接抛 `StockdbNotCalibrated`，让调用方回退 AD。
先用 `scripts/tool_stockdb_probe.py` 跑实测、写进校准变量，才允许放行 ——
**宁可整个源不可用，也不要它悄悄产出错的 K 线。**

校准变量（全部可用环境变量覆盖，默认值 = "未校准"）
------------------------------------------------
  STOCKDB_CALIBRATED    "1" 才放行；否则抛错
  STOCKDB_LABEL_SHIFT   分钟，把源标签平移到 AD 口径（AD = bar 起点）
  STOCKDB_VOLUME_MULT   成交量倍数（AD 单位 = 股）
  STOCKDB_FQ            none | qfq | hfq —— ⚠️ live 必须 none（不复权实价）

红线
----
- 只做纯数据变换 + 一次取数，**不落盘、不写状态**；
- 缺根不静默：每日根数 < 240 的日期写进 `attrs["gaps"]`，由调用方告警；
- 任何异常原样抛，**不吞** —— 回退 AD 的决定权在 `data_fetch`，不在这里。
"""
from __future__ import annotations

import os
import time

import pandas as pd

STD_COLS = ["time", "open", "high", "low", "close", "volume", "amount"]
BARS_PER_DAY = 240                 # 09:30–11:29 + 13:00–14:59
PRICE_COLS = ("open", "high", "low", "close")

DEFAULT_HOST = "127.0.0.1:7899"

_TIME_CANDIDATES = ("date", "time", "datetime", "kline_time", "dt")


class StockdbError(RuntimeError):
    """stockdb 取数失败（未安装 / 未启动 / 返回异常）。"""


class StockdbNotCalibrated(StockdbError):
    """口径未校准，拒绝出数。跑 `scripts/tool_stockdb_probe.py` 后再放行。"""


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def calibration() -> dict:
    """当前校准参数（也供探针与日志展示）。"""
    return {
        "calibrated": os.environ.get("STOCKDB_CALIBRATED", "").strip() == "1",
        "label_shift_min": _float_env("STOCKDB_LABEL_SHIFT", 0.0),
        "volume_mult": _float_env("STOCKDB_VOLUME_MULT", 1.0),
        "fq": (os.environ.get("STOCKDB_FQ") or "none").strip().lower(),
    }


def require_calibrated() -> dict:
    """没校准就抛 —— 这是本模块唯一的安全闸。"""
    cal = calibration()
    if not cal["calibrated"]:
        raise StockdbNotCalibrated(
            "stockdb 口径未校准，拒绝出数。请先运行 "
            "`python scripts/tool_stockdb_probe.py` 实测标签/成交量/复权三条口径，"
            "再设 STOCKDB_CALIBRATED=1 与对应变量。"
        )
    if cal["fq"] not in ("none", "qfq", "hfq"):
        raise StockdbError(f"STOCKDB_FQ 非法：{cal['fq']}（只允许 none|qfq|hfq）")
    return cal


def _import_rd():
    """取 stockdb 的 rd 对象。两种import名都试（仓库里两处文档写法不一致）。

    `pybao/安装.py` 装完是 `from stockdb import rd`；`sdk_test.py` 写的是
    `from stock_sdk import *`。两者都试，避免因版本差异误判"没装"。
    """
    errs = []
    for mod in ("stockdb", "stock_sdk"):
        try:
            m = __import__(mod, fromlist=["rd"])
            rd = getattr(m, "rd", None)
            if rd is not None:
                return rd
            errs.append(f"{mod}: 无 rd 属性")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{mod}: {type(exc).__name__}: {exc}")
    raise StockdbError(
        "未找到 stockdb SDK（" + "；".join(errs) + "）。"
        "请先运行 ./pybao/安装.py，或改用 HTTP 通道。"
    )


def _pick_time_col(df: pd.DataFrame) -> str:
    for c in _TIME_CANDIDATES:
        if c in df.columns:
            return c
    raise StockdbError(f"返回中找不到时间列：{list(df.columns)}")


def normalize_stockdb_1m(raw: pd.DataFrame, cal: dict | None = None) -> pd.DataFrame:
    """stockdb 原始 1m → 项目标准口径（纯函数，可单测）。

    变换全部由**校准参数**驱动，代码里没有任何"猜"的常数：
      - 时间：转 datetime → 平移 `label_shift_min` 分钟（对齐 AD 的 bar 起点口径）
      - 成交量：× `volume_mult`（对齐 AD 的"股"）
      - 其余价格列：直接转 float（复权已由取数时的 fq 决定）
    """
    cal = cal or calibration()
    df = raw.copy()
    if df is None or df.empty:
        raise StockdbError("stockdb 返回空表")

    tc = _pick_time_col(df)
    df["time"] = pd.to_datetime(df[tc], errors="coerce")
    if df["time"].isna().all():
        raise StockdbError(f"时间列 `{tc}` 全部解析失败，样本={df[tc].head(3).tolist()}")
    df = df.dropna(subset=["time"])

    shift = float(cal.get("label_shift_min") or 0.0)
    if shift:
        df["time"] = df["time"] - pd.Timedelta(minutes=shift)

    for col in PRICE_COLS:
        if col not in df.columns:
            raise StockdbError(f"缺少价格列 `{col}`，实际列={list(df.columns)}")
        df[col] = df[col].astype(float)

    mult = float(cal.get("volume_mult") or 1.0)
    if "volume" not in df.columns:
        raise StockdbError(f"缺少成交量列，实际列={list(df.columns)}")
    df["volume"] = (df["volume"].astype(float) * mult).round(0)
    if "amount" not in df.columns:
        df["amount"] = 0.0
    df["amount"] = df["amount"].astype(float)

    df = (df.drop_duplicates(subset=["time"], keep="last")
            .sort_values("time").reset_index(drop=True))
    return df[STD_COLS].copy()


def bar_counts(df: pd.DataFrame) -> dict:
    """每日根数（'YYYY-MM-DD' → n），用于缺根告警。"""
    if df.empty:
        return {}
    s = df["time"].dt.strftime("%Y-%m-%d").value_counts().sort_index()
    return {str(k): int(v) for k, v in s.items()}


def fetch_stockdb_kline(code: str, begin: str, end: str,
                        period: str = "min1") -> pd.DataFrame:
    """从 stockdb 取 1m（或日线），返回标准列 DataFrame。

    `begin`/`end` 为 `YYYY-MM-DD`；转成它要求的 `YYYYMMDD`。
    失败一律抛 `StockdbError` 子类，回退 AD 由调用方决定（对齐 P1-b 的 QMT 分支）。
    """
    cal = require_calibrated()

    if period not in ("min1", "day"):
        raise StockdbError(f"stockdb 通道不支持 period={period}（只 1m / 1d）")
    freq = "1m" if period == "min1" else "1d"

    rd = _import_rd()
    start = begin.replace("-", "")
    stop = end.replace("-", "")
    fq = None if cal["fq"] == "none" else cal["fq"]

    t0 = time.time()
    try:
        raw = rd.get_data(code=code, start=start, end=stop,
                          frequency=freq, fq=fq, as_df=True)
    except Exception as exc:  # noqa: BLE001
        raise StockdbError(f"stockdb 取数失败：{type(exc).__name__}: {exc}") from exc

    if isinstance(raw, pd.DataFrame):
        df = normalize_stockdb_1m(raw, cal)
    else:
        df = normalize_stockdb_1m(pd.DataFrame(raw), cal)

    counts = bar_counts(df) if period == "min1" else {}
    gaps = {d: n for d, n in counts.items() if n < BARS_PER_DAY}
    df.attrs["gaps"] = gaps
    df.attrs["bar_counts"] = counts
    df.attrs["source"] = "stockdb"
    df.attrs["calibration"] = cal
    df.attrs["elapsed_s"] = round(time.time() - t0, 3)
    return df
