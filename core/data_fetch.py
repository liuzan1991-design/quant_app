# -*- coding: utf-8 -*-
"""星耀数智（AmazingData）1 分钟数据增量拉取，写入 APP 本地数据目录。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

from .ad_silence import suppress_sdk_output

APP_DIR = Path(__file__).resolve().parents[1]
CRED_FILE = Path(r"D:\Codex输出\ad_credentials.json")
STD_COLS = ["time", "open", "high", "low", "close", "volume", "amount"]
_AD_CACHE = {}


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


# 登录软超时（秒）：断网时 ad.login() 会无限阻塞（第三方 SDK 不设超时），
# 必须在线程里 join 超时，否则整个采集进程卡死在登录、不抛异常、无 traceback。
_LOGIN_TIMEOUT = 45


def _login():
    import socket
    import threading
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

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=_LOGIN_TIMEOUT)
    if "err" in result:
        raise result["err"]
    if "ad" not in result:
        raise TimeoutError(
            f"星耀登录超时（>{_LOGIN_TIMEOUT}秒），疑似网络不通或服务器无响应")
    _AD_CACHE["ad"] = result["ad"]
    return _AD_CACHE["ad"]


def fetch_kline(code: str, begin: str, end: str, period: str = "min1") -> pd.DataFrame:
    """拉取 [begin, end]（YYYY-MM-DD）的K线数据，period 支持 min1/day 等，返回标准列 DataFrame。"""
    ad_code = code_to_ad(code)
    b = int(begin.replace("-", ""))
    e = int(end.replace("-", ""))
    for attempt in range(2):
        ad = _login()
        base = ad.BaseData()
        calendar = base.get_calendar()
        market = ad.MarketData(calendar)
        period_val = getattr(ad.constant.Period, period).value
        kline = market.query_kline([ad_code], begin_date=b, end_date=e,
                                   period=period_val)
        if kline is not None and ad_code in kline and kline[ad_code] is not None:
            df = kline[ad_code].reset_index(drop=True)
            break
        # 连接可能被顶/失效：清缓存重登重试一次
        _AD_CACHE.clear()
        if attempt == 1:
            raise RuntimeError(
                f"星耀接口未返回 {ad_code} 的数据（可能账号连接被占用或代码无效，"
                f"请检查是否其他会话正在使用星耀）")
    else:
        raise RuntimeError(f"星耀接口未返回 {ad_code} 的数据")
    time_col = next((c for c in ("kline_time", "time") if c in df.columns), None)
    if time_col is None:
        raise ValueError(f"K线返回中未找到时间列：{list(df.columns)}")
    df = df.rename(columns={time_col: "time"})
    for col in STD_COLS:
        if col not in df.columns:
            df[col] = 0
    return df[STD_COLS].copy()


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
