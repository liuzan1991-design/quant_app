# -*- coding: utf-8 -*-
"""单标的「网格 / 日内做T」适配性体检（L1 日线口径 + 可选 L2 分钟回测）。

## 为什么要有它

全市场批量下载在 QMT 上**不可靠**：实测第 1 批 250 只耗时 531s，
第 2 批**卡死 72 分钟无响应**（download_history_data2 挂起）⇒ 全市场扫描这条路走不通。

而用户的实际用法是「**自己随时挑一只**」⇒ 正确的形态是**按需体检单只**：
输入 6 位代码，拉这只（约 2 秒），出指标与结论。

## 指标（与 tool_market_screen 同一套，保证口径一致）

- `RE = 日均振幅 / 日均|净涨跌|` —— 往返效率，**网格友好度**（越高越震荡）
- `grid_cover = 日均振幅 / grid_pct` —— **档位覆盖**（≥2 表示一天能穿 2 档）
- `amount` —— 日均成交额（流动性）
- `lot_pct` —— 1 手占权益比（≤RISK_PCT 才买得起）
- `wick_ratio` —— 影线占比（往返留存，越高对网格越有利）
- 数据完整性 —— 交易日数 / 满 240 根占比（分钟口径，需本地 1m 数据）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

EQUITY = 509_716.53
RISK_PCT = 0.10
GRID_PCT = 2.0
BARS_PER_DAY = 240


def _to_dates(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    idx = out.index
    if np.issubdtype(np.asarray(idx).dtype, np.number):
        v = np.asarray(idx, dtype="int64")
        out.index = pd.to_datetime(v, unit="ms" if v.max() > 1e11 else "s")
    else:
        s = pd.Series(idx).astype(str).str.replace("-", "", regex=False)
        out.index = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
    return out[out.index.notna()].sort_index()


def fetch_daily(code: str, start: str = "20230101", end: str = "20261023",
                download: bool = True, window: int = 240) -> pd.DataFrame:
    from xtquant import xtdata
    # 后缀：0/3 开头=深市；4/8 开头=北交所；其余（6 等）=沪市
    if code[0] in "03":
        suf = "SZ"
    elif code[0] in "48":
        suf = "BJ"
    else:
        suf = "SH"
    qmt = f"{code}.{suf}"
    raw = xtdata.get_market_data_ex(
        ["open", "high", "low", "close", "amount"], [qmt],
        period="1d", start_time=start, end_time=end).get(qmt)
    have = 0 if raw is None else int(raw["close"].notna().sum())
    if have < window and download:
        print(f"本地仅 {have} 根，尝试下载 {qmt} …", flush=True)
        try:
            xtdata.download_history_data(qmt, period="1d",
                                         start_time=start, end_time=end)
            raw = xtdata.get_market_data_ex(
                ["open", "high", "low", "close", "amount"], [qmt],
                period="1d", start_time=start, end_time=end).get(qmt)
        except Exception as exc:  # noqa: BLE001
            print(f"下载失败：{type(exc).__name__}: {exc}")
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = _to_dates(raw).dropna(subset=["close", "high", "low"])
    return df.tail(window)


def daily_metrics(df: pd.DataFrame) -> dict:
    close = df["close"].astype(float)
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    px = float(close.iloc[-1])
    amp = float((rng / close).mean())
    absret = float(close.pct_change().abs().mean())
    body = float(((close - df["open"]).abs() / rng).mean())
    return {
        "days": len(df),
        "last_px": px,
        "lot_value": px * 100,
        "lot_pct": px * 100 / EQUITY,
        "amp": amp,
        "absret": absret,
        "re": amp / absret if absret > 0 else float("nan"),
        "grid_cover": amp / (GRID_PCT / 100.0),
        "body_ratio": body,
        "wick_ratio": 1.0 - body,
        "amount_wan": float(df["amount"].mean()) / 1e4,
        "amp_60": float((rng / close).tail(60).mean()),
    }


def minute_quality(code: str) -> dict | None:
    """本地 1m 数据质量（不足 240 根的日子占比）；无数据 ⇒ None。"""
    f = APP / "data" / "live" / f"stock_{code}_1m.csv"
    if not f.exists():
        f = APP / "data" / f"stock_{code}_1m.csv"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f, encoding="utf-8-sig", usecols=["time"])
        days = pd.to_datetime(df["time"]).dt.normalize()
        cnt = days.value_counts()
        return {"path": str(f.relative_to(APP)), "days": int(len(cnt)),
                "full_ratio": float((cnt == BARS_PER_DAY).mean()),
                "last": str(days.max().date())}
    except Exception:  # noqa: BLE001
        return None


def verdict(m: dict) -> list[tuple[str, bool, str]]:
    """逐条判定（不合成单一分数 —— 让每条理由都可核对）。"""
    return [
        ("价格窗口", m["lot_pct"] <= RISK_PCT,
         f"1手 {m['lot_value']:,.0f} = 权益 {m['lot_pct']:.2%}（上限 {RISK_PCT:.0%}）"),
        ("档位覆盖", m["grid_cover"] >= 2.0,
         f"{m['grid_cover']:.2f} 档/天（≥2 才有网格空间）"),
        ("往返效率", m["re"] >= 1.6,
         f"RE={m['re']:.2f}（越高越震荡；<1.5 偏单边，网格易被套）"),
        ("流动性", m["amount_wan"] >= 30000,
         f"{m['amount_wan']:,.0f} 万/日（≥3 亿滑点可控）"),
        ("近60日活跃", m["amp_60"] >= 0.02,
         f"{m['amp_60']:.2%}（≥2% 才有 T 空间）"),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="单标的网格/日内T 适配性体检")
    ap.add_argument("code", help="6 位代码，如 300502")
    ap.add_argument("--no-download", action="store_true", help="只用本地数据")
    args = ap.parse_args()

    code = args.code.strip()
    print(f"体检 {code} …")
    df = fetch_daily(code, download=not args.no_download)
    if df.empty:
        print("❌ 拿不到日线数据（本地无 + 下载失败）")
        return 2

    m = daily_metrics(df)
    print(f"\n区间：{df.index[0].date()} ~ {df.index[-1].date()}（{m['days']} 个交易日，末收盘 {m['last_px']:.2f}）")
    print(f"日均振幅 {m['amp']:.2%} | 日均净涨跌 {m['absret']:.2%} | RE {m['re']:.2f}")
    print(f"实体占比 {m['body_ratio']:.3f} / 影线占比 {m['wick_ratio']:.3f} | 日均成交额 {m['amount_wan']:,.0f} 万")

    q = minute_quality(code)
    if q:
        print(f"1m 数据：{q['path']} | {q['days']} 天 | 满 240 根 {q['full_ratio']:.1%} | 末 {q['last']}")
    else:
        print("1m 数据：本地无（无法做分钟级 L2 回测）")

    print("\n判定：")
    fails = 0
    for name, ok, detail in verdict(m):
        print(f"  {'✅' if ok else '❌'} {name:8s} {detail}")
        fails += 0 if ok else 1

    print()
    if fails == 0:
        print("==> L1 全过，可进入 L2（分钟级双策略回测）")
    elif fails == 1:
        print(f"==> L1 有 {fails} 项不达标，需人工判断该短板是否可接受")
    else:
        print(f"==> L1 有 {fails} 项不达标，**不建议**作为网格/日内T 标的")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
