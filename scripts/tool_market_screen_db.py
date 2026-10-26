# -*- coding: utf-8 -*-
"""全市场「网格 / 日内做T」适配性筛选 —— stockdb 数据源版（L1 粗筛，日线口径）。

为什么单独一个工具
------------------
`scripts/tool_market_screen.py` 走 QMT（`xtdata.get_market_data_ex`），每只票一次远程调用，
全市场要几十分钟且受 QMT 会话限制。
stockdb 是**本地库**，同样一次调用但走本机 LevelDB ⇒ 全市场几十秒级，
适合"先粗筛出候选池，再对候选跑 L2 分钟级回测"。

核心指标与 tool_market_screen.py **同口径**，不改定义：
    RE = 日均振幅 / 日均|净涨跌|
- RE 高 ⇒ 日内来回多、方向净移动小 ⇒ 网格友好
- RE → 1 ⇒ 振幅几乎全被单边吃掉 ⇒ 会被单边拖死

⚠️ RE 只衡量「往返效率」，**不衡量赚钱**。
   300502 的 RE 很高但一路阴跌 −42%，实测 A/B 回测仍亏 −28.33%（2026）。
   ⇒ 本工具的输出是"候选池"，**不是"买入清单"**，最终必须过 L2 分钟回测 + 基本面/流动性门槛。

用法
----
    python scripts/tool_market_screen_db.py --start 20250101 --end 20261023 \
        --out D:/Codex输出/screen_20261026.csv

    小样本试跑（先量吞吐，别一上来跑全市场）：
    python scripts/tool_market_screen_db.py --limit 50
"""
from __future__ import annotations

import argparse
import ast
import csv
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

PYBAO_ENV = "STOCKDB_PYBAO"
DEFAULT_PYBAO = Path(r"D:\stockdb\stockdb\pybao")
HOST_ENV = "STOCKDB_HOST"
PORT_ENV = "STOCKDB_PORT"

EXCLUDE_NAME_KW = ("ST", "退", "PT")

# 粗筛门槛（先给保守默认，跑完看分布再定，别拍脑袋收紧）
MIN_DAYS = 120          # 少于 120 个交易日的数据不足以谈"日均"
MIN_AMOUNT = 5e7        # 日均成交额 ≥ 5000 万（网格要能进出）
MIN_PRICE, MAX_PRICE = 3.0, 500.0


def _rd():
    """取 stockdb 查询对象，并按需切换端口（允许同时存在多个实例）。"""
    root = os.environ.get(PYBAO_ENV) or str(DEFAULT_PYBAO)
    if root not in sys.path:
        sys.path.insert(0, root)
    import stock_sdk  # noqa: PLC0415

    host = os.environ.get(HOST_ENV, "127.0.0.1")
    port = int(os.environ.get(PORT_ENV, "7899"))
    stock_sdk.init(host=host, port=port)
    rd = getattr(stock_sdk, "rd", None)
    if rd is None or not hasattr(rd, "get_data"):
        raise SystemExit("未找到可用的 stockdb 查询对象（需要 stock_sdk.rd.get_data）")
    return rd


def normalize_daily(raw):
    """stockdb 日线 → 统一列名。date 可能是整数 20261023，必须按位数解析。"""
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    df = raw.copy()
    cols = {c.lower(): c for c in df.columns}
    dc = next((cols[k] for k in ("date", "time", "datetime", "day") if k in cols), None)
    if dc is None:
        raise ValueError(f"无日期列，实际列={list(df.columns)[:10]}")
    s = df[dc]
    if pd.api.types.is_numeric_dtype(s):
        v = s.astype("int64").astype(str)
        fmt = "%Y%m%d" if v.str.len().max() == 8 else "%Y%m%d%H%M%S"
        df["date"] = pd.to_datetime(v, format=fmt, errors="coerce")
    else:
        df["date"] = pd.to_datetime(s.astype(str).str.replace("-", ""),
                                    format="%Y%m%d", errors="coerce")
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return df


def compute_metrics(df, code="", name="", min_days=None):
    """纯函数：一只票的往返效率与流动性指标。数据不足返回 None。"""
    need = MIN_DAYS if min_days is None else min_days
    if df is None or len(df) < need:
        return None
    close = df["close"].astype(float)
    high = df["high"].astype(float) if "high" in df else close
    low = df["low"].astype(float) if "low" in df else close
    prev = close.shift(1)
    rng = (high - low) / prev
    move = (close - prev).abs() / prev
    rng, move = rng.iloc[1:], move.iloc[1:]
    ok = rng.notna() & move.notna()
    if ok.sum() < need - 1:
        return None
    amp = float(rng[ok].mean()) * 100
    net = float(move[ok].mean()) * 100
    re_ratio = amp / net if net > 1e-9 else float("inf")
    amount = float(df["amount"].mean()) if "amount" in df else float("nan")
    name = name or (str(df["name"].iloc[-1]) if "name" in df else "")
    is_st = bool(df["is_st"].iloc[-1]) if "is_st" in df else False
    fmv = float(df["float_mv"].iloc[-1]) / 1e8 if "float_mv" in df else float("nan")
    turn = float(df["turnover"].mean()) if "turnover" in df else float("nan")
    return {
        "code": code,
        "name": name,
        "is_st": is_st,
        "days": int(len(df)),
        "first": df["date"].iloc[0].strftime("%Y-%m-%d"),
        "last": df["date"].iloc[-1].strftime("%Y-%m-%d"),
        "last_close": round(float(close.iloc[-1]), 3),
        "amp_pct": round(amp, 3),
        "net_pct": round(net, 3),
        "RE": round(re_ratio, 2),
        "avg_amount_yi": round(amount / 1e8, 2) if amount == amount else float("nan"),
        "float_mv_yi": round(fmv, 1) if fmv == fmv else float("nan"),
        "avg_turnover": round(turn, 2) if turn == turn else float("nan"),
        "total_ret_pct": round(float(close.iloc[-1] / close.iloc[0] - 1) * 100, 1),
    }


