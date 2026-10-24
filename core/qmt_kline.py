# -*- coding: utf-8 -*-
"""P1-b：QMT（xtdata）1 分钟线 → 项目标准口径的规范化。

背景
----
项目全链路（`fetch_kline` 的消费者）默认列 `[time, open, high, low, close, volume, amount]`，
且沿用 AD 的三条口径（12 个交易日实测恒定，见 `D:\\Codex输出\\P1-b切源验收清单_20261024.md`）：

  1. **标签 = bar 起点**；QMT 的标签是 **bar 终点** ⇒ 平移 **−1 分钟**；
  2. **成交量单位 = 股**；QMT 是 **手** ⇒ **×100**；
  3. **开盘集合竞价并入 09:30**：QMT 单列一根（平移后落在 09:29），
     合并规则（实测 12/12 全中）：
       open   = 竞价根.open
       close  = 首根连续分钟.close
       high   = max(两根.high)      low = min(两根.low)
       volume = (两根.volume 之和) × 100
  4. **尾盘 14:57 归属**（H54）：AD 把首根 14:57 段的成交并入 **14:56**，
     14:57/14:58 只留零量承接根；QMT 单列在 14:57（平移后）。
     ⇒ 为「切源不改策略行为」，这里按 AD 口径对齐：14:57 并入 14:56，并补一根零量 14:57。

红线
----
- 只做**纯数据变换**（`normalize_qmt_1m` 无 IO，可直接单测）；
- 缺根不静默：`bar_counts` 记每日根数，少于 240 的日期写进 `attrs["gaps"]` 并由调用方告警。
"""
from __future__ import annotations

import pandas as pd

STD_COLS = ["time", "open", "high", "low", "close", "volume", "amount"]
BARS_PER_DAY = 240          # 09:30–11:29 + 13:00–14:59
PRICE_COLS = ("open", "high", "low", "close")


def qmt_code(code: str) -> str:
    code = code.strip().upper()
    return code if "." in code else f"{code}.SZ"


def _to_beijing(ms: pd.Series) -> pd.Series:
    """QMT 的 1m 索引是 epoch(ms)，按交易所时区换算（不硬编码时差，H25 口径）。"""
    return (pd.to_datetime(ms, unit="ms").dt.tz_localize("UTC")
            .dt.tz_convert("Asia/Shanghai").dt.tz_localize(None))


def normalize_qmt_1m(raw: pd.DataFrame) -> pd.DataFrame:
    """QMT 原始 1m → 项目标准口径（见模块 docstring）。"""
    df = raw.copy()
    df["time"] = _to_beijing(df["time"]) - pd.Timedelta(minutes=1)   # 口径①标签平移
    df["volume"] = (df["volume"].astype(float) * 100).round(0)       # 口径②手→股
    for col in PRICE_COLS + ("amount",):
        df[col] = df[col].astype(float)
    df = (df.drop_duplicates(subset=["time"], keep="last")
            .sort_values("time").reset_index(drop=True))
    df["hm"] = df["time"].dt.strftime("%H:%M")

    out = []
    for day, g in df.groupby(df["time"].dt.normalize(), sort=True):
        g = g.copy()
        # 口径③：竞价根（09:29）+ 首根连续分钟（09:30）→ 一根 09:30
        if "09:29" in set(g["hm"]) and "09:30" in set(g["hm"]):
            auc = g[g["hm"] == "09:29"].iloc[0]
            first = g[g["hm"] == "09:30"].iloc[0]
            merged = pd.DataFrame([{
                "time": day + pd.Timedelta(hours=9, minutes=30),
                "open": float(auc["open"]),
                "high": float(max(auc["high"], first["high"])),
                "low": float(min(auc["low"], first["low"])),
                "close": float(first["close"]),
                "volume": float(auc["volume"]) + float(first["volume"]),
                "amount": float(auc["amount"]) + float(first["amount"]),
            }])
            g = pd.concat([merged, g[~g["hm"].isin(["09:29", "09:30"])]], ignore_index=True)
            g["hm"] = g["time"].dt.strftime("%H:%M")

        # 口径④（H54）：14:57 段并入 14:56，补一根零量 14:57 —— 对齐 AD 的 bar 布局
        has = set(g["hm"])
        if {"14:56", "14:57"} <= has and float(g.loc[g["hm"] == "14:57", "volume"].iloc[0]) > 0:
            i56 = g.index[g["hm"] == "14:56"][0]
            i57 = g.index[g["hm"] == "14:57"][0]
            a, b = g.loc[i56], g.loc[i57]
            g.loc[i56, ["high", "low", "close", "volume", "amount"]] = [
                max(a["high"], b["high"]), min(a["low"], b["low"]),
                b["close"], a["volume"] + b["volume"], a["amount"] + b["amount"]]
            g.loc[i57, ["open", "high", "low", "close", "volume", "amount"]] = [
                b["close"], b["close"], b["close"], b["close"], 0.0, 0.0]
        out.append(g)

    res = pd.concat(out, ignore_index=True).sort_values("time").reset_index(drop=True)
    res.drop(columns=["hm"], inplace=True)

    # 缺口不静默：按日计数，少于 240 根的日期登记
    counts = res.groupby(res["time"].dt.date).size()
    gaps = {str(d): int(n) for d, n in counts.items() if n < BARS_PER_DAY}
    res.attrs["bar_counts"] = {str(d): int(n) for d, n in counts.items()}
    res.attrs["gaps"] = gaps
    return res[STD_COLS]


def fetch_qmt_kline(code: str, begin: str, end: str) -> pd.DataFrame:
    """从 QMT 取 1m 并规范化（只读行情；不触 xttrader）。

    ⚠️ H49：miniQMT 启动后**不自动补当日** ⇒ 必须先 `download_history_data`，
    否则末根停在前一日而根数依然上万（假绿）。
    """
    from xtquant import xtdata  # 延迟 import

    sym = qmt_code(code)
    b, e = begin.replace("-", ""), end.replace("-", "")
    xtdata.download_history_data(sym, "1m", b, e)
    data = xtdata.get_market_data_ex(
        ["time", "open", "high", "low", "close", "volume", "amount"],
        [sym], period="1m", start_time=b, end_time=e)
    raw = data.get(sym) if isinstance(data, dict) else None
    if raw is None or len(raw) == 0:
        raise ValueError(f"QMT 未返回 {sym} 的 1m 数据（{begin}~{end}）")
    return normalize_qmt_1m(raw.reset_index(drop=True))
