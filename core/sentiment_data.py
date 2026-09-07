# -*- coding: utf-8 -*-
"""大盘/板块情绪数据：指数1分钟、行业指数日线、个股行业映射（增量缓存）。"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from core import data_fetch

APP_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = APP_DIR / "data"

INDEX_POOL = {
    "000001.SH": "上证指数",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
}

# 预置股票池个股 → 申万一级行业名（行业成分接口暂不可用，用内置映射）
STOCK_INDUSTRY = {
    "688256": "电子", "300308": "通信", "300502": "通信",
    "300418": "传媒", "300364": "传媒", "300319": "电子",
    "601619": "公用事业", "000572": "汽车", "002371": "电子",
    "300122": "医药生物", "600900": "公用事业", "002594": "汽车",
}


def _fetch_and_cache(kind: str, name: str, ad_code: str, begin: str, end: str,
                     period, cols: list) -> pd.DataFrame:
    """通用增量缓存：已有数据只补缺口，返回合并后的 DataFrame。"""
    target = DATA_DIR / f"{kind}_{name}.csv"
    existing = None
    if target.exists():
        existing = pd.read_csv(target, parse_dates=["time"])
    begin_dt = pd.Timestamp(begin)
    end_dt = pd.Timestamp(end)
    if existing is not None and len(existing):
        first = existing["time"].min()
        last = existing["time"].max()
        in_range = ((existing["time"] >= begin_dt) &
                    (existing["time"] <= end_dt.replace(hour=15, minute=0))).sum()
        if (first <= begin_dt + pd.Timedelta(days=5)
                and last >= end_dt.replace(hour=15, minute=0)
                and in_range > 0):
            return existing
        if end_dt < first:
            begin_dt = pd.Timestamp(begin)
        elif begin_dt > last:
            begin_dt = last - pd.Timedelta(days=7)
        else:
            begin_dt = pd.Timestamp(begin)
    new = data_fetch.fetch_kline(ad_code, begin_dt.strftime("%Y-%m-%d"),
                                 end_dt.strftime("%Y-%m-%d"), period=period)
    new = new[cols]
    if existing is not None and len(existing):
        merged = pd.concat([existing, new], ignore_index=True)
    else:
        merged = new
    merged = merged.drop_duplicates(subset="time", keep="last").sort_values("time").reset_index(drop=True)
    merged.to_csv(target, index=False, encoding="utf-8-sig")
    return merged


def load_index_min(index_code: str, start: str, end: str) -> pd.DataFrame:
    """指数 1 分钟数据（增量缓存）。index_code 如 399006.SZ。"""
    return _fetch_and_cache("index", index_code.replace(".", "_"), index_code,
                            start, end, "min1",
                            ["time", "open", "high", "low", "close", "volume", "amount"])


def compute_market_status(index_df: pd.DataFrame) -> Tuple[Dict, Dict]:
    """把指数1分钟转成大盘状态：返回 (status_map, chg_map)，key 为 bar 时间。
    status: strong/weak/crash/flat；chg: 指数当日涨跌幅(%)。"""
    if not len(index_df):
        return {}, {}
    d = index_df.copy()
    d["date"] = pd.to_datetime(d["time"]).dt.date
    open_d = d.groupby("date")["open"].transform("first")
    chg = (d["close"] - open_d) / open_d * 100.0
    cum_amt = d.groupby("date")["amount"].cumsum()
    cum_vol = d.groupby("date")["volume"].cumsum()
    vwap = cum_amt / cum_vol.replace(0, np.nan)
    dev = (d["close"] - vwap) / vwap * 100.0
    status_map, chg_map = {}, {}
    for ts, c, dv in zip(d["time"], chg, dev):
        if np.isnan(c):
            c = 0.0
        if np.isnan(dv):
            dv = 0.0
        if c <= -0.8:
            st = "crash"
        elif c >= 0.3 and dv > 0:
            st = "strong"
        elif c <= -0.2 or dv <= -0.25:
            st = "weak"
        else:
            st = "flat"
        status_map[ts] = st
        chg_map[ts] = c
    return status_map, chg_map


def load_industry_level1() -> Dict[str, str]:
    """申万一级行业：行业名 → 指数代码。缓存 industry_level1.csv。"""
    target = DATA_DIR / "industry_level1.csv"
    if target.exists():
        df = pd.read_csv(target)
        return dict(zip(df["NAME"], df["INDEX_CODE"]))
    ad = data_fetch._login()
    base = ad.BaseData()
    info = ad.InfoData()
    _ = base.get_calendar()
    df = info.get_industry_base_info()
    l1 = df[df["LEVEL_TYPE"] == 1][["INDEX_CODE", "LEVEL1_NAME"]].drop_duplicates()
    l1.columns = ["INDEX_CODE", "NAME"]
    l1.to_csv(target, index=False, encoding="utf-8-sig")
    return dict(zip(l1["NAME"], l1["INDEX_CODE"]))


def get_stock_industry_index(code: str) -> Optional[str]:
    """个股 → 行业指数代码（内置映射 + 行业表）。"""
    name = STOCK_INDUSTRY.get(code)
    if not name:
        return None
    return load_industry_level1().get(name)


def load_industry_daily(index_code: str, start: str, end: str) -> pd.DataFrame:
    """行业指数日线（增量缓存）。"""
    target = DATA_DIR / f"industry_{index_code.replace('.', '_')}_daily.csv"
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
            return existing
        if end_dt < first:
            begin_dt = pd.Timestamp(start)
        elif begin_dt > last:
            begin_dt = last - pd.Timedelta(days=30)
        else:
            begin_dt = pd.Timestamp(start)

    ad = data_fetch._login()
    info = ad.InfoData()
    raw = info.get_industry_daily([index_code], is_local=False)
    d = raw[index_code].reset_index()
    time_col = "time" if "time" in d.columns else d.columns[0]
    d = d.rename(columns={time_col: "time"})
    d["time"] = pd.to_datetime(d["time"])
    d = d.rename(columns={c: c.lower() for c in d.columns})
    keep = [c for c in ("time", "open", "high", "low", "close", "volume", "amount") if c in d.columns]
    d = d[keep]
    d = d[(d["time"] >= begin_dt) & (d["time"] <= end_dt)]
    if existing is not None and len(existing):
        merged = pd.concat([existing, d], ignore_index=True)
    else:
        merged = d
    merged = merged.drop_duplicates(subset="time", keep="last").sort_values("time").reset_index(drop=True)
    merged.to_csv(target, index=False, encoding="utf-8-sig")
    return merged


def compute_sector_status(daily_df: pd.DataFrame) -> str:
    """板块强弱：5日/20日均线 + 当日涨跌。返回 strong/weak/flat。"""
    if not len(daily_df):
        return "flat"
    close = daily_df["close"].astype(float)
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    trend_up = ma5.iloc[-1] > ma20.iloc[-1] if len(close) >= 20 else False
    day_chg = close.pct_change().iloc[-1] * 100 if len(close) >= 2 else 0.0
    if (trend_up and day_chg > 0.1) or day_chg > 0.5:
        return "strong"
    if ((not trend_up) and day_chg < -0.1) or day_chg < -0.5:
        return "weak"
    return "flat"


def compute_trend_mode(df: pd.DataFrame, ma_fast: int = 5, ma_slow: int = 10,
                       ma_trend: int = 20) -> Dict:
    """个股日线趋势模式：hold=多头排列（持股防卖飞）、defense=空头排列（禁买）、
    range=震荡（正常做T）。返回 {日期: 模式}。"""
    d = df.copy()
    d["date"] = pd.to_datetime(d["time"]).dt.date
    close_col = "signal_close" if "signal_close" in d.columns else "close"
    daily = d.groupby("date")[close_col].last()
    ma5 = daily.rolling(ma_fast).mean()
    ma10 = daily.rolling(ma_slow).mean()
    ma20 = daily.rolling(ma_trend).mean()
    mode = {}
    prev = None
    for dt in daily.index:
        if pd.isna(ma20[dt]) or pd.isna(ma5[dt]) or pd.isna(ma10[dt]):
            prev = ma20[dt]
            mode[dt] = "range"
        elif (daily[dt] > ma20[dt] and ma5[dt] > ma10[dt] > ma20[dt]
              and (prev is not None and ma20[dt] > prev)):
            mode[dt] = "hold"
        elif (daily[dt] < ma20[dt] and ma5[dt] < ma10[dt] < ma20[dt]
              and (prev is not None and ma20[dt] < prev)):
            mode[dt] = "defense"
        else:
            mode[dt] = "range"
        prev = ma20[dt]
    return mode
