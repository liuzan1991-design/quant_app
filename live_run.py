# -*- coding: utf-8 -*-
"""观察期总入口：采集 + 撮合一起常驻。

采集进程与撮合循环统一协调：
- 每 30 秒采集一次当天实时分钟数据（AmazingData 单点登录，本进程唯一持有）。
- 采集后立即让撮合循环处理新 bar。
- 收盘缓冲：15:00 后继续采集，确认连续 N 次无新 bar（或本地时间达到收盘后缓冲上限）才写摘要退出。
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
LOG_DIR = APP_DIR / "live_outputs" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOGGER = logging.getLogger("live_run")
_LOGGER.setLevel(logging.INFO)
if not _LOGGER.handlers:
    _fh = logging.FileHandler(
        LOG_DIR / f"live_{datetime.now():%Y%m%d}.log", encoding="utf-8")
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"))
    _LOGGER.addHandler(_fh)
for _p in (str(APP_DIR), str(APP_DIR / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import live_match_loop as match  # noqa: E402
from core.market_collector import MarketCollector  # noqa: E402

CODES = [c for c, _ in match.COMBOS]
POLL_INTERVAL = 30
# 14:59 最后一根 bar 后，连续这么多次采集都没有新 bar，才判定收盘。
CLOSE_QUIET_ROUNDS = 6
# 兜底：即使一直有新 bar，本地时间（内罗毕=北京-5小时）到 10:10 也强制收盘。
# 北京 15:10 = 内罗毕 10:10。
CLOSE_HARD_LOCAL_HOUR = 10
CLOSE_HARD_LOCAL_MINUTE = 10


def _latest_bar_time(observers) -> datetime | None:
    latest = None
    for obs in observers:
        df = match._load_live(obs.code)
        if not df.empty:
            t = df["time"].max()
            if latest is None or t > latest:
                latest = t
    return latest


def _past_hard_close() -> bool:
    now = datetime.now()
    return (now.hour, now.minute) >= (CLOSE_HARD_LOCAL_HOUR, CLOSE_HARD_LOCAL_MINUTE)


def main() -> None:
    collector = MarketCollector(dry_run=False)
    observers = match._build_observers()
    quiet_rounds = 0
    last_seen = None

    _LOGGER.info("== 观察期启动 == codes=%s", CODES)
    while True:
        # 采集当天实时数据
        try:
            collected = collector.run_once(CODES, datetime.now().strftime("%Y-%m-%d"))
            _LOGGER.info("[collector] 采集完成 %s", collected)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("[collector] 采集失败")

        # 撮合新 bar
        summary = match._run_once(observers, match.OUTPUT_DIR)
        for obs in observers:
            equity = obs.broker.account.equity()
            obs.day_high_equity = max(obs.day_high_equity, equity)
            obs.equity_high = max(obs.equity_high, equity)
            obs.persist_all()
        _LOGGER.info("[match] %s", summary)
        print(summary)

        # 收盘判定：数据最后 bar 到 14:59 后，连续无新 bar 才写摘要。
        latest = _latest_bar_time(observers)
        if latest is not None and latest.time() >= datetime.strptime("14:59", "%H:%M").time():
            if last_seen is not None and latest == last_seen:
                quiet_rounds += 1
            else:
                quiet_rounds = 0
            last_seen = latest
            if quiet_rounds >= CLOSE_QUIET_ROUNDS or _past_hard_close():
                _LOGGER.info("== 收盘缓冲结束，写每日摘要 ==")
                print("== 收盘缓冲结束，写每日摘要 ==")
                for obs in observers:
                    summary_item = match._write_daily_summary(obs)
                    _LOGGER.info("[daily] %s", summary_item)
                break

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()