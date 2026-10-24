# -*- coding: utf-8 -*-
"""全市场「网格 / 日内做T」适配性筛选（L1 粗筛，日线口径）。

⚠️ 与 H4 原文口径的**关键区别（必须先读）
------------------------------------------------
H4（2026-09-14 登记）的口径是「**相对持股超额 + 统一窗口长度**」——那是**收益口径**，
用来挑「能跑赢基准」的标的，适用于**趋势/多头**策略。

但网格与日内做T**不靠方向赚钱，靠波动往返**：
- 标的上涨不必然赚钱（网格上沿卖完就空等）；
- 标的下跌必然被套（下沿买满即锁死）；
- 真正决定盈亏的是「**日内来回多少次**」。

⇒ 本工具用「**往返效率 RE**」作为核心指标：

    RE = 日均振幅 / 日均|净涨跌|

- 分子（振幅）＝ 当天真正可吃的波动幅度；
- 分母（|净涨跌|）＝ 当天必须承担的方向位移；
- **RE 高 ⇒ 日内来回多、方向净移动小 ⇒ 震荡 ⇒ 网格友好**；
- **RE → 1 ⇒ 振幅几乎全被单边吃掉 ⇒ 网格会被单边拖死**。

这只工具只做**粗筛（日线）**；最终必须用分钟数据跑真实网格回测（L2）确认。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

OUT_DIR = APP / "test_outputs" / "market_screen"

# 权益基准（实盘 QMT 快照 2026-10-24）
EQUITY = 509_716.53
RISK_PCT = 0.10
GRID_PCT = 2.0          # strategies/grid_trade.py default_params()

# 排除项（不参与筛选）
EXCLUDE_NAME_KW = ("ST", "退", "PT")


def _to_index_dates(df: pd.DataFrame) -> pd.DataFrame:
    """QMT 日线索引可能是 epoch(ms) 或 'YYYYMMDD' 字符串，统一成 datetime。"""
    out = df.copy()
    idx = out.index
    if np.issubdtype(np.asarray(idx).dtype, np.number):
        v = np.asarray(idx, dtype="int64")
        unit = "ms" if v.max() > 1e11 else "s"
        out.index = pd.to_datetime(v, unit=unit)
    else:
        s = pd.Series(idx).astype(str)
        out.index = pd.to_datetime(s.str.replace("-", ""), format="%Y%m%d", errors="coerce")
    return out[out.index.notna()].sort_index()


def build_universe(start: str = "20230101", end: str = "20261023",
                   min_days: int = 240, window: int = 240) -> pd.DataFrame:
    """⚠️ 本地日线覆盖率实测：5225 只都有 ≥200 根，但只有 82 只有 480 根以上
    （QMT 本地日线多数只到 2025 年初）⇒ 若按 480 天筛，会把全市场筛成 82 只。

    ⇒ 改为**统一取最近 `window=240` 个交易日**：样本期一致、全市场可比，
    也符合 H4「统一窗口长度」的要求。不足 240 根的标的排除（多为次新股）。
    """
    from xtquant import xtdata

    codes = xtdata.get_stock_list_in_sector("沪深A股")
    raw = xtdata.get_market_data_ex(
        ["open", "high", "low", "close", "amount", "volume"], codes,
        period="1d", start_time=start, end_time=end)

    rows = []
    for code, df in raw.items():
        if df is None or len(df) == 0:
            continue
        df = _to_index_dates(df).dropna(subset=["close", "high", "low"])
        if len(df) < min_days:
            continue
        df = df.tail(window)          # 统一窗口，保证可比
        close = df["close"].astype(float)
        px = float(close.iloc[-1])
        if px <= 0:
            continue
        rng = (df["high"] - df["low"]).replace(0, np.nan)
        amp = float((rng / close).mean())
        absret = float(close.pct_change().abs().mean())
        # 日内「方向净位移」占振幅的比例：实体越大 ⇒ 单边越明确 ⇒ 做T（信号方向）友好
        body = float(((close - df["open"]).abs() / rng).mean())
        rows.append({
            "code": code,
            "days": len(df),
            "last_px": px,
            "lot_value": px * 100,
            "lot_pct": px * 100 / EQUITY,
            "amp": amp,
            "absret": absret,
            "re": amp / absret if absret > 0 else np.nan,
            "grid_cover": amp / (GRID_PCT / 100.0),
            # grid_trade：吃往返 ⇒ wick_ratio 高（影线多、不留方向）友好
            "body_ratio": body,
            "wick_ratio": 1.0 - body,
            "amount_wan": float(df["amount"].mean()) / 1e4,
            "amp_60": float(((df["high"] - df["low"]) / close).tail(60).mean()),
        })

    uni = pd.DataFrame(rows)
    if uni.empty:
        return uni

    # 名称（用于排除 ST / 退市）；detail 为本地查询，失败则留空
    names = {}
    for code in uni["code"]:
        try:
            d = xtdata.get_instrument_detail(code) or {}
            names[code] = (d.get("InstrumentName") or "").strip()
        except Exception:
            names[code] = ""
    uni["name"] = uni["code"].map(names)
    return uni


def apply_gates(uni: pd.DataFrame) -> pd.DataFrame:
    """硬门槛。**阈值全部来自实测分布**（见 print_distribution），不凭直觉拍。"""
    u = uni.copy()
    u["excl_st"] = u["name"].apply(lambda s: any(k in s for k in EXCLUDE_NAME_KW))
    gates = {
        "非ST": ~u["excl_st"],
        "价格窗口": (u["last_px"] >= 5) & (u["last_px"] <= EQUITY * RISK_PCT / 100),
        "档位覆盖≥1.5": u["grid_cover"] >= 1.5,
        "流动性≥3亿": u["amount_wan"] >= 30000,
        "近60日仍活跃": u["amp_60"] >= 0.02,
    }
    for k, v in gates.items():
        u[f"ok_{k}"] = v
    u["all_gates"] = np.logical_and.reduce([u[f"ok_{k}"].to_numpy() for k in gates])
    return u


def print_distribution(uni: pd.DataFrame) -> None:
    print("=" * 78)
    print("全市场分布（用于定阈值，不拍脑袋）")
    print("=" * 78)
    for col, label in (("re", "往返效率 RE = 振幅/|净涨跌|"),
                       ("amp", "日均振幅"),
                       ("grid_cover", "档位覆盖 = 振幅/2%"),
                       ("body_ratio", "日内实体占比(|C-O|/振幅)"),
                       ("wick_ratio", "日内影线占比=1-实体"),
                       ("amount_wan", "日均成交额(万)"),
                       ("last_px", "末收盘价")):
        s = uni[col].dropna()
        q = s.quantile([0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99])
        print(f"{label:26s} n={len(s):4d}  "
              f"p10={q.iloc[0]:>10.2f} p25={q.iloc[1]:>10.2f} p50={q.iloc[2]:>10.2f} "
              f"p75={q.iloc[3]:>10.2f} p90={q.iloc[4]:>10.2f} p95={q.iloc[5]:>10.2f} p99={q.iloc[6]:>11.2f}")
    print()


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("拉取全市场日线…")
    uni = build_universe()
    print(f"有效标的（≥480 交易日）: {len(uni)} 只\n")

    print_distribution(uni)

    u = apply_gates(uni)
    gate_cols = [c for c in u.columns if c.startswith("ok_")]
    print("各门槛单独通过数：")
    for c in gate_cols:
        print(f"  {c[3:]:14s} {int(u[c].sum()):5d} 只  ({u[c].mean():.1%})")
    print(f"  {'全部通过':14s} {int(u['all_gates'].sum()):5d} 只  ({u['all_gates'].mean():.2%})\n")

    passed = (u[u["all_gates"]]
              .sort_values("re", ascending=False))
    cols = ["code", "name", "last_px", "lot_pct", "amp", "absret", "re",
            "grid_cover", "body_ratio", "wick_ratio", "amp_60", "amount_wan", "days"]
    print("通过全部门槛: %d 只，按 RE 降序前 30"
          "（body_ratio 高=单边明确→intraday_t 友好；wick_ratio 高=往返多→grid 友好）" % len(passed))
    show = passed[cols].head(30).copy()
    for c, d in (("last_px", 2), ("lot_pct", 4), ("amp", 4), ("absret", 4),
                 ("re", 2), ("grid_cover", 2), ("body_ratio", 3), ("wick_ratio", 3),
                 ("amp_60", 4), ("amount_wan", 0)):
        show[c] = show[c].round(d)
    print(show.to_string(index=False))

    u.to_csv(OUT_DIR / "universe_all.csv", index=False, encoding="utf-8-sig")
    passed.to_csv(OUT_DIR / "universe_passed.csv", index=False, encoding="utf-8-sig")
    print(f"\n产物: {OUT_DIR/'universe_all.csv'}")
    print(f"      {OUT_DIR/'universe_passed.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
