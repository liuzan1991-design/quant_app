# -*- coding: utf-8 -*-
"""从 QMT 策略输出日志导出只读运行器的数据为 CSV。

背景：2026-11-14 起券商关闭 QMT 内置 Python 的文件读写能力，
策略侧只能 print()，输出落到
    <安装目录>\\userdata\\log\\XtClient_FormulaOutput_YYYYMMDD.log
本脚本在 QMT 外部运行（不受限制），把日志里的 [RO] 行解析成 CSV。

用法：
    python scripts/qmt_log_export.py                # 今天
    python scripts/qmt_log_export.py --date 20261104
    python scripts/qmt_log_export.py --log <路径>

输出：
    D:\\Codex输出\\qmt_readonly\\ro_<日期>.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

QMT_LOG_DIR = Path("D:/迅投QMT交易终端   国盛证券实盘版/userdata/log")
OUT_DIR = Path(r"D:\Codex输出\qmt_readonly")

TAG = "[RO] "


def log_path(date: str) -> Path:
    return QMT_LOG_DIR / ("XtClient_FormulaOutput_%s.log" % date)


def extract(logfile: Path) -> tuple[list[str], list[str]]:
    """返回 (元信息行, 数据行)。"""
    meta: list[str] = []
    rows: list[str] = []
    if not logfile.exists():
        return meta, rows
    for raw in logfile.read_text(encoding="utf-8", errors="replace").splitlines():
        payload = None
        if "output = " in raw:
            tail = raw.split("output = ", 1)[1]
            if TAG in tail:
                payload = tail.split(TAG, 1)[1]
        elif raw.startswith(TAG) or raw.lstrip().startswith(TAG):
            payload = raw.lstrip().split(TAG, 1)[1]
        if payload is None:
            continue
        payload = payload.rstrip()
        if not payload:
            continue
        if payload[0].isdigit():
            rows.append(payload)
        else:
            meta.append(payload)
    # 去重：同一条 bar 可能因重启被打印多次。日志是追加写的，
    # 所以后出现的更可信（前面的可能来自旧版本/重启前的运行）。
    last: dict[str, str] = {}
    order: list[str] = []
    for r in rows:
        key = r.split(",", 2)[1] if "," in r else r
        if key not in last:
            order.append(key)
        last[key] = r
    uniq: list[str] = [last[k] for k in order]
    return meta, uniq


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().strftime("%Y%m%d"))
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    lp = Path(args.log) if args.log else log_path(args.date)
    if not lp.exists():
        print("日志不存在: %s" % lp)
        return 2

    meta, rows = extract(lp)
    if not rows:
        print("日志里没有 [RO] 数据行: %s" % lp)
        for m in meta[:10]:
            print("  meta:", m)
        return 3

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / ("ro_%s.csv" % args.date)
    header = rows[0] if rows[0].startswith("bar,") else None
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["bar", "bar_ts", "bj_date", "bj_time", "open", "high",
                    "low", "close", "vol", "day_cum", "pos_vol",
                    "pos_sellable", "avail", "signal", "ticks"])
        n = 0
        for r in rows:
            if r.startswith("bar,"):
                continue
            w.writerow(r.split(","))
            n += 1

    print("日志: %s" % lp)
    print("元信息 %d 条，数据行 %d 条 -> %s" % (len(meta), n, out))
    print("首根: %s" % rows[1].split(",")[2:4])
    print("末根: %s" % rows[-1].split(",")[2:4])
    return 0


if __name__ == "__main__":
    sys.exit(main())
