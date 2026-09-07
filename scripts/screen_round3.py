# -*- coding: utf-8 -*-
"""第三轮筛选：深跌潜力股（地产链/软件/次新/光伏/消费），后复权口径。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.data_fetch import code_to_ad  # noqa: E402
from core.ad_silence import suppress_sdk_output  # noqa: E402

OUT_CSV = Path(r"D:\Codex输出\股票筛选_第三轮.csv")
CACHE_DIR = Path(r"D:\Codex输出\ad_cache")

CODES = [
    "002791", "603737", "300737", "002572", "603816", "000002", "600048",
    "600383", "601155", "600340", "002146", "002410", "600588", "300454",
    "002439", "600536", "300369", "603285", "603312", "002081", "002612",
    "603983", "688276", "300244", "688331", "300347", "688202", "688169",
    "603486", "601699", "601857", "600938", "601225", "002129", "601012",
    "600438", "688223", "300274", "688390", "603185", "002709", "300390",
    "688063", "300207", "688032", "603806", "688598", "688556", "300316",
]

NAME_MAP = {}
LIST_CSV = Path(__file__).resolve().parents[1] / "data" / "stock_list.csv"
if LIST_CSV.exists():
    NAME_MAP = dict(zip(pd.read_csv(LIST_CSV, dtype={"code": str})["code"],
                        pd.read_csv(LIST_CSV, dtype={"code": str})["name"]))

BASE_DATE = pd.Timestamp("2024-08-30")
END_DATE = pd.Timestamp("2025-08-29")


def main() -> None:
    cred = json.loads(Path(r"D:\Codex输出\ad_credentials.json").read_text(encoding="utf-8"))
    import AmazingData as ad
    with suppress_sdk_output():
        ad.login(username=cred["username"], password=cred["password"],
                 host=cred["host"], port=int(cred["port"]))
    base = ad.BaseData()
    calendar = base.get_calendar()
    mkt = ad.MarketData(calendar)

    ad_codes = [code_to_ad(c) for c in CODES]
    print(f"共 {len(ad_codes)} 只，拉日线与复权因子…")
    kline = mkt.query_kline(ad_codes, begin_date=20240801, end_date=20260821,
                            period=ad.constant.Period.day.value)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    factors = base.get_backward_factor(ad_codes, local_path=str(CACHE_DIR),
                                       is_local=False)

    rows = []
    for code6, ad_code in zip(CODES, ad_codes):
        df = kline.get(ad_code)
        if df is None or not len(df):
            rows.append({"代码": code6, "名称": NAME_MAP.get(code6, ""), "无数据": True})
            continue
        df = df.reset_index()
        time_col = next((c for c in ("kline_time", "time", "datetime", "index")
                         if c in df.columns), None)
        if time_col is None:
            rows.append({"代码": code6, "名称": NAME_MAP.get(code6, ""), "无数据": True})
            continue
        df = df.rename(columns={time_col: "time"})
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").drop_duplicates("time")
        df = df[(df["time"] >= BASE_DATE) & (df["time"] <= "2026-08-21")]
        if not len(df):
            rows.append({"代码": code6, "名称": NAME_MAP.get(code6, ""), "无数据": True})
            continue
        base_row = df[df["time"] <= BASE_DATE]
        if not len(base_row):
            rows.append({"代码": code6, "名称": NAME_MAP.get(code6, ""), "无数据": True})
            continue

        f = factors[ad_code] if isinstance(factors, dict) else factors[ad_code]
        f = pd.Series(f.dropna(), index=pd.to_datetime(f.dropna().index))
        f = f.reindex(df["time"]).ffill().bfill().values
        close = df["close"].astype(float).values
        high = df["high"].astype(float).values
        adj_close = close * f
        adj_high = high * f

        idx_base = len(base_row) - 1
        end_rows = df[df["time"] <= END_DATE]
        idx_end = len(end_rows) - 1
        b = adj_close[idx_base]
        e = adj_close[idx_end]
        now = adj_close[-1]
        peak = adj_high[idx_base + 1: idx_end + 1].max()
        rows.append({
            "代码": code6,
            "名称": NAME_MAP.get(code6, ""),
            "无数据": False,
            "窗口涨幅%": round((e / b - 1) * 100, 1),
            "峰值涨幅%": round((peak / b - 1) * 100, 1),
            "现价涨幅%": round((now / b - 1) * 100, 1),
            "自峰值回落%": round((now / peak - 1) * 100, 1),
            "基准价": round(float(close[idx_base]), 2),
            "现价": round(float(close[-1]), 2),
            "最新日期": df.iloc[-1]["time"].strftime("%Y-%m-%d"),
        })

    res = pd.DataFrame(rows)
    res.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(res.to_string(index=False))
    print(f"\n已保存：{OUT_CSV}")


if __name__ == "__main__":
    main()
