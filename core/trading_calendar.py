# -*- coding: utf-8 -*-
"""交易日历（H38）：**全项目唯一的"今天是不是交易日"判定**。

为什么必须收敛到一处
--------------------
H38 的成因是判定散布三层、强度不对称（任务层 Weekly 挡周末 / `watchdog.py:73`
自己判 `weekday()>=5` / `live_run` 完全不判）。节假日（工作日休市）只有任务层挡不住，
于是空跑 + 每实例写一次伪摘要（2026-09-25 中秋实测污染 **7 次**）。
⇒ 本模块是**唯一判定**，`live_run.py` 与 `watchdog.py` 都复用它，禁止再写第三处。

数据源与降级（不许静默）
------------------------
1. **QMT `xtdata.get_trading_dates`**（交易所日历，权威，只读）；
2. 本地缓存 `data/trading_calendar.csv`（QMT 不可用时读它）；
3. 都没有 ⇒ 退回"周一~周五"并 **WARN**（明确降级，不假装知道节假日）。

⚠️ 与"AD 空返回"的关系（登记里的方向 B）
---------------------------------------
**不把"接口连续空返回"当成休市判据**。登记原文已点明风险：行情源真实故障时
也会连续空返回，误判成休市比现状更糟（该收的盘不收、该告警的不告警）。
⇒ 日历为唯一休市判据；空返回仍按**故障**处理（进 H34 熔断/告警链路）。
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
CACHE_FILE = DATA_DIR / "trading_calendar.csv"

BJ = timezone(timedelta(hours=8))
_CACHE: dict[date, bool] = {}
_WARNED: set[str] = set()
# 日历覆盖上界 = 已拉到的最后一根已知交易日；之后的日期**不可**按"不在列表=休市"处理
_COVERAGE_END: date | None = None


def beijing_today(now: datetime | None = None) -> date:
    """北京日期（市场日期）。本机 GMT+3 ⇒ 不能直接用 `datetime.now().date()` 想当然。

    `now` 为 naive datetime 时按**本机时区**解释（便于测试传固定时刻）。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.astimezone()
    return now.astimezone(BJ).date()


def _warn_once(key: str, msg: str) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(msg, flush=True)


def _epoch_to_date(v) -> date:
    """QMT 的交易日是 epoch（**毫秒**，实测 1.7675424e12 量级）；按交易所时区换算。

    为兼容秒级返回值，按量级自适应（>1e11 视为毫秒）。
    """
    n = int(v)
    if n > 10 ** 11:
        n //= 1000
    return datetime.fromtimestamp(n, tz=timezone.utc).astimezone(BJ).date()


def _load_cache() -> None:
    if _CACHE or not CACHE_FILE.exists():
        return
    try:
        with open(CACHE_FILE, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                _CACHE[date.fromisoformat(row["date"])] = row["is_trading"] == "1"
    except Exception as exc:  # noqa: BLE001 —— 缓存坏了按无缓存处理
        _warn_once("cache_bad", f"[WARN] 交易日历缓存读取失败（{type(exc).__name__}），忽略: {CACHE_FILE}")


def _save_cache() -> None:
    if not _CACHE:
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "is_trading"])
            for d in sorted(_CACHE):
                w.writerow([d.isoformat(), "1" if _CACHE[d] else "0"])
        tmp.replace(CACHE_FILE)
    except Exception as exc:  # noqa: BLE001
        _warn_once("cache_write", f"[WARN] 交易日历缓存写入失败（{type(exc).__name__}）")


def _fetch_year(year: int) -> bool:
    """从 QMT 拉某年的交易日并写入内存缓存；失败返回 False（不抛）。

    🚨 **只缓存到「最后一根已知交易日」为止**（`coverage_end`）：
    `get_trading_dates` **不含未来日期**（实测 2026 年只返回到 10-23，之后全空），
    若照抄"不在返回列表 ⇒ 休市"会把下周一误判成节假日、**整条采集被跳过**。
    ⇒ 覆盖区外的日期一律**不写入缓存**，由 `is_trading_day` 走降级判定。
    """
    global _COVERAGE_END
    try:
        from xtquant import xtdata  # 延迟 import
    except Exception as exc:  # noqa: BLE001
        _warn_once("no_xtquant", f"[WARN] 交易日历：xtquant 不可用（{type(exc).__name__}），改用缓存/降级")
        return False
    try:
        raw = xtdata.get_trading_dates("SH", f"{year}0101", f"{year}1231")
    except Exception as exc:  # noqa: BLE001
        _warn_once(f"qmt_fail_{year}", f"[WARN] 交易日历：QMT 取数失败（{type(exc).__name__}），改用缓存/降级")
        return False
    if not raw:
        _warn_once(f"qmt_empty_{year}", f"[WARN] 交易日历：QMT 返回空（{year}），改用缓存/降级")
        return False
    trading = {_epoch_to_date(x) for x in raw}
    last_known = max(trading)
    if _COVERAGE_END is None or last_known > _COVERAGE_END:
        _COVERAGE_END = last_known
    d = date(year, 1, 1)
    while d.year == year and d <= last_known:
        _CACHE[d] = d in trading
        d += timedelta(days=1)
    _save_cache()
    return True


def is_trading_day(day: date) -> bool:
    """`day` 是否交易日。优先级：内存/文件缓存 → QMT → 周一~周五降级。"""
    _load_cache()
    if day in _CACHE:
        return _CACHE[day]
    if _fetch_year(day.year) and day in _CACHE:
        return _CACHE[day]
    if _COVERAGE_END is not None and day > _COVERAGE_END:
        # 交易所日历尚未发布到该日（QMT 只给历史）⇒ 只能按周一~周五降级，**不能判休市**
        _warn_once(f"beyond_{day.year}",
                   f"[WARN] 交易日历未覆盖 {day}（QMT 只到 {_COVERAGE_END}，未来日历未发布）"
                   f" ⇒ 降级为「周一~周五」判定")
        return day.weekday() < 5
    _warn_once(f"degrade_{day.year}",
               f"[WARN] 交易日历降级为「周一~周五」判定（{day.year} 无 QMT 日历也无缓存）"
               f" —— 法定节假日会误判为交易日")
    return day.weekday() < 5


def trading_days(begin: date, end: date) -> list[date]:
    """区间内交易日列表（对缓存缺失的年份会尝试补拉）。"""
    _load_cache()
    days, d = [], begin
    while d <= end:
        if d not in _CACHE:
            _fetch_year(d.year)
        days.append(d) if is_trading_day(d) else None
        d += timedelta(days=1)
    return days
