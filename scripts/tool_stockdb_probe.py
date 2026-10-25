# -*- coding: utf-8 -*-
"""stockdb 口径探针（只读）—— 装完库之后跑这一个，把三条口径测出来。

为什么需要它
------------
`core/stockdb_kline.py` 故意默认"拒绝出数"（`STOCKDB_CALIBRATED != 1` 即抛错），
因为标签口径 / 成交量单位 / 复权基准这三件事**历史上都真出过事**（H53 / H54 / QMT 手→股）。
本探针负责把这三条**实测**出来，而不是靠文档猜。

它做什么
--------
以本地 AD 分钟线 `data/live/stock_<code>_1m.csv`（不复权实价，权威基准）为参照：
  ① 取两者重叠的最后 N 个交易日；
  ② 比对**时间标签网格** ⇒ 推出 `STOCKDB_LABEL_SHIFT`（分钟）；
  ③ 比对**成交量合计** ⇒ 推出 `STOCKDB_VOLUME_MULT`；
  ④ 比对**收盘价** ⇒ 判断 fq 是否等价于"不复权"；
  ⑤ 统计**每日根数** ⇒ 报缺口（AD 基准 = 240 根/日）。

它**不写任何文件**，只打印报告 + 可直接复制的环境变量。

用法
----
    D:/Anaconda3/python.exe scripts/tool_stockdb_probe.py
    D:/Anaconda3/python.exe scripts/tool_stockdb_probe.py --code 300502 --days 10
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

import pandas as pd

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

STD_COLS = ["time", "open", "high", "low", "close", "volume", "amount"]
BARS_PER_DAY = 240
HOST = ("127.0.0.1", 7899)


def _port_open() -> bool:
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect(HOST)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _load_ad(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["time"])
    for c in ("volume", "amount"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df.sort_values("time").reset_index(drop=True)


def _import_rd():
    """复用适配器里的 import 逻辑（含 pybao 路径自动加载），避免两套代码漂移。"""
    from core.stockdb_kline import _import_rd as _imp
    from core.stockdb_kline import pybao_dir

    try:
        rd = _imp()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ {exc}")
        print("  解决：把 stockdb 装到 D:\\stockdb\\stockdb，或设 STOCKDB_PYBAO "
              "指向 pybao 目录（**无需运行 pybao/安装.py**）。")
        return None, None
    d = pybao_dir()
    return rd, (str(d) if d else "?")


def _hm_set(df: pd.DataFrame) -> set[str]:
    return set(df["time"].dt.strftime("%H:%M"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="300308")
    ap.add_argument("--days", type=int, default=5, help="比对最近 N 个交易日（未给 --start/--end 时）")
    ap.add_argument("--start", default="", help="比对起始日 YYYY-MM-DD 或 YYYYMMDD")
    ap.add_argument("--end", default="", help="比对结束日 YYYY-MM-DD 或 YYYYMMDD")
    ap.add_argument("--ad-csv", default="")
    args = ap.parse_args()

    code = args.code.strip()
    ad_csv = Path(args.ad_csv) if args.ad_csv else APP / "data" / "live" / f"stock_{code}_1m.csv"

    print("=" * 72)
    print(f"stockdb 口径探针　code={code}　基准={ad_csv}")
    print("=" * 72)

    if not _port_open():
        print("✗ 127.0.0.1:7899 无监听 —— stockdb 没启动（双击 stockdb.exe 后再跑）")
        return 2
    print("✓ 127.0.0.1:7899 已在监听")

    rd, modname = _import_rd()
    if rd is None:
        return 2
    print(f"✓ SDK 可用（import {modname}）")

    if not ad_csv.exists():
        print(f"✗ 基准数据不存在：{ad_csv}")
        print("  没有 AD 基准就无法推口径（不能拿两套未知互相比）。换 --ad-csv 指定。")
        return 2
    ad = _load_ad(ad_csv)

    def _norm(d: str) -> str:
        d = d.strip().replace("-", "")
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    if args.start and args.end:
        s, e = _norm(args.start), _norm(args.end)
        ad_days = [d for d in sorted(ad["time"].dt.strftime("%Y-%m-%d").unique())
                   if s <= d <= e]
        if not ad_days:
            print(f"✗ AD 基准里没有 {s} ~ {e} 的数据")
            return 2
    else:
        ad_days = sorted(ad["time"].dt.strftime("%Y-%m-%d").unique())[-args.days:]
    print(f"✓ AD 基准：{len(ad)} 行，比对日 {ad_days[0]} ~ {ad_days[-1]}（{len(ad_days)} 日）")

    start, stop = ad_days[0].replace("-", ""), ad_days[-1].replace("-", "")
    try:
        raw = rd.get_data(code=code, start=start, end=stop,
                          frequency="1m", fq=None, as_df=True)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ rd.get_data 失败：{type(exc).__name__}: {exc}")
        return 2

    sdb = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
    if sdb.empty:
        print("✗ stockdb 返回空表（该标的本地无数据？先跑一次同步）")
        return 2
    print(f"✓ stockdb 返回 {len(sdb)} 行，列={list(sdb.columns)}")

    # 时间列归一（字段名未知，按候选挑）
    tc = next((c for c in ("date", "time", "datetime", "kline_time", "dt") if c in sdb.columns), None)
    if tc is None:
        print(f"✗ 找不到时间列，实际列={list(sdb.columns)}")
        return 2
    sdb = sdb.copy()
    sdb["time"] = pd.to_datetime(sdb[tc].astype(str), errors="coerce") \
        if not pd.api.types.is_datetime64_any_dtype(sdb[tc]) else pd.to_datetime(sdb[tc])
    sdb = sdb.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)

    # 先按已知规则把尾盘 241 根对齐成 AD 的 240 根，否则尾盘那根会被误判成"口径不一致"
    from core.stockdb_kline import realign_tail_to_ad
    before = len(sdb)
    sdb = realign_tail_to_ad(sdb)
    if len(sdb) != before:
        print(f"  （已做尾盘对齐：{before} 根 → {len(sdb)} 根）")

    # ① 标签口径
    ad_hms = _hm_set(ad[ad["time"].dt.strftime("%Y-%m-%d").isin(ad_days)])
    sdb_hms = _hm_set(sdb)
    ad_last, sdb_last = (max(ad_hms) if ad_hms else "—"), (max(sdb_hms) if sdb_hms else "—")
    ad_first, sdb_first = (min(ad_hms) if ad_hms else "—"), (min(sdb_hms) if sdb_hms else "—")

    def _m(h: str) -> int:
        hh, mm = h.split(":")
        return int(hh) * 60 + int(mm)

    shift_guess = None
    if ad_first != "—" and sdb_first != "—":
        # 用**首根**判标签口径：末根受收盘竞价归属影响（stockdb 曾放在 15:00），会误判
        shift_guess = _m(sdb_first) - _m(ad_first)

    print("\n── ① 时间标签 ──")
    print(f"  AD      首/末 = {ad_first} / {ad_last}")
    print(f"  stockdb 首/末 = {sdb_first} / {sdb_last}")
    print(f"  交集 {len(ad_hms & sdb_hms)} 个时点；AD {len(ad_hms)} / sdb {len(sdb_hms)}")
    if shift_guess is None:
        print("  ⇒ 无法判定（缺端点）")
    elif shift_guess == 0:
        print("  ⇒ 标签口径与 AD 一致（bar 起点）⇒ STOCKDB_LABEL_SHIFT=0")
    else:
        print(f"  ⇒ stockdb 末根比 AD 晚 {shift_guess} 分钟 ⇒ STOCKDB_LABEL_SHIFT={shift_guess}")

    # ② 成交量单位
    vol_mult = None
    print("\n── ② 成交量单位 ──")
    if "volume" in sdb.columns:
        ad_v = float(ad[ad["time"].dt.strftime("%Y-%m-%d").isin(ad_days)]["volume"].sum())
        sdb_v = float(pd.to_numeric(sdb["volume"], errors="coerce").fillna(0).sum())
        print(f"  AD 合计 = {ad_v:,.0f}　stockdb 合计 = {sdb_v:,.0f}")
        if sdb_v > 0:
            vol_mult = ad_v / sdb_v
            print(f"  比值 AD/sdb = {vol_mult:.4f}")
            for cand, name in ((1.0, "股"), (100.0, "手→股")):
                if abs(vol_mult - cand) / cand < 0.02:
                    print(f"  ⇒ 判定 stockdb 单位 = {name}（比值≈{cand}）⇒ STOCKDB_VOLUME_MULT={cand:g}")
                    break
            else:
                print("  ⚠️ 比值不接近 1 或 100 —— 可能有停牌/缺根/口径不同，别直接设，先查缺口")
    else:
        print(f"  ✗ 无 volume 列（列={list(sdb.columns)}）")

    # ③ 复权
    print("\n── ③ 复权（fq=None 是否等价不复权）──")
    merged = pd.merge(
        ad[ad["time"].dt.strftime("%Y-%m-%d").isin(ad_days)][["time", "close"]],
        sdb[["time", "close"]] if "close" in sdb.columns else pd.DataFrame(),
        on="time", how="inner", suffixes=("_ad", "_sdb"))
    if len(merged) == 0:
        print("  ⚠️ 无可对齐时点（标签口径不同？先用 LABEL_SHIFT 对齐再复测）")
    else:
        ratio = (merged["close_sdb"] / merged["close_ad"]).replace([float("inf"), -float("inf")], pd.NA).dropna()
        med = float(ratio.median())
        print(f"  可对齐 {len(merged)} 点，close 比值中位数 = {med:.6f}（1.0 = 完全一致）")
        print(f"  ⇒ {'一致，fq=None 可用于 live' if abs(med - 1) < 1e-4 else f'⚠️ 不一致（偏差 {(med-1)*100:+.3f}%），先查复权基准，不要切源'}")

    # ④ 每日根数
    print("\n── ④ 每日根数（AD 基准 240）──")
    sdb["_d"] = sdb["time"].dt.strftime("%Y-%m-%d")
    per_day = sdb.groupby("_d").size().to_dict()
    bad = {d: n for d, n in per_day.items() if n != BARS_PER_DAY}
    for d in ad_days:
        print(f"  {d}  stockdb {per_day.get(d, 0):>4} 根 {'✓' if per_day.get(d, 0) == BARS_PER_DAY else '⚠️'}")
    if bad:
        print(f"  ⚠️ 非 240 根的日期：{bad}")
    else:
        print("  ⇒ 全部 240 根，与 AD 一致")

    # 收尾：给可复制的环境变量
    print("\n" + "=" * 72)
    print("校准结论（实测值，复制到启动环境后即可放行 stockdb 数据源）")
    print("=" * 72)
    lines = [
        f"STOCKDB_CALIBRATED=1",
        f"STOCKDB_LABEL_SHIFT={shift_guess if shift_guess is not None else 0}",
        f"STOCKDB_VOLUME_MULT={vol_mult if vol_mult is not None else 1.0:.0f}"
        if vol_mult and abs(vol_mult - round(vol_mult)) < 0.02 else
        f"STOCKDB_VOLUME_MULT={vol_mult:.4f}" if vol_mult else "STOCKDB_VOLUME_MULT=待定",
        "STOCKDB_FQ=none",
    ]
    for line in lines:
        print("  " + line)
    print("\n（以上为本次实测量。若换 code / 换同步批次，须重跑本探针。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
