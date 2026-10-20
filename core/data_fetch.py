# -*- coding: utf-8 -*-
"""星耀数智（AmazingData）1 分钟数据增量拉取，写入 APP 本地数据目录。"""
from __future__ import annotations

import json
import socket
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

from .ad_silence import suppress_sdk_output
from .ad_errors import (  # noqa: F401  （部分名称在此 re-export，供 live_run / 工具脚本取用）
    AdCalendarError,
    AdError,
    AdFetchTimeout,
    AdKlineEmpty,
    AdLoginError,
    AdLoginTimeout,
    AdSdkError,
    classify,
    format_failure_line,
)

APP_DIR = Path(__file__).resolve().parents[1]
CRED_FILE = Path(r"D:\Codex输出\ad_credentials.json")
STD_COLS = ["time", "open", "high", "low", "close", "volume", "amount"]
_AD_CACHE = {}

# 登录软超时（秒）：断网时 ad.login() 会无限阻塞（第三方 SDK 不设超时），
# 必须在线程里 join 超时，否则整个采集进程卡死在登录、不抛异常、无 traceback。
_LOGIN_TIMEOUT = 45

# SDK 调用期的 socket 默认超时（秒）。
# 🚨 H44 实测根因之一：登录结束后 finally 把 setdefaulttimeout 恢复成 None，
#    于是后续 get_calendar()/query_kline() 全部无超时裸奔（TypeError 块实测
#    单轮阻塞 158~397 秒）。这里把它覆盖到**整个** fetch 过程，而不是只在登录期。
_SDK_SOCKET_TIMEOUT = 20

# 盘中单轮硬上限（秒）：socket 超时只管单次收发，管不住 SDK 内部重试循环，
# 因此再叠一层墙钟上限。仅 live 采集路径启用（market_collector.fetch_today），
# 历史批量补数不启用 —— 长区间拉取本就该慢，不该被硬砍。
LIVE_FETCH_TIMEOUT = 90


def code_to_ad(code: str) -> str:
    if "." in code:
        return code
    if code.startswith(("60", "68", "51", "56", "58", "50")):
        return f"{code}.SH"
    if code.startswith(("00", "30", "12", "15", "16", "18")):
        return f"{code}.SZ"
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"{code}.BJ"
    return f"{code}.SH"


def load_credentials() -> dict:
    if not CRED_FILE.exists():
        raise FileNotFoundError("未找到星耀数智凭证文件 ad_credentials.json")
    return json.loads(CRED_FILE.read_text(encoding="utf-8"))


def _login():
    import AmazingData as ad
    if _AD_CACHE.get("ad") is not None:
        return _AD_CACHE["ad"]
    cred = load_credentials()

    result: dict = {}

    def _worker():
        # 底层 socket 默认超时：兜底 connect/recv 无限阻塞（SDK 可能显式 timeout=None）。
        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(30)
        try:
            with suppress_sdk_output():
                ad.login(username=cred["username"], password=cred["password"],
                         host=cred["host"], port=int(cred["port"]))
            result["ad"] = ad
        except Exception as exc:  # noqa: BLE001
            result["err"] = exc
        finally:
            socket.setdefaulttimeout(old)

    t = threading.Thread(target=_worker, daemon=True, name="ad-login")
    t.start()
    t.join(timeout=_LOGIN_TIMEOUT)
    if "err" in result:
        raise AdLoginError(
            f"星耀登录失败：{type(result['err']).__name__}: {result['err']}",
            stage="login") from result["err"]
    if "ad" not in result:
        raise AdLoginTimeout(
            f"星耀登录超时（>{_LOGIN_TIMEOUT}秒），疑似网络不通或服务器无响应",
            stage="login")
    _AD_CACHE["ad"] = result["ad"]
    return _AD_CACHE["ad"]


def _run_with_deadline(fn, timeout: float):
    """在守护线程里跑 fn，超过 timeout 抛 AdFetchTimeout。

    ⚠️ Python 无法强杀线程 ⇒ 超时后**放弃**该线程（daemon，进程退出即回收）。
    放弃的线程靠 `_SDK_SOCKET_TIMEOUT` 的 socket 默认超时自行退出，
    所以 socket 超时必须在外层设置（本函数不负责，避免被放弃线程回改全局值）。
    """
    box: dict = {}

    def _worker():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=_worker, daemon=True, name="ad-fetch")
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AdFetchTimeout(
            f"星耀采集整轮硬超时（>{timeout}秒）：SDK 无响应，本轮放弃",
            stage="deadline")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _fetch_kline_inner(code: str, begin: str, end: str,
                       period: str = "min1") -> pd.DataFrame:
    """真实拉取逻辑（不含超时包装）。每个环节按 H44 分类返错。"""
    ad_code = code_to_ad(code)
    b = int(begin.replace("-", ""))
    e = int(end.replace("-", ""))
    for attempt in range(2):
        ad = _login()
        try:
            base = ad.BaseData()
        except Exception as exc:  # noqa: BLE001
            raise AdSdkError(
                f"星耀 BaseData() 构造失败：{type(exc).__name__}: {exc}",
                stage="calendar") from exc
        try:
            calendar = base.get_calendar()
        except Exception as exc:  # noqa: BLE001
            # 实测形态：SDK base_data.py:50 'NoneType' object is not subscriptable
            raise AdCalendarError(
                f"星耀 get_calendar() 抛 {type(exc).__name__}: {exc}",
                stage="calendar") from exc
        if calendar is None:
            raise AdCalendarError(
                "星耀 get_calendar() 返回 None（SDK 未就绪或会话异常）",
                stage="calendar")
        try:
            market = ad.MarketData(calendar)
            period_val = getattr(ad.constant.Period, period).value
        except Exception as exc:  # noqa: BLE001
            raise AdSdkError(
                f"星耀 MarketData 初始化失败：{type(exc).__name__}: {exc}",
                stage="query") from exc
        try:
            kline = market.query_kline([ad_code], begin_date=b, end_date=e,
                                       period=period_val)
        except Exception as exc:  # noqa: BLE001
            raise AdSdkError(
                f"星耀 query_kline 抛 {type(exc).__name__}: {exc}",
                stage="query") from exc
        if kline is not None and ad_code in kline and kline[ad_code] is not None:
            df = kline[ad_code].reset_index(drop=True)
            break
        # 连接可能被顶/失效：清缓存重登重试一次
        _AD_CACHE.clear()
        if attempt == 1:
            raise AdKlineEmpty(
                f"星耀接口未返回 {ad_code} 的数据（可能账号连接被占用或代码无效，"
                f"请检查是否其他会话正在使用星耀）", stage="query")
    else:
        raise AdKlineEmpty(f"星耀接口未返回 {ad_code} 的数据", stage="query")
    time_col = next((c for c in ("kline_time", "time") if c in df.columns), None)
    if time_col is None:
        raise AdSdkError(f"K线返回中未找到时间列：{list(df.columns)}", stage="parse")
    df = df.rename(columns={time_col: "time"})
    for col in STD_COLS:
        if col not in df.columns:
            df[col] = 0
    return df[STD_COLS].copy()


