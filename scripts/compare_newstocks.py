# -*- coding: utf-8 -*-
"""横向对比次新股：超纯应材 301717 vs 国仪量子 688828 vs 频准激光 688826。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.ad_silence import suppress_sdk_output  # noqa: E402

CRED = json.loads(Path(r"D:\Codex输出\ad_credentials.json").read_text(encoding="utf-8"))
CACHE = Path(r"D:\Codex输出\ad_cache")

CODES = ["301717.SZ", "688828.SH", "688826.SH"]
ISSUE_PRICE = {"301717.SZ": 65.99, "688828.SH": 21.22, "688826.SH": 186.88}


def fetch_with_retry(fn, code, lp, tries=3):
    for i in range(tries):
        try:
            return fn([code], local_path=lp, is_local=False,
                      begin_date=20240101, end_date=20260826)
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
    return None


def main() -> None:
    import AmazingData as ad
    with suppress_sdk_output():
        ad.login(username=CRED["username"], password=CRED["password"],
                 host=CRED["host"], port=int(CRED["port"]))
    info = ad.InfoData()
    base = ad.BaseData()
    calendar = base.get_calendar()
    mkt = ad.MarketData(calendar)
    CACHE.mkdir(parents=True, exist_ok=True)
    lp = str(CACHE)

    for code in CODES:
        print(f"\n########## {code} ##########")
        inc = fetch_with_retry(info.get_income, code, lp)
        if inc:
            df = inc.get(code)
            if df is not None and len(df):
                df = df[df["STATEMENT_TYPE"].astype(str) == "1"].sort_values("REPORTING_PERIOD")
                cols = [c for c in [
                    "REPORTING_PERIOD", "ANN_DATE", "OPERA_REV", "LESS_OPERA_COST",
                    "LESS_ADMIN_EXP", "RD_EXP", "OPERA_PROFIT", "TOTAL_PROFIT",
                    "INCOME_TAX", "NET_PRO_EXCL_MIN_INT_INC", "NET_PRO_AFTER_DED_NR_GL",
                    "OTH_INCOME", "BASIC_EPS",
                ] if c in df.columns]
                print("--- 利润表(合并) ---")
                print(df[cols].to_string(index=False))
        try:
            bs = info.get_balance_sheet([code], local_path=lp, is_local=False,
                                        begin_date=20250101, end_date=20260826)
            bdf = bs.get(code)
            if bdf is not None and len(bdf):
                bdf = bdf[bdf["STATEMENT_TYPE"].astype(str) == "1"].sort_values("REPORTING_PERIOD")
                bcols = [c for c in [
                    "REPORTING_PERIOD", "TOTAL_ASSETS", "TOTAL_LIAB",
                    "TOT_SHARE_EQUITY_EXCL_MIN_INT", "CURRENCY_CAP", "ACCT_RECEIVABLE",
                    "TOT_SHARE",
                ] if c in bdf.columns]
                print("--- 资产负债表(合并,近2期) ---")
                print(bdf[bcols].tail(2).to_string(index=False))
        except Exception as e:  # noqa: BLE001
            print("资产负债表失败:", e)
        try:
            cf = info.get_cash_flow([code], local_path=lp, is_local=False,
                                    begin_date=20250101, end_date=20260826)
            cdf = cf.get(code)
            if cdf is not None and len(cdf):
                cdf = cdf[cdf["STATEMENT_TYPE"].astype(str) == "1"].sort_values("REPORTING_PERIOD")
                ccols = [c for c in [
                    "REPORTING_PERIOD", "NET_CASH_FLOWS_OPERA_ACT",
                    "NET_CASH_FLOWS_INV_ACT", "NET_CASH_FLOWS_FIN_ACT",
                    "END_BAL_CASH_CASH_EQU",
                ] if c in cdf.columns]
                print("--- 现金流量表(合并,近2期) ---")
                print(cdf[ccols].tail(2).to_string(index=False))
        except Exception as e:  # noqa: BLE001
            print("现金流量表失败:", e)
        try:
            eq = info.get_equity_structure([code], local_path=lp, is_local=False)
            edf = eq.get(code)
            if edf is not None and len(edf):
                cols = [c for c in ["ANN_DATE", "CHANGE_DATE", "TOT_SHARE", "FLOAT_SHARE"]
                        if c in edf.columns]
                print("--- 股本(最新) ---")
                print(edf[cols].head(2).to_string(index=False))
        except Exception as e:  # noqa: BLE001
            print("股本失败:", e)
        try:
            kl = mkt.query_kline([code], begin_date=20260801, end_date=20260826,
                                 period=ad.constant.Period.day.value)
            kdf = kl.get(code)
            if kdf is not None and len(kdf):
                kdf = kdf.reset_index()
                tc = next((c for c in ("kline_time", "time", "datetime", "index")
                           if c in kdf.columns), None)
                if tc:
                    kdf = kdf.rename(columns={tc: "time"})
                print("--- 日线(上市以来) ---")
                print(kdf[["time", "open", "high", "low", "close", "volume"]].to_string(index=False))
        except Exception as e:  # noqa: BLE001
            print("日线失败:", e)


if __name__ == "__main__":
    main()
