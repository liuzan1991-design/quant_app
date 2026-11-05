# -*- coding: utf-8 -*-
"""开机补跑：电脑重启/断电恢复后，检查当天是否漏采，漏了就补。

背景：2026-11-05 早上 9:30 电脑没电，计划任务 QuantLiveRun（本机 04:30
触发）错过且不会自动补跑，当天 data/live 空缺一整天。

本脚本放在「启动」文件夹，登录时自动执行：
  1. 非交易日 / 非交易时段 → 直接退出
  2. 采集器正在跑（今日 live 日志 10 分钟内有更新）→ 退出，避免抢 AD 登录
  3. 当日 data/live 已有数据 → 退出
  4. 否则调用 scripts/qmt_ad_backfill.py --force 补当天

日志：D:\\Codex输出\\qmt_startup\\catchup_YYYYMMDD.log

用法：
    python scripts/qmt_startup_catchup.py            # 正常（供开机自启）
    python scripts/qmt_startup_catchup.py --dry-run  # 只看会做什么
"""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
LIVE_DIR = APP_DIR / "data" / "live"
LOG_DIR = Path(r"D:\Codex输出\qmt_startup")
PY = Path(r"D:\Anaconda3\python.exe")
BACKFILL = APP_DIR / "scripts" / "qmt_ad_backfill.py"
LIVE_LOGS = APP_DIR / "live_outputs" / "logs"

BJ = dt.timezone(dt.timedelta(hours=8))


def bj_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(BJ)


def log(msg: str) -> None:
    line = "[%s] %s" % (bj_now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / ("catchup_%s.log" % bj_now().strftime("%Y%m%d"))).open(
                "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def live_codes() -> list[str]:
    import json
    try:
        cfg = json.loads((APP_DIR / "data" / "live_config.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[str] = []
    for p in cfg.get("positions", []):
        c = str(p.get("code", "")).strip()
        if c and c not in out:
            out.append(c)
    return out


def today_rows(code: str, iso: str) -> int:
    p = LIVE_DIR / ("stock_%s_1m.csv" % code)
    if not p.exists():
        return -1
    import pandas as pd
    try:
        df = pd.read_csv(p, encoding="utf-8-sig", usecols=["time"])
    except Exception:
        return -1
    return int((df["time"].astype(str).str[:10] == iso).sum())


def collector_running(minutes: int = 10) -> tuple[bool, str]:
    logf = LIVE_LOGS / ("live_%s.log" % bj_now().strftime("%Y%m%d"))
    if not logf.exists():
        return False, "今日无采集日志"
    age = (dt.datetime.now().timestamp() - logf.stat().st_mtime) / 60.0
    return age <= minutes, "采集日志 %.1f 分钟前更新" % age


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    now = bj_now()
    log("开机补跑启动 | 北京 %s %s | dry-run=%s" % (now.strftime("%Y-%m-%d %H:%M:%S"),
                                                now.strftime("%a"), args.dry_run))

    if now.weekday() >= 5:
        log("非交易日，退出")
        return 0
    hm = now.hour * 60 + now.minute
    if not (9 * 60 + 35) <= hm <= (23 * 60):
        log("不在补跑时段（09:35–23:00），退出")
        return 0

    running, why = collector_running()
    if running:
        log("采集器在跑（%s），不补，退出" % why)
        return 0
    log("采集器未在跑（%s）" % why)

    iso = now.strftime("%Y-%m-%d")
    codes = live_codes()
    if not codes:
        log("live_config 无标的，退出")
        return 2

    missing = []
    for c in codes:
        n = today_rows(c, iso)
        log("  %s 当日 %d 行" % (c, n))
        if n == 0:
            missing.append(c)
    if not missing:
        log("当日数据已存在，无需补采，退出")
        return 0

    cmd = [str(PY), "-X", "utf8", str(BACKFILL), "--force",
           "--codes", ",".join(missing)]
    log("执行补采：%s" % " ".join(cmd))
    if args.dry_run:
        log("[dry-run] 不执行")
        return 0

    try:
        r = subprocess.run(cmd, cwd=str(APP_DIR), capture_output=True,
                           text=True, timeout=900, encoding="utf-8", errors="replace")
    except Exception as e:
        log("补采调用失败：%s: %s" % (type(e).__name__, e))
        return 3

    for line in (r.stdout or "").strip().splitlines():
        log("  | " + line)
    for line in (r.stderr or "").strip().splitlines()[-5:]:
        log("  ! " + line)
    log("补采退出码 %d" % r.returncode)

    for c in missing:
        log("  补采后 %s 当日 %d 行" % (c, today_rows(c, iso)))
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
