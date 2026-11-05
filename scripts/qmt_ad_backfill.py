# -*- coding: utf-8 -*-
"""补采 AD 分钟线到 data/live/（计划任务错过时的补数工具）。

场景：2026-11-05 电脑断电，计划任务 QuantLiveRun（本机 04:30 触发）
错过，当天 data/live 当天 0 行。

⚠️ 为什么不用 core.data_fetch.incremental_update
------------------------------------------------
它的覆盖判据是「first <= begin+5d 且 last >= end 15:00 且 in_range > 0」，
`in_range > 0` 只看"区间内有任意数据"，不是真实交易日覆盖率 ——
实测会直接早退返回"无需更新"（假绿，见 scripts/tool_data_gap_fill.py
开头的 A20 记录）。补数用它会报成功但什么都不做。

本工具照 A20 的做法：
  ① 直接调 `fetch_kline`（只返回 DataFrame、不写盘）；
  ② **纯追加**：只补本地没有的时间戳，不动任何已有一行；
  ③ 写前备份、写后验证（原有行未改动 / 时间单调 / 重复 0）。

用法
----
    python scripts/qmt_ad_backfill.py --dry-run
    python scripts/qmt_ad_backfill.py --date 20261105
    python scripts/qmt_ad_backfill.py --date 20261105 --force   # 忽略采集窗口检查

退出码：0 成功；2 参数/窗口/配置错；3 拉取失败
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

LIVE_DIR = APP_DIR / "data" / "live"
CONFIG = APP_DIR / "data" / "live_config.json"
BACKUP_ROOT = Path(r"C:\Users\27329\quant_app_backups")


def live_codes() -> list[str]:
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception as e:
        print("读 live_config 失败：%s" % e)
        return []
    out: list[str] = []
    for p in cfg.get("positions", []):
        c = str(p.get("code", "")).strip()
        if c and c not in out:
            out.append(c)
    return out


def in_collect_window(now: dt.datetime | None = None) -> bool:
    """本机 04:30–10:15 周一~五 = 北京 09:30–15:15，采集器在跑。
    此窗口内补数会与采集器抢 AD 单点登录。"""
    now = now or dt.datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (4 * 60 + 30) <= hm <= (10 * 60 + 15)


def read_csv(p: Path) -> pd.DataFrame:
    df = pd.read_csv(p, encoding="utf-8-sig")
    df["time"] = pd.to_datetime(df["time"])
    return df


def rows_on(df: pd.DataFrame, iso: str) -> int:
    return int((df["time"].dt.strftime("%Y-%m-%d") == iso).sum())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().strftime("%Y%m%d"))
    ap.add_argument("--codes", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="忽略采集窗口检查")
    args = ap.parse_args(argv)

    date = args.date
    if len(date) != 8 or not date.isdigit():
        print("日期格式应为 YYYYMMDD：%s" % date)
        return 2
    iso = "%s-%s-%s" % (date[:4], date[4:6], date[6:])

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or live_codes()
    if not codes:
        print("未指定 --codes，且 live_config 里没有标的")
        return 2

    do_apply = not args.dry_run
    print("模式：%s" % ("【APPLY 写盘】" if do_apply else "【DRY-RUN 不写盘】"))
    print("补采日期 %s | 标的 %s" % (iso, ",".join(codes)))
    if in_collect_window() and not args.force:
        print("!! 落在采集窗口（本机 04:30–10:15 周一~五）⇒ 拒绝执行，避免抢 AD 登录")
        print("   确认采集器已停可加 --force")
        return 2

    from core.data_fetch import fetch_kline  # noqa: E402

    backup_dir = None
    if do_apply:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = BACKUP_ROOT / ("ad_backfill_%s" % stamp)
        backup_dir.mkdir(parents=True, exist_ok=True)
        print("备份目录：%s" % backup_dir)

    rc = 0
    for code in codes:
        p = LIVE_DIR / ("stock_%s_1m.csv" % code)
        if not p.exists():
            print("%s ❌ 本地文件不存在" % code)
            rc = 3
            continue

        old = read_csv(p)
        old_ts = set(old["time"])
        n_before = rows_on(old, iso)

        try:
            new = fetch_kline(code, iso, iso, period="min1")
        except Exception as exc:  # noqa: BLE001
            print("%s ❌ 拉取异常：%s: %s" % (code, type(exc).__name__, exc))
            rc = 3
            continue

        if new is None or not len(new):
            print("%s ❌ 源侧返回空（%s）" % (code, iso))
            rc = 3
            continue

        new = new.copy()
        new["time"] = pd.to_datetime(new["time"])
        new = new[new["time"].dt.strftime("%Y-%m-%d") == iso]
        add = new[~new["time"].isin(old_ts)].copy()
        n_add = len(add)

        if not do_apply:
            print("%s 将补 %d 根 | 当日已有 %d 行 | 源侧返回 %d 根"
                  % (code, n_add, n_before, len(new)))
            continue

        if n_add:
            if backup_dir is not None:
                shutil.copy2(p, backup_dir / p.name)
            merged = (pd.concat([old, add], ignore_index=True)
                      .sort_values("time").reset_index(drop=True))
            merged = merged[list(old.columns)]
            merged.to_csv(p, index=False, encoding="utf-8-sig")

            chk = read_csv(p)
            inter = chk[chk["time"].isin(old_ts)].sort_values("time").reset_index(drop=True)
            old_s = old.sort_values("time").reset_index(drop=True)
            same = inter.equals(old_s)
            mono = chk["time"].is_monotonic_increasing
            dup = int(chk["time"].duplicated().sum())
            print("%s ✅ 补 %d 根 | 当日 %d → %d 行 | 原有行未改动 %s | 时间单调 %s | 重复 %d | 共 %d 行"
                  % (code, n_add, n_before, rows_on(chk, iso), same, mono, dup, len(chk)))
            if not same or not mono or dup:
                rc = 3
        else:
            print("%s ○ 无需补充 | 当日已有 %d 行" % (code, n_before))

    return rc


if __name__ == "__main__":
    sys.exit(main())