def _qr_as_dict(rd, table="股票代码"):
    """把 `rd.get(table)` 的 QueryResult 转成普通 dict。

    ⚠️ 两个实测坑（都不能靠"应该能取"来写）：
    ① 逐键下标不稳定：`qr['0']` 能拿到，紧接着 `qr['1']` 抛 TypeError；
    ② **一旦下标失败，该对象的 `str()` 就被污染成 "Missing required parameters"**，
       再解析就拿不到数据。
    ⇒ 正确顺序是「先 `str(qr)` 一次性解析，绝不下标」，下标只作最后兜底。
    """
    try:
        qr = rd.get(table)
    except Exception:
        return {}
    try:
        d = ast.literal_eval(str(qr))
        if isinstance(d, dict) and d:
            return {str(k): [str(x) for x in v] for k, v in d.items()}
    except Exception:
        pass
    out = {}
    try:
        for k in list(qr.keys()):
            out[str(k)] = [str(x) for x in qr[k]]
    except Exception:
        pass
    return out


def load_universe(rd=None, limit=None):
    """标的清单：优先 stockdb 自带的「股票代码」表（本地、零网络），QMT 仅作兜底。

    stockdb 返回 {'0': [...], '3': [...], '6': [...], '9': [...]} 分市场。
    只取 A 股市场：0=深主板/中小、3=创业板、6=沪主板、9=北交所；
    1 / 5 是基金与债券，网格策略不适用，排除。
    """
    codes = []
    if rd is not None:
        try:
            d = _qr_as_dict(rd)
            markets = [k for k in d if k in ("0", "3", "6", "9")]
            for mk in markets:
                codes.extend(d[mk])
            detail = " ".join(f"{k}:{len(d[k])}" for k in markets)
            print(f"[universe] stockdb A股 {len(codes)} 只  {detail}")
        except Exception as e:
            print(f"[universe] stockdb 枚举失败（{type(e).__name__}: {e}）")
    if not codes:
        try:
            from xtquant import xtdata  # noqa: PLC0415
            lst = list(xtdata.get_stock_list_in_sector("沪深A股") or [])
            codes = [c.split(".")[0] for c in lst if c.endswith((".SH", ".SZ", ".BJ"))]
            print(f"[universe] QMT A股 {len(codes)} 只")
        except Exception as e:
            print(f"[universe] QMT 也不可用（{type(e).__name__}）")
    if not codes:
        raise SystemExit("无法取得标的清单：stockdb 与 QMT 都不可用，可用 --codes 指定 csv")
    codes = sorted(set(codes))
    return codes[:limit] if limit else codes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20250101")
    ap.add_argument("--end", default="20261023")
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 只（量吞吐用）")
    ap.add_argument("--min-days", type=int, default=MIN_DAYS,
                    help="最少交易日数（数据不足的样本不进统计）")
    ap.add_argument("--codes", default="", help="自定义代码清单 csv（一列 code）")
    args = ap.parse_args()

    rd = _rd()
    if args.codes:
        pool = [str(r[0]).strip().zfill(6) for r in
                csv.reader(open(args.codes, encoding="utf-8"))
                if r and r[0].strip().isdigit()]
    else:
        pool = load_universe(rd=rd, limit=args.limit or None)

    out = Path(args.out) if args.out else (APP / "test_outputs" / f"screen_{args.start}_{args.end}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)

    rows, fails, t0 = [], 0, time.time()
    for i, code in enumerate(pool, 1):
        try:
            raw = rd.get_data(code=code, start=args.start, end=args.end,
                              frequency="1d", fq=None, as_df=True)
            m = compute_metrics(normalize_daily(raw), code, "", min_days=args.min_days)
            if m:
                rows.append(m)
        except Exception:
            fails += 1
        if i % 200 == 0:
            el = time.time() - t0
            print(f"  {i}/{len(pool)}  成功 {len(rows)} 失败 {fails}  "
                  f"{el:.0f}s  {i / el:.1f} 只/秒", flush=True)

    if not rows:
        print(f"没有任何标的通过数据完整性门槛（要求 ≥{args.min_days} 个交易日）"
              f" —— 先确认数据源覆盖区间")
        return 3

    df = pd.DataFrame(rows)
    df.to_csv(out, index=False, encoding="utf-8-sig")

    el = time.time() - t0
    print(f"\n完成 {len(df)} 只，失败 {fails}，用时 {el:.0f}s（{len(pool) / max(el, 1e-9):.1f} 只/秒）")
    print(f"输出 {out}")

    gate = df[(df.avg_amount_yi * 1e8 >= MIN_AMOUNT)
              & df.last_close.between(MIN_PRICE, MAX_PRICE)
              & (~df.is_st)
              & ~df.name.fillna("").str.contains("|".join(EXCLUDE_NAME_KW))]
    print(f"\n通过门槛（成交额≥{MIN_AMOUNT/1e8:.1f}亿、价格 {MIN_PRICE}~{MAX_PRICE}、非ST）：{len(gate)} 只")
    cols = ["code", "name", "last_close", "amp_pct", "net_pct", "RE",
            "avg_amount_yi", "float_mv_yi", "total_ret_pct"]
    print("\nRE 前 15:")
    print(gate.nlargest(15, "RE")[cols].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
