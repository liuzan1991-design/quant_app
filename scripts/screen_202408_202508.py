# -*- coding: utf-8 -*-
"""筛选 2024-08 → 2025-08 三类股票：仍维持 >100% / 冲高回落 / 跌幅 >50%。
用星耀数智官方日线（不复权）验证，输出 CSV 到 D:\\Codex输出。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.data_fetch import code_to_ad  # noqa: E402
from core.ad_silence import suppress_sdk_output  # noqa: E402

OUT_CSV = Path(r"D:\Codex输出\股票筛选_202408-202508.csv")
CACHE_DIR = Path(r"D:\Codex输出\ad_cache")

# 候选池：A=AI/半导体/券商(预期仍高位) B=主题冲高回落 C=大跌
CANDIDATES = {
    "A": ["688256", "300502", "300394", "300308", "300476", "002463",
          "688041", "688981", "603986", "688018", "688608", "300604",
          "002371", "300059", "300033", "601138", "000977", "603019",
          "300474", "688008", "300433", "002241", "601127", "300750",
          "002594", "000333"],
    "B": ["300251", "000158", "002261", "600839", "002456", "300085",
          "300377", "300803", "601162", "601099", "601727", "002583",
          "300045", "300484", "600580", "300024", "002747", "688017",
          "603728", "002896", "300073", "300207", "603083", "300364",
          "002292", "300654", "688165", "002527"],
    "C": ["300093", "300716", "688680", "600732", "688185", "300142",
          "603882", "601888", "600702", "000799", "600779", "603833",
          "002271", "300957", "603605", "002310", "600606", "300125",
          "688429", "300345", "002607", "300015", "600763", "688363",
          "000661", "300676", "688180", "002822",
          # 新增：ST/暴雷/光伏深跌/高送转
          "300630", "603377", "002750", "600381", "000506", "300108",
          "603608", "603363", "002309", "300020", "000628", "603185",
          "688063", "688717", "688390", "688598", "688556", "688032",
          "603669", "000793", "600811", "601828", "002594", "300280"],
}

NAME_MAP = {}
LIST_CSV = Path(__file__).resolve().parents[1] / "data" / "stock_list.csv"
if LIST_CSV.exists():
    df_list = pd.read_csv(LIST_CSV, dtype={"code": str})
    NAME_MAP = dict(zip(df_list["code"], df_list["name"]))

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

    all_codes = sorted({c for v in CANDIDATES.values() for c in v})
    ad_codes = [code_to_ad(c) for c in all_codes]
    print(f"共 {len(ad_codes)} 只候选股，开始拉日线与复权因子…")
    kline = mkt.query_kline(ad_codes, begin_date=20240801, end_date=20260821,
                            period=ad.constant.Period.day.value)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        factors = base.get_backward_factor(ad_codes, local_path=str(CACHE_DIR),
                                           is_local=False)
    except Exception as e:  # noqa: BLE001
        print(f"复权因子获取失败（用不复权计算）：{e}")
        factors = None

    rows = []
    for code6, ad_code in zip(all_codes, ad_codes):
        df = kline.get(ad_code)
        if df is None or not len(df):
            rows.append({"代码": code6, "名称": NAME_MAP.get(code6, ""), "类别": "",
                         "无数据": True})
            continue
        df = df.reset_index()
        time_col = next((c for c in ("kline_time", "time", "datetime", "index")
                         if c in df.columns), None)
        if time_col is None:
            rows.append({"代码": code6, "名称": NAME_MAP.get(code6, ""), "类别": "",
                         "无数据": True})
            continue
        df = df.rename(columns={time_col: "time"})
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").drop_duplicates("time")
        df = df[(df["time"] >= BASE_DATE) & (df["time"] <= "2026-08-21")]
        if not len(df):
            rows.append({"代码": code6, "名称": NAME_MAP.get(code6, ""), "类别": "",
                         "无数据": True})
            continue

        base_row = df[df["time"] <= BASE_DATE]
        end_row = df[df["time"] <= END_DATE]
        if not len(base_row) or not len(end_row):
            rows.append({"代码": code6, "名称": NAME_MAP.get(code6, ""), "类别": "",
                         "无数据": True})
            continue

        # 后复权因子对齐
        f_series = None
        if factors is not None:
            try:
                f = factors[ad_code] if isinstance(factors, dict) else factors[ad_code]
                f = pd.Series(f.dropna(), index=pd.to_datetime(f.dropna().index))
                f = f.reindex(df["time"]).ffill().bfill()
                f_series = f.values
            except Exception:  # noqa: BLE001
                f_series = None

        close = df["close"].astype(float).values
        high = df["high"].astype(float).values
        if f_series is not None:
            adj_close = close * f_series
            adj_high = high * f_series
            factor_jump = float(f_series.max() / f_series.min())
        else:
            adj_close = close
            adj_high = high
            factor_jump = 1.0

        idx_base = len(base_row) - 1
        idx_end = len(base_row) + len(df[(df["time"] > BASE_DATE) & (df["time"] <= END_DATE)]) - 1
        base_adj = float(adj_close[idx_base])
        end_adj = float(adj_close[idx_end])
        now_adj = float(adj_close[-1])
        win_high = adj_high[idx_base + 1: idx_end + 1]
        peak_adj = float(win_high.max()) if len(win_high) else base_adj
        rows.append({
            "代码": code6,
            "名称": NAME_MAP.get(code6, ""),
            "类别": "",
            "无数据": False,
            "窗口涨幅%(复权)": round((end_adj / base_adj - 1) * 100, 1),
            "窗口峰值涨幅%(复权)": round((peak_adj / base_adj - 1) * 100, 1),
            "现价涨幅%(复权)": round((now_adj / base_adj - 1) * 100, 1),
            "窗口内自峰值回落%": round((end_adj / peak_adj - 1) * 100, 1),
            "复权因子倍数": round(factor_jump, 2),
            "基准价(不复权)": round(float(close[idx_base]), 2),
            "现价(最新)": round(float(close[-1]), 2),
            "最新日期": df.iloc[-1]["time"].strftime("%Y-%m-%d"),
        })

    res = pd.DataFrame(rows)
    res.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(res.to_string(index=False))
    print(f"\n结果已保存：{OUT_CSV}")


if __name__ == "__main__":
    main()
