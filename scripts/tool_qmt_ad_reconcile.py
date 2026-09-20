#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QMT(xtdata) ↔ AmazingData(本地CSV) 一分钟线对账 · **只读**

目的
----
为「行情源切换（AmazingData → QMT 官方 xtdata）」提供**可复现的口径依据**。
回答三个问题：
  1. 两个源的价格是否同一套数据（逐根 O/H/L/C 一致率）
  2. 时间戳口径差多少（是否 bar 开始 vs bar 结束；是否还叠加时区差）
  3. 成交量单位是否一致（股 vs 手）

红线（本脚本刻意不逾越）
------------------------
- **只读行情**：只调 xtdata（`get_market_data_ex` / `download_history_data`），
  不 import `xttrader`、不触碰账户与下单接口。
- **不登录 AmazingData**：AD 一侧只读**本地已落盘的 CSV**，
  因此不会占用单点登录、不会与观察期采集冲突。
- 下载历史行情会写入 QMT 自己的 `userdata_mini/datadir`（券商客户端缓存），
  不写 quant_app 的 `data/`。

用法
----
  # 默认：300308，自动取「本地 CSV 与 QMT 都能覆盖」的最后一个交易日
  D:\\Anaconda3\\python.exe scripts/tool_qmt_ad_reconcile.py

  # 指定日期
  D:\\Anaconda3\\python.exe scripts/tool_qmt_ad_reconcile.py --date 2026-09-08
  D:\\Anaconda3\\python.exe scripts/tool_qmt_ad_reconcile.py --code 688256 --date 2026-09-18

产物
----
  test_outputs/qmt_ad_reconcile/reconcile_{code}_{date}.json   机读结论
  test_outputs/qmt_ad_reconcile/reconcile_{code}_{date}.csv    对齐后的逐根明细
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")

APP_DIR = Path(__file__).resolve().parents[1]
OUT_DIR = APP_DIR / "test_outputs" / "qmt_ad_reconcile"

BJ = timezone(timedelta(hours=8))     # 北京
LOCAL_SHIFT_HOURS = 3                 # 本机 GMT+3（坦桑尼亚）；用于解释索引标签

PRICE_FIELDS = ["open", "high", "low", "close"]
# 候选时间戳平移（分钟）：0 / ±1 分钟（bar开始vs结束）/ ±时区差
CANDIDATE_SHIFTS_MIN = [0, -1, 1, -5 * 60, 5 * 60, -4 * 60, -6 * 60, -LOCAL_SHIFT_HOURS * 60]