def fetch_kline(code: str, begin: str, end: str, period: str = "min1",
                hard_timeout: Optional[float] = None) -> pd.DataFrame:
    """拉取 [begin, end]（YYYY-MM-DD）的K线数据，period 支持 min1/day 等，返回标准列 DataFrame。

    hard_timeout：
      - `None`（默认）：只加 socket 默认超时，不加上限 —— 历史批量补数走这条路，
        长区间本就该慢，不该被硬砍。
      - 数值：整轮 wall-clock 硬上限（含内部重登重试）。**盘中采集必须传**，
        否则 SDK 内部重试循环可把单轮拖到 158~397 秒（H44 实测）。

    socket 默认超时在外层设置：超时被放弃的工作线程不会回改全局值（否则会把
    外层刚设的值恢复成 None，重新变成无超时裸奔）。

    ⚠️ 硬超时触发时**故意不复原**全局 socket 超时：被放弃的工作线程仍在跑 SDK
    调用，若此刻把全局值恢复成 `None`，它立刻重新变成无限阻塞。代价是此后本进程
    的 socket 默认超时停在 `_SDK_SOCKET_TIMEOUT`（有界，比 `None` 安全）。
    """
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_SDK_SOCKET_TIMEOUT)
    abandoned = False
    try:
        if hard_timeout is None:
            return _fetch_kline_inner(code, begin, end, period)
        return _run_with_deadline(
            lambda: _fetch_kline_inner(code, begin, end, period), hard_timeout)
    except AdFetchTimeout:
        abandoned = True
        raise
    finally:
        if not abandoned:
            socket.setdefaulttimeout(old)


def incremental_update(code: str, start: str, end: str,
                       data_dir: Optional[Path] = None) -> Tuple[int, int, str]:
    """增量更新本地 CSV。已有数据只补"上次日期之后 + 最近 7 天覆盖"；无数据则全量拉。"""
    data_dir = Path(data_dir) if data_dir else APP_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / f"stock_{code}_1m.csv"

    existing = None
    if target.exists():
        existing = pd.read_csv(target, parse_dates=["time"])

    begin_dt = pd.Timestamp(start)
    end_dt = pd.Timestamp(end)
    if existing is not None and len(existing):
        first = existing["time"].min()
        last = existing["time"].max()
        in_range = ((existing["time"] >= begin_dt) &
                    (existing["time"] <= end_dt.replace(hour=15, minute=0))).sum()
        if (first <= begin_dt + pd.Timedelta(days=5)
                and last >= end_dt.replace(hour=15, minute=0)
                and in_range > 0):
            return 0, len(existing), f"本地数据已覆盖 {first:%Y-%m-%d} ~ {last:%Y-%m-%d}，无需更新"
        if end_dt < first:
            begin_dt = pd.Timestamp(start)      # 目标区间在已有数据之前：全量拉目标区间
        elif begin_dt > last:
            begin_dt = last - pd.Timedelta(days=7)  # 目标区间在已有数据之后：增量补缺口
        else:
            begin_dt = pd.Timestamp(start)      # 部分重叠：全量拉目标区间，保证完整

    new = fetch_kline(code, begin_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
    if not len(new):
        total = len(existing) if existing is not None else 0
        return 0, total, f"区间内没有新数据（{begin_dt:%Y-%m-%d} ~ {end_dt:%Y-%m-%d}）"

    if existing is not None and len(existing):
        merged = pd.concat([existing, new], ignore_index=True)
    else:
        merged = new
    merged = merged.drop_duplicates(subset="time", keep="last").sort_values("time").reset_index(drop=True)
    merged.to_csv(target, index=False, encoding="utf-8-sig")
    return len(merged) - (len(existing) if existing is not None else 0), len(merged), \
        f"更新完成：新增 {len(merged) - (len(existing) if existing is not None else 0)} 行，共 {len(merged)} 行"
