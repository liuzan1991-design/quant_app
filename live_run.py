# -*- coding: utf-8 -*-
"""观察期总入口：采集 + 撮合一起常驻。

采集进程与撮合循环统一协调：
- 每 30 秒采集一次当天实时分钟数据（AmazingData 单点登录，本进程唯一持有）。
- 采集后立即让撮合循环处理新 bar。
- 收盘缓冲：15:00 后继续采集，确认连续 N 次无新 bar（或本地时间达到收盘后缓冲上限）才写摘要退出。
"""
from __future__ import annotations

import json
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

# 控制台输出重定向：计划任务下 stdout/stderr 无处可去，未捕获 traceback 会静默丢失
# （2026-09-09 12:43 进程无声死亡，就是崩溃现场没留痕）。这里全部落到文件。
_CONSOLE_LOG = open(LOG_DIR / f"console_{datetime.now():%Y%m%d}.log",
                    "a", buffering=1, encoding="utf-8")
sys.stdout = _CONSOLE_LOG
sys.stderr = _CONSOLE_LOG
try:
    import faulthandler
    faulthandler.enable(_CONSOLE_LOG)  # 段错误等硬崩溃也留 Python 层栈
except Exception:
    pass

import live_match_loop as match  # noqa: E402
from core.market_collector import MarketCollector  # noqa: E402

# COMBOS 里同一 code 可能对应多个策略，去重保留出现顺序（日志里不再出现重复 code）。
CODES = list(dict.fromkeys(c for c, _ in match.COMBOS))
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


def _write_heartbeat(path: Path, started_at: datetime, status: str,
                     last_success_at: datetime | None, collect_errors: int = 0,
                     match_errors: int = 0) -> None:
    """启动心跳：一启动就落盘，采集成功持续更新。

    用途：health.json 只在收盘写，盘中"启动即死 / 采集停摆"靠它暴露——
    - 没有 heartbeat.json            → 今天根本没启动（计划任务漏触发）；
    - started_at 有、last_success_at 为 None → 启动了但从未采到数据（登录失败/网络断）；
    - last_success_at 停在某个时刻    → 从那之后采集停摆（中途断网/崩溃）。
    """
    try:
        heartbeat = {
            "date": started_at.strftime("%Y-%m-%d"),
            "started_at": started_at.isoformat(timespec="seconds"),
            "status": status,
            "last_success_at": (last_success_at.isoformat(timespec="seconds")
                                if last_success_at is not None else None),
            "collect_errors": collect_errors,
            "match_errors": match_errors,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        path.write_text(json.dumps(heartbeat, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception:  # noqa: BLE001
        _LOGGER.exception("[heartbeat] 心跳写入失败")


def main() -> None:
    started_at = datetime.now()
    health_path = match.OUTPUT_DIR / "health.json"
    heartbeat_path = match.OUTPUT_DIR / "heartbeat.json"
    # 启动心跳：即使随后 init/build_observers/login 卡死或崩，也能看出"今天启动过但没采到数据"。
    _write_heartbeat(heartbeat_path, started_at, status="starting",
                     last_success_at=None, collect_errors=0, match_errors=0)

    # 启动段之前不在 try/except 里，断网/损坏曾导致静默退出、无 traceback。
    try:
        collector = MarketCollector(dry_run=False)
        # 观察期启动时，用历史 CSV 初始化 live 文件（幂等），之后采集追加不覆盖。
        for code in CODES:
            collector.init_from_history(code)
        observers = match._build_observers()
    except Exception:
        _LOGGER.exception("== 启动段初始化失败（init_from_history / build_observers）==")
        _write_heartbeat(heartbeat_path, started_at, status="failed",
                         last_success_at=None, collect_errors=0, match_errors=0)
        raise

    quiet_rounds = 0
    last_seen = None
    # 健康标记累积：采集异常次数 + 各组合累计订单/成交/最新水位。
    collect_errors = 0
    last_collected = {}
    last_success_at = None

    _LOGGER.info("== 观察期启动 == codes=%s", CODES)
    match_errors = 0  # 连续撮合异常计数；超过阈值说明状态已坏，退出交计划任务重启
    while True:
        # 采集当天实时数据
        try:
            collected = collector.run_once(CODES, datetime.now().strftime("%Y-%m-%d"))
            last_collected = dict(collected) if isinstance(collected, dict) else {}
            last_success_at = datetime.now()
            _write_heartbeat(heartbeat_path, started_at, status="running",
                             last_success_at=last_success_at,
                             collect_errors=collect_errors, match_errors=match_errors)
            _LOGGER.info("[collector] 采集完成 %s", collected)
        except Exception:  # noqa: BLE001
            collect_errors += 1
            _LOGGER.exception("[collector] 采集失败")
            # 采集失败也落心跳（last_success_at 保持上次成功值），让断网可从 collect_errors 观测。
            _write_heartbeat(heartbeat_path, started_at, status="running",
                             last_success_at=last_success_at,
                             collect_errors=collect_errors, match_errors=match_errors)

        # 撮合新 bar：单轮异常只记日志不退出，下一轮从持久化状态继续。
        try:
            summary = match._run_once(observers, match.OUTPUT_DIR)
            for obs in observers:
                equity = obs.broker.account.equity()
                obs.day_high_equity = max(obs.day_high_equity, equity)
                obs.equity_high = max(obs.equity_high, equity)
                try:
                    obs.persist_all()
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("[persist] %s 持久化失败", obs.strategy_id)
            _LOGGER.info("[match] %s", summary)
            print(summary)
            match_errors = 0
        except Exception:  # noqa: BLE001
            match_errors += 1
            _LOGGER.exception("[match] 撮合循环异常（连续第 %d 次）", match_errors)
            if match_errors >= 10:
                _LOGGER.critical("[match] 连续 %d 轮撮合异常，退出交由计划任务重启",
                                 match_errors)
                raise

        # 收盘判定：数据最后 bar 到 14:59 后，连续无新 bar 才写摘要。
        try:
            latest = _latest_bar_time(observers)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("[latest] 读取最新bar时间失败，本轮跳过收盘判定")
            latest = None
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
                # 每天必写健康标记（正常也写），文件日期未更新即视为断签。
                health = {
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "collect_errors": collect_errors,
                    "last_collected": last_collected,
                    "observers": {
                        f"{obs.code}×{obs.strategy_id}": {
                            # 最新水位 = processed_times 里最大的已处理 bar 时间
                            # （Observer 无 .watermark 属性，此前的 AttributeError 即因此）
                            "latest_watermark": (max(obs.processed_times).isoformat()
                                                  if obs.processed_times else None),
                            "orders": len(obs.broker.orders),
                            "fills": len(obs.broker.fills),
                        }
                        for obs in observers
                    },
                }
                health_path.write_text(json.dumps(health, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
                _LOGGER.info("[health] %s", health)
                break

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _LOGGER.exception("== 主循环致命退出（非零码，交由计划任务重启）==")
        raise SystemExit(1)