# ────────────────────────────── 工具 ──────────────────────────────
def to_qmt_code(code: str) -> str:
    """6 位代码 → QMT 代码（300/301→SZ，600/601/603/605/688→SH，8/4→BJ）。"""
    code = code.strip().upper()
    if "." in code:
        return code
    if code.startswith(("60", "68", "51", "58", "11")):
        return f"{code}.SH"
    if code.startswith(("83", "87", "43", "92")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def fmt_ts(ts) -> str:
    try:
        return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


# ────────────────────────────── 数据装载 ──────────────────────────────
def load_ad_side(code: str, local_csv: Path, date: str) -> pd.DataFrame:
    """读本地 AmazingData CSV（不联网、不登录），只取指定交易日。"""
    if not local_csv.exists():
        raise FileNotFoundError(f"本地 CSV 不存在：{local_csv}")
    df = pd.read_csv(local_csv)
    # 兼容 BOM 与大小写
    df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
    tcol = "time" if "time" in df.columns else df.columns[0]
    df[tcol] = pd.to_datetime(df[tcol], errors="coerce")
    df = df.dropna(subset=[tcol]).sort_values(tcol).reset_index(drop=True)
    day = df[df[tcol].dt.strftime("%Y-%m-%d") == date].reset_index(drop=True)
    if len(day) == 0:
        raise ValueError(
            f"本地 CSV 内没有 {date} 的数据（该文件末根 = {fmt_ts(df[tcol].iloc[-1])}）")
    day = day.rename(columns={tcol: "adm_time"})
    return day


def load_qmt_side(qmt_code: str, date: str) -> tuple[pd.DataFrame, dict]:
    """从 xtdata 取 1m（只读）。返回 (标准化后的 df, 原始口径诊断)。"""
    from xtquant import xtdata  # 延迟导入，便于无客户端时给出清楚报错

    try:
        xtdata.enable_hello = False
    except Exception:
        pass

    ymd = date.replace("-", "")
    try:
        xtdata.download_history_data(qmt_code, period="1m", start_time=ymd, end_time=ymd)
    except Exception as exc:  # 下载失败不致命：可能本地已有缓存
        print(f"  ⚠️ download_history_data 异常（继续尝试读缓存）：{type(exc).__name__}: {exc}")

    data = xtdata.get_market_data_ex(
        [], [qmt_code], period="1m", start_time=ymd, end_time=ymd)
    raw = data.get(qmt_code) if isinstance(data, dict) else None
    if raw is None or len(raw) == 0:
        raise RuntimeError(
            f"xtdata 未返回 {qmt_code} 在 {date} 的 1m 数据"
            "（客户端未登录 / 该日无行情 / 未下载成功）")

    df = raw.copy()

    # 原始索引标签（疑似本机时区渲染）与 epoch 字段（疑似北京时）都留档对比
    idx_raw = [str(i) for i in df.index[:3]]
    diag = {"index_raw_head": idx_raw, "index_raw_len": len(df)}

    # 主时间轴：优先用 time 字段（epoch ms），退回索引字符串
    if "time" in df.columns:
        df["qmt_ms"] = pd.to_numeric(df["time"], errors="coerce")
        # 自动判别该 epoch 是「秒」还是「毫秒」
        med = df["qmt_ms"].dropna()
        unit_ms = True
        if len(med) and med.median() < 1e11:
            unit_ms = False
        df["qmt_dt"] = pd.to_datetime(
            df["qmt_ms"], unit="ms" if unit_ms else "s", utc=True
        ).dt.tz_convert(BJ).dt.tz_localize(None)
        diag["time_unit"] = "ms" if unit_ms else "s"
        diag["epoch_head"] = [fmt_ts(v) for v in df["qmt_dt"].head(3)]
    else:
        df["qmt_time"] = pd.to_datetime(df.index, format="%Y%m%d%H%M%S", errors="coerce")
        if df["qmt_time"].isna().all():
            df["qmt_time"] = pd.to_datetime(df.index, errors="coerce")
        df["qmt_dt"] = df["qmt_time"]
        diag["time_unit"] = "(无 time 字段，用索引)"

    # 索引字符串（若可解析）也留档，用于验证“索引=本机时区”这一假设
    try:
        idx_dt = pd.to_datetime(pd.Series([str(i) for i in df.index]),
                                format="%Y%m%d%H%M%S", errors="coerce")
        if idx_dt.notna().any():
            diag["index_as_local_head"] = [
                fmt_ts(v) for v in idx_dt.head(3)]
            diag["index_as_beijing_head"] = [
                fmt_ts(v + pd.Timedelta(hours=5)) for v in idx_dt.head(3)]
    except Exception:
        pass

    df = df.dropna(subset=["qmt_dt"]).sort_values("qmt_dt").reset_index(drop=True)
    return df, diag


# ────────────────────────────── 对账 ──────────────────────────────
def score_shift(ad: pd.DataFrame, qmt: pd.DataFrame, shift_min: int) -> int:
    """按给定平移量对齐后，close 完全相同的根数。"""
    q = qmt.copy()
    q["k"] = q["qmt_dt"] + pd.Timedelta(minutes=shift_min)
    a = ad[["adm_time", "close"]].rename(columns={"adm_time": "k"})
    m = a.merge(q[["k", "close"]].rename(columns={"close": "q_close"}), on="k", how="inner")
    if len(m) == 0:
        return 0
    return int((m["close"].round(6) == m["q_close"].round(6)).sum())


def reconcile(ad: pd.DataFrame, qmt: pd.DataFrame) -> dict:
    scores = {s: score_shift(ad, qmt, s) for s in CANDIDATE_SHIFTS_MIN}
    best_shift = max(scores, key=lambda k: (scores[k], -abs(k)))
    n_int = 0
    for s in CANDIDATE_SHIFTS_MIN:
        q = qmt[["qmt_dt"]].copy()
        q["k"] = q["qmt_dt"] + pd.Timedelta(minutes=s)
        n_int = max(n_int, len(ad[["adm_time"]].rename(columns={"adm_time": "k"})
                               .merge(q, on="k", how="inner")))

    q = qmt.copy()
    q["k"] = q["qmt_dt"] + pd.Timedelta(minutes=best_shift)
    keep = [c for c in (PRICE_FIELDS + ["volume", "amount"]) if c in q.columns]
    qsub = (q[["k"] + keep]
            .rename(columns={c: f"qmt_{c}" for c in keep}))
    merged = ad.merge(qsub, left_on="adm_time", right_on="k", how="inner")

    price_stats = {}
    for c in PRICE_FIELDS:
        qc = f"qmt_{c}"
        if c in ad.columns and qc in merged.columns and len(merged):
            eq = (merged[c].round(6) == merged[qc].round(6))
            price_stats[c] = {
                "n": int(len(merged)),
                "exact": int(eq.sum()),
                "match_rate": round(float(eq.mean()), 6),
                "max_abs_diff": round(float((merged[c] - merged[qc]).abs().max()), 6),
            }

    vol_stats = {}
    if "qmt_volume" in merged.columns and len(merged):
        a_vol = pd.to_numeric(merged["volume"], errors="coerce")
        q_vol = pd.to_numeric(merged["qmt_volume"], errors="coerce")
        ratio = (q_vol / a_vol).replace([float("inf"), float("-inf")], pd.NA).dropna()
        vol_stats = {
            "n": int(len(ratio)),
            "ratio_median": round(float(ratio.median()), 6) if len(ratio) else None,
            "ratio_mean": round(float(ratio.mean()), 6) if len(ratio) else None,
            "exact_same": int((a_vol == q_vol).sum()),
            "nearest_int_div100": int((a_vol.round() == (q_vol * 100).round()).sum()),
        }

    close_note = None
    if "close" in merged.columns and "qmt_close" in merged.columns and len(merged):
        last_a = float(ad["close"].iloc[-1])
        last_q = float(qmt["close"].iloc[-1])
        close_note = {"ad_last_close": round(last_a, 4), "qmt_last_close": round(last_q, 4),
                      "equal": abs(last_a - last_q) < 1e-6}

    return {
        "shift_scores": {str(k): v for k, v in scores.items()},
        "best_shift_min": best_shift,
        "best_shift_close_match": scores[best_shift],
        "aligned_rows": int(len(merged)),
        "intersection_max": int(n_int),
        "price_stats": price_stats,
        "volume_stats": vol_stats,
        "last_close": close_note,
        "merged": merged,
    }


# ────────────────────────────── main ──────────────────────────────
def _run_merge_check(code: str, qmt_code: str, local_csv: Path, days: int,
                     since: str | None) -> int:
    """检验「开盘集合竞价合并规则」—— 凭证 = AD 09:30 ≟ merge(QMT 09:30, QMT 09:31)。

    规则（待多日复验）：
        open   = QMT_09:30.open      （竞价成交价）
        close  = QMT_09:31.close     （首根连续竞价收盘）
        high   = max(QMT_0930.high, QMT_0931.high)
        low    = min(QMT_0930.low,  QMT_0931.low)
        volume = (QMT_0930.volume + QMT_0931.volume) * 100   （手→股；允许 1 手取整误差）
    """
    probe = pd.read_csv(local_csv)
    probe.columns = [c.strip().lstrip("\ufeff") for c in probe.columns]
    ts = pd.to_datetime(probe[probe.columns[0]], errors="coerce").dropna()
    all_dates = sorted(set(ts.dt.strftime("%Y-%m-%d")))
    if since:
        all_dates = [d for d in all_dates if d >= since]
    dates = all_dates[-days:]

    print(f"本地 CSV : {local_csv}")
    print(f"检验天数 : {len(dates)}   {dates[0] if dates else '-'} ~ {dates[-1] if dates else '-'}")
    print()
    hdr = (f"{'日期':<12}{'AD开':>9}{'规则开':>9}{'AD收':>9}{'规则收':>9}"
           f"{'AD高':>9}{'规则高':>9}{'AD低':>9}{'规则低':>9}{'AD量(股)':>12}{'规则量':>12}")
    print(hdr)
    print("-" * 104)

    stat = {"open": 0, "close": 0, "high": 0, "low": 0, "vol": 0}
    n = 0
    for d in dates:
        try:
            ad = load_ad_side(code, local_csv, d)
            qmt, _ = load_qmt_side(qmt_code, d)
        except Exception as exc:
            print(f"{d:<12} ★失败 {type(exc).__name__}: {exc}")
            continue
        def pick(df, tcol, hhmm):
            m = df[df[tcol].dt.strftime("%H:%M") == hhmm]
            return m.iloc[0] if len(m) else None
        a0 = pick(ad, "adm_time", "09:30")
        q30 = pick(qmt, "qmt_dt", "09:30")
        q31 = pick(qmt, "qmt_dt", "09:31")
        if a0 is None or q30 is None or q31 is None:
            print(f"{d:<12} ★缺根（AD09:30/QMT09:30/QMT09:31 有缺）")
            continue
        r_open = float(q30["open"])
        r_close = float(q31["close"])
        r_high = max(float(q30["high"]), float(q31["high"]))
        r_low = min(float(q30["low"]), float(q31["low"]))
        r_vol = (float(q30["volume"]) + float(q31["volume"])) * 100
        stat["open"] += abs(float(a0["open"]) - r_open) < 1e-6
        stat["close"] += abs(float(a0["close"]) - r_close) < 1e-6
        stat["high"] += abs(float(a0["high"]) - r_high) < 1e-6
        stat["low"] += abs(float(a0["low"]) - r_low) < 1e-6
        stat["vol"] += abs(float(a0["volume"]) - r_vol) <= 100
        n += 1
        def g(v):   # 统一按 4 位小数显示：避免 float repr（900.3000000000001）把相邻列拼成一串引发误读
            return f"{float(v):.4f}"
        print(f"{d:<12}{g(a0['open']):>10}{g(r_open):>10}{g(a0['close']):>10}{g(r_close):>10}"
              f"{g(a0['high']):>10}{g(r_high):>10}{g(a0['low']):>10}{g(r_low):>10}"
              f"{float(a0['volume']):>13,.0f}{r_vol:>13,.0f}")

    print()
    if not n:
        print("★ 无有效样本（客户端未开？）")
        return 1
    print("规则命中率：")
    for k, label in [("open", "open = QMT09:30.open"), ("close", "close = QMT09:31.close"),
                     ("high", "high = max(两高)"), ("low", "low = min(两低)"),
                     ("vol", "volume = (两量和)×100")]:
        print(f"   {label:28}: {stat[k]}/{n}")
    allok = all(stat[k] == n for k in stat)
    print()
    print("✅ 规则 100% 成立 —— 可写进对照表作为实测依据" if allok
          else "⚠️ 规则未全部命中 —— 需逐项排查（不要拍脑袋定）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"{code}_{dates[0]}_{dates[-1]}_mergecheck"
    (OUT_DIR / f"auction_merge_{tag}.json").write_text(
        json.dumps({"code": code, "qmt_code": qmt_code, "local_csv": str(local_csv),
                    "days": n, "hits": stat,
                    "generated_at": datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S +08:00")},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"产物：{OUT_DIR / f'auction_merge_{tag}.json'}")
    return 0


def _run_multi(code: str, qmt_code: str, local_csv: Path, days: int,
               since: str | None) -> int:
    """逐日跑对账，检验口径结论在**所有可用交易日**上是否稳定。

    单日样本只能说明"存在这个口径差"，多日样本才能说明"它是恒定规律还是偶然"。
    """
    probe = pd.read_csv(local_csv)
    probe.columns = [c.strip().lstrip("\ufeff") for c in probe.columns]
    ts = pd.to_datetime(probe[probe.columns[0]], errors="coerce").dropna()
    all_dates = sorted(set(ts.dt.strftime("%Y-%m-%d")))
    if since:
        all_dates = [d for d in all_dates if d >= since]
    dates = all_dates[-days:]

    print(f"本地 CSV : {local_csv}")
    print(f"可对账日 : {len(dates)} 天   {dates[0] if dates else '-'} ~ {dates[-1] if dates else '-'}")
    print()
    hdr = (f"{'日期':<12}{'AD':>5}{'QMT':>5}{'平移':>7}{'close':>10}"
           f"{'open':>10}{'末根收盘等':>12}{'量比中位':>11}")
    print(hdr)
    print("-" * 78)

    rows = []
    for d in dates:
        try:
            ad = load_ad_side(code, local_csv, d)
            qmt, _ = load_qmt_side(qmt_code, d)
            res = reconcile(ad, qmt)
            ps = res["price_stats"]
            vs = res.get("volume_stats") or {}
            lc = res.get("last_close") or {}
            row = {
                "date": d, "ad_rows": len(ad), "qmt_rows": len(qmt),
                "best_shift_min": res["best_shift_min"],
                "close_match": (f"{ps['close']['exact']}/{ps['close']['n']}" if "close" in ps else "-"),
                "open_match": (f"{ps['open']['exact']}/{ps['open']['n']}" if "open" in ps else "-"),
                "last_close_equal": lc.get("equal"),
                "last_close": (lc.get("ad_last_close"), lc.get("qmt_last_close")),
                "vol_ratio_median": vs.get("ratio_median"),
                "high_match": (f"{ps['high']['exact']}/{ps['high']['n']}" if "high" in ps else "-"),
                "low_match": (f"{ps['low']['exact']}/{ps['low']['n']}" if "low" in ps else "-"),
            }
            rows.append(row)
            print(f"{d:<12}{row['ad_rows']:>5}{row['qmt_rows']:>5}{row['best_shift_min']:>7}"
                  f"{row['close_match']:>10}{row['open_match']:>10}"
                  f"{str(row['last_close_equal']):>12}{str(row['vol_ratio_median']):>11}")
        except Exception as exc:
            print(f"{d:<12} ★失败 {type(exc).__name__}: {exc}")
            rows.append({"date": d, "error": f"{type(exc).__name__}: {exc}"})

    ok = [r for r in rows if "error" not in r]
    print()
    print(f"成功 {len(ok)}/{len(rows)} 天")
    if ok:
        from collections import Counter
        print(f"best_shift 分布      : {dict(Counter(r['best_shift_min'] for r in ok))}")
        print(f"末根收盘全等         : {sum(1 for r in ok if r['last_close_equal'])}/{len(ok)}")
        vr = [r["vol_ratio_median"] for r in ok if r["vol_ratio_median"] is not None]
        if vr:
            print(f"量比中位数范围       : {min(vr)} ~ {max(vr)}")
        for f in ("close_match", "open_match", "high_match", "low_match"):
            vals = []
            for r in ok:
                a, b = r[f].split("/")
                vals.append(int(a) / int(b) if int(b) else 0)
            if vals:
                print(f"{f:<20}: 均值 {sum(vals)/len(vals):.4f}  最低 {min(vals):.4f}")
        excl = [r["date"] for r in ok if r["best_shift_min"] != ok[0]["best_shift_min"]]
        print(f"与首日平移不一致的日期: {excl or '无（口径恒定）'}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"{code}_{dates[0]}_{dates[-1]}_multi" if dates else f"{code}_multi"
    (OUT_DIR / f"reconcile_{tag}.json").write_text(
        json.dumps({"code": code, "qmt_code": qmt_code, "local_csv": str(local_csv),
                    "days": len(dates), "rows": rows,
                    "generated_at": datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S +08:00")},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"产物：{OUT_DIR / f'reconcile_{tag}.json'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="QMT(xtdata) ↔ AmazingData(本地CSV) 1m 对账（只读）")
    ap.add_argument("--code", default="300308", help="6 位代码，默认 300308")
    ap.add_argument("--date", default=None, help="交易日 YYYY-MM-DD；默认取本地 CSV 的末根日")
    ap.add_argument("--local-csv", default=None, help="本地 AD CSV 路径；默认 data/stock_{code}_1m.csv")
    ap.add_argument("--multi", action="store_true",
                    help="多日模式：对本地 CSV 里最近 N 个交易日逐日对账（检验口径是否恒定）")
    ap.add_argument("--days", type=int, default=12, help="配合 --multi/--check-merge：取最近多少天，默认 12")
    ap.add_argument("--check-merge", action="store_true",
                    help="检验「开盘集合竞价合并规则」：凭证 = AD 09:30 ≟ merge(QMT 09:30, QMT 09:31)")
    args = ap.parse_args(argv)

    code = args.code.strip()
    local_csv = Path(args.local_csv) if args.local_csv else (APP_DIR / "data" / f"stock_{code}_1m.csv")
    qmt_code = to_qmt_code(code)

    if args.check_merge:
        print("=" * 74)
        print(f"开盘集合竞价合并规则检验   标的={code} ({qmt_code})")
        print("=" * 74)
        return _run_merge_check(code, qmt_code, local_csv, args.days, args.date)

    if args.multi:
        print("=" * 74)
        print(f"QMT(xtdata) ↔ AmazingData 本地CSV 一分钟线对账 · 多日模式   标的={code} ({qmt_code})")
        print("=" * 74)
        return _run_multi(code, qmt_code, local_csv, args.days, args.date)

    print("=" * 74)
    print(f"QMT(xtdata) ↔ AmazingData 本地CSV 一分钟线对账   标的={code} ({qmt_code})")
    print("=" * 74)

    # 日期：默认取本地 CSV 末根日（保证 AD 侧一定有数据）
    probe = pd.read_csv(local_csv)
    probe.columns = [c.strip().lstrip("\ufeff") for c in probe.columns]
    last_day = pd.to_datetime(probe[probe.columns[0]], errors="coerce").dropna().max()
    date = args.date or last_day.strftime("%Y-%m-%d")
    print(f"本地 CSV      : {local_csv}")
    print(f"  末根        : {fmt_ts(last_day)}")
    print(f"对账日期      : {date}")
    print()

    print("── AD 侧（本地 CSV，不联网）──")
    ad = load_ad_side(code, local_csv, date)
    print(f"  根数 = {len(ad)} | 首根 = {fmt_ts(ad['adm_time'].iloc[0])} | 末根 = {fmt_ts(ad['adm_time'].iloc[-1])}")
    print()

    print("── QMT 侧（xtdata 只读）──")
    qmt, diag = load_qmt_side(qmt_code, date)
    print(f"  根数 = {len(qmt)} | 首根 = {fmt_ts(qmt['qmt_dt'].iloc[0])} | 末根 = {fmt_ts(qmt['qmt_dt'].iloc[-1])}")
    print(f"  索引原样(前3)   : {diag.get('index_raw_head')}")
    print(f"  epoch 字段(前3) : {diag.get('epoch_head')}  [单位={diag.get('time_unit')}]")
    if "index_as_beijing_head" in diag:
        print(f"  索引按本机GMT+3 : {diag.get('index_as_local_head')}")
        print(f"  索引+5h→北京时  : {diag.get('index_as_beijing_head')}")
    print()

    print("── 逐根对账 ──")
    res = reconcile(ad, qmt)
    print("  时间戳平移候选得分（close 完全相同的根数）：")
    for s, v in sorted(res["shift_scores"].items(), key=lambda kv: -kv[1]):
        tip = ""
        if int(s) == 0:
            tip = "（同标签）"
        elif abs(int(s)) == 1:
            tip = "（bar开始 vs bar结束）"
        elif abs(int(s)) == 300:
            tip = "（±5h＝时区差）"
        print(f"    平移 {int(s):>5d} 分钟 → {v:>4d} 根 {tip}")
    print(f"  ★ 最佳平移 = {res['best_shift_min']} 分钟（{res['best_shift_close_match']} 根 close 相同）")
    print(f"  对齐后交集 = {res['aligned_rows']} 根（候选平移中的最大交集 = {res['intersection_max']}）")
    print()
    print("  价格字段一致率（对齐后）：")
    for c, st in res["price_stats"].items():
        print(f"    {c:6} {st['exact']:>4d}/{st['n']:<4d}  一致率={st['match_rate']:.6f}  最大差={st['max_abs_diff']}")
    if res["volume_stats"]:
        v = res["volume_stats"]
        print(f"  成交量：完全相等 {v['exact_same']}/{v['n']}"
              f" | QMT/AD 比值中位数={v['ratio_median']} 均值={v['ratio_mean']}"
              f" | AD取整==QMT×100 的根数={v['nearest_int_div100']}")
    if res["last_close"]:
        lc = res["last_close"]
        print(f"  末根收盘：AD={lc['ad_last_close']}  QMT={lc['qmt_last_close']}  相等={lc['equal']}")
    print()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"{code}_{date}"
    merged = res.pop("merged")
    merged.to_csv(OUT_DIR / f"reconcile_{tag}.csv", index=False, encoding="utf-8-sig")
    payload = {
        "code": code, "qmt_code": qmt_code, "date": date,
        "local_csv": str(local_csv),
        "ad_rows": int(len(ad)), "qmt_rows": int(len(qmt)),
        "qmt_diag": diag,
        "generated_at": datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S +08:00"),
        **res,
    }
    (OUT_DIR / f"reconcile_{tag}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"产物：{OUT_DIR / f'reconcile_{tag}.json'}")
    print(f"      {OUT_DIR / f'reconcile_{tag}.csv'}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"\n★ 失败：{type(exc).__name__}: {exc}")
        sys.exit(1)
