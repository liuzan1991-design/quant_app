# -*- coding: utf-8 -*-
"""QMT 策略心跳监控（在 QMT 外部运行）。

为什么需要：2026-11-04 实测 QMT 客户端在 14:46 静默退出，策略随之停摆，
没有任何告警 —— 与 AD 侧 H34 同类的"静默死亡"。

2026-11-14 之后策略内不能写文件、不能联网，所以策略侧只能 print()。
本脚本读日志做两件事：
  1) 客户端还活着吗（日志文件是否还在增长）
  2) 策略还在出数据吗（最后一条 [RO] 行有多老）

用法：
    python scripts/qmt_hb_watch.py                  # 单次检查
    python scripts/qmt_hb_watch.py --stale-min 5    # 自定义阈值
    python scripts/qmt_hb_watch.py --loop 60        # 每 60 秒循环

退出码：0 正常；1 过期（疑似停摆）；2 日志不存在
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import time
from pathlib import Path

LOG_DIR = Path("D:/迅投QMT交易终端   国盛证券实盘版/userdata/log")
TAG = "[RO] "
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def latest_line() -> tuple[dt.datetime | None, str]:
    """返回 (最后一条策略输出行的日志时间, 该行内容)。日志时间=本机时区。"""
    lp = LOG_DIR / ("XtClient_FormulaOutput_%s.log" % time.strftime("%Y%m%d"))
    if not lp.exists():
        return None, ""
    last_ts = None
    last_txt = ""
    with lp.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if TAG not in line:
                continue
            m = TS_RE.match(line)
            if not m:
                continue
            try:
                last_ts = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            last_txt = line.rstrip()
    return last_ts, last_txt


def in_session(now: dt.datetime) -> bool:
    """本机时区 GMT+3；北京时间 = 本机 + 5 小时。只在 A 股时段内告警。"""
    bj = now + dt.timedelta(hours=5)
    if bj.weekday() >= 5:
        return False
    hm = bj.hour * 60 + bj.minute
    # 午休 11:30–13:00 无 tick ⇒ 无新 [RO] 行，判活无意义；13:00 后留 5 分钟宽限。
    if (11 * 60 + 30) <= hm < (13 * 60 + 5):
        return False
    return (9 * 60 + 30) <= hm <= (15 * 60)


def check(stale_min: int, quiet: bool) -> int:
    now = dt.datetime.now()
    ts, txt = latest_line()
    if ts is None:
        if not quiet:
            print("[HB] 今日日志不存在或无 [RO] 行 | 本机 %s | 北京 %s"
                  % (now.strftime("%H:%M:%S"),
                     (now + dt.timedelta(hours=5)).strftime("%H:%M:%S")))
        return 2
    age = (now - ts).total_seconds() / 60.0
    bj = (now + dt.timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
    status = "OK" if age <= stale_min else "STALE"
    if not quiet or status != "OK":
        print("[HB] %s 最后输出 %.1f 分钟前 | 本机 %s | 北京 %s"
              % (status, age, now.strftime("%H:%M:%S"), bj))
        print("     %s" % txt[-160:])
    if status == "STALE" and in_session(now):
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale-min", type=int, default=5)
    ap.add_argument("--loop", type=int, default=0, help="秒；0=只跑一次")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not args.loop:
        return check(args.stale_min, args.quiet)
    rc = 0
    while True:
        rc = check(args.stale_min, args.quiet)
        time.sleep(max(10, args.loop))
        if rc == 2:
            return 2


if __name__ == "__main__":
    sys.exit(main())
