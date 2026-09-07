# -*- coding: utf-8 -*-
"""拉取 301717 财报三表（对照 300308），探测新股财报数据可用性。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.ad_silence import suppress_sdk_output  # noqa: E402

CRED = json.loads(Path(r"D:\Codex输出\ad_credentials.json").read_text(encoding="utf-8"))
CACHE = Path(r"D:\Codex输出\ad_cache")


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

    for code in ["301717.SZ", "300308.SZ"]:
        print(f"\n===== {code} =====")
        for name, fn in [("利润表", info.get_income),
                         ("资产负债表", info.get_balance_sheet),
                         ("现金流量表", info.get_cash_flow)]:
            try:
                d = fn([code], local_path=lp, is_local=False,
                       begin_date=20240101, end_date=20260826)
                df = d.get(code)
                if df is None or not len(df):
                    print(f"{name}: 空")
                    continue
                keep = [c for c in df.columns if c in (
                    "REPORTING_PERIOD", "ANN_DATE", "STATEMENT_TYPE",
                    "OPERA_REV", "TOT_OPERA_REV", "NET_PRO_EXCL_MIN_INT_INC",
                    "NET_PRO_INCL_MIN_INT_INC", "NET_PRO_AFTER_DED_NR_GL",
                    "LESS_OPERA_COST", "LESS_SELLING_EXP", "LESS_ADMIN_EXP",
                    "RD_EXP", "LESS_FIN_EXP", "OPERA_PROFIT", "TOTAL_PROFIT",
                    "INCOME_TAX", "OTH_INCOME", "PLUS_NON_OPERA_REV",
                    "LESS_NON_OPERA_EXP", "BASIC_EPS", "TOTAL_ASSETS",
                    "TOTAL_LIAB", "TOT_SHARE_EQUITY_EXCL_MIN_INT",
                    "CURRENCY_CAP", "ACCT_RECEIVABLE", "INV", "TOTAL_CUR_ASSETS",
                    "TOTAL_CUR_LIAB", "TOTAL_NONCUR_LIAB", "GOODWILL",
                    "NET_CASH_FLOWS_OPERA_ACT", "NET_CASH_FLOWS_INV_ACT",
                    "NET_CASH_FLOWS_FIN_ACT", "END_BAL_CASH_CASH_EQU",
                    "FREE_CASH_FLOW", "CASH_RECP_SG_AND_RS",
                )]
                df2 = df[keep].sort_values("REPORTING_PERIOD")
                with pd.option_context("display.max_columns", None,
                                       "display.width", 250):
                    print(name)
                    print(df2.tail(8).to_string(index=False))
            except Exception as e:  # noqa: BLE001
                print(f"{name}: 失败 -> {type(e).__name__}: {e}")

        try:
            kl = mkt.query_kline([code], begin_date=20260810, end_date=20260826,
                                 period=ad.constant.Period.day.value)
            kdf = kl.get(code)
            if kdf is not None and len(kdf):
                kdf = kdf.reset_index()
                tc = next((c for c in ("kline_time", "time", "datetime", "index")
                           if c in kdf.columns), None)
                if tc:
                    kdf = kdf.rename(columns={tc: "time"})
                print("日线(近期):")
                print(kdf[["time", "open", "high", "low", "close", "volume"]].tail(12).to_string(index=False))
            else:
                print("日线: 空")
        except Exception as e:  # noqa: BLE001
            print(f"日线: 失败 -> {e}")


if __name__ == "__main__":
    main()
