# -*- coding: utf-8 -*-
"""同赛道老票估值横向对比：半导体零部件/科学仪器量子/激光 三组 vs 三只新股。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.data_fetch import code_to_ad  # noqa: E402
from core.ad_silence import suppress_sdk_output  # noqa: E402

CRED = json.loads(Path(r"D:\Codex输出\ad_credentials.json").read_text(encoding="utf-8"))
CACHE = Path(r"D:\Codex输出\ad_cache")
OUT_CSV = Path(r"D:\Codex输出\同赛道老票估值对比.csv")

# (代码, 名称, 组别)
GROUPS = {
    "半导体零部件": [
        ("002371", "北方华创"), ("688012", "中微公司"), ("688072", "拓荆科技"),
        ("688409", "富创精密"), ("300666", "江丰电子"), ("300260", "新莱应材"),
        ("301611", "珂玛科技"), ("301297", "富乐德"),
    ],
    "科学仪器/量子": [
        ("688027", "国盾量子"), ("688622", "禾信仪器"), ("688600", "皖仪科技"),
        ("300203", "聚光科技"), ("688361", "中科飞测"), ("300567", "精测电子"),
    ],
    "激光": [
        ("300747", "锐科激光"), ("688025", "杰普特"), ("688167", "炬光科技"),
        ("688048", "长光华芯"), ("688188", "柏楚电子"), ("000988", "华工科技"),
        ("002008", "大族激光"), ("300776", "帝尔激光"),
    ],
}

NEW = [
    ("301717", "超纯应材", "半导体零部件", 404.00, 411.46, 2.282, 0.0, 67.2, 42.4),
    ("688828", "国仪量子", "科学仪器/量子", 118.50, 473.0, None, 0.0, 91.5, None),
    ("688826", "频准激光", "激光", 1024.49, 409.8, 1.767, 0.0, 80.0, 42.0),
]


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

    codes = []
    for code, _ in [c for v in GROUPS.values() for c in v]:
        codes.append(code_to_ad(code))

    print("逐只拉取财报三表…")
    inc_d, bs_d, cf_d = {}, {}, {}
    for adc in codes:
        # 利润表/现金流：带日期；资产负债表：不能带日期
        for name, fn, store, use_dates in [
            ("利润表", info.get_income, inc_d, True),
            ("资产负债表", info.get_balance_sheet, bs_d, False),
            ("现金流量表", info.get_cash_flow, cf_d, True),
        ]:
            for i in range(3):
                try:
                    kw = dict(local_path=lp, is_local=False)
                    if use_dates:
                        kw.update(begin_date=20240101, end_date=20260826)
                    d = fn([adc], **kw)
                    df = d.get(adc) if isinstance(d, dict) else d
                    if df is not None and len(df):
                        store[adc] = df
                    break
                except Exception as e:  # noqa: BLE001
                    if i == 2:
                        print(f"  {name}失败 {adc}: {e}")

    eq_d = None
    for i in range(4):
        try:
            eq_d = info.get_equity_structure(codes, local_path=lp, is_local=False)
            break
        except Exception as e:  # noqa: BLE001
            print(f"  eq retry {i}: {e}")

    kl_d = mkt.query_kline(codes, begin_date=20260810, end_date=20260826,
                           period=ad.constant.Period.day.value)
    close_map = {}
    for adc in codes:
        df = kl_d.get(adc)
        if df is not None and len(df):
            close_map[adc] = float(df.iloc[-1]["close"])

    def _pick(d, adc):
        if d is None:
            return None
        if isinstance(d, dict):
            return d.get(adc)
        if isinstance(d, pd.DataFrame) and "MARKET_CODE" in d.columns:
            sub = d[d["MARKET_CODE"] == adc]
            return sub if len(sub) else None
        return None

    # 总股本只用股本结构接口（单位：万股），资产负债表里的 TOT_SHARE 单位不统一，弃用
    share_map = {}
    if eq_d is not None:
        for adc in codes:
            try:
                df = _pick(eq_d, adc)
                if df is None:
                    continue
                df = df.dropna(subset=["TOT_SHARE"]).sort_values("CHANGE_DATE")
                if len(df):
                    share_map[adc] = float(df.iloc[-1]["TOT_SHARE"]) * 10000  # 万股→股
            except Exception:  # noqa: BLE001
                pass
    for adc in codes:
        if adc in share_map:
            print(f"  股本 {adc}: {share_map[adc]/1e4:.0f} 万股 = {share_map[adc]/1e8:.2f} 亿股")

    rows = []
    for grp, items in GROUPS.items():
        for code6, name in items:
            adc = code_to_ad(code6)
            try:
                inc = _pick(inc_d, adc)
                if inc is None:
                    rows.append({"代码": code6, "名称": name, "组别": grp, "备注": "无利润表"})
                    continue
                inc = inc[inc["STATEMENT_TYPE"].astype(str) == "1"].sort_values("REPORTING_PERIOD")
                rp = pd.to_numeric(inc["REPORTING_PERIOD"], errors="coerce")
                h1 = inc[rp == 20260630]
                h1_25 = inc[rp == 20250630]
                fy25 = inc[rp == 20251231]
                h1 = h1.iloc[-1] if len(h1) else None
                h1_25 = h1_25.iloc[-1] if len(h1_25) else None
                fy25 = fy25.iloc[-1] if len(fy25) else None
                if h1 is None:
                    if fy25 is None:
                        rows.append({"代码": code6, "名称": name, "组别": grp,
                                     "备注": "无财报数据"})
                        continue
                    fallback = True
                    rev = fy25["OPERA_REV"]
                    np_ = fy25["NET_PRO_EXCL_MIN_INT_INC"]
                    cost = fy25["LESS_OPERA_COST"]
                    rev25 = np25 = None
                else:
                    fallback = False
                    rev = h1["OPERA_REV"]
                    rev25 = h1_25["OPERA_REV"] if h1_25 is not None else None
                    np_ = h1["NET_PRO_EXCL_MIN_INT_INC"]
                    np25 = h1_25["NET_PRO_EXCL_MIN_INT_INC"] if h1_25 is not None else None
                    cost = h1["LESS_OPERA_COST"]
                gm = (rev - cost) / rev * 100 if rev else None
                nm = np_ / rev * 100 if rev else None
                rev_yoy = (rev / rev25 - 1) * 100 if rev25 else None
                np_yoy = (np_ / np25 - 1) * 100 if np25 is not None and np25 else None

                # TTM
                ttm_rev = ttm_np = None
                if fallback:
                    ttm_rev = rev
                    ttm_np = np_
                elif fy25 is not None and h1_25 is not None:
                    ttm_rev = rev + fy25["OPERA_REV"] - h1_25["OPERA_REV"]
                    ttm_np = np_ + fy25["NET_PRO_EXCL_MIN_INT_INC"] - h1_25["NET_PRO_EXCL_MIN_INT_INC"]

                # 净资产/现金流
                eq = None
                if bs_d is not None:
                    try:
                        bdf = _pick(bs_d, adc)
                        if bdf is None:
                            raise ValueError
                        bdf = bdf[bdf["STATEMENT_TYPE"].astype(str) == "1"].sort_values("REPORTING_PERIOD")
                        if len(bdf):
                            eq = bdf.iloc[-1]["TOT_SHARE_EQUITY_EXCL_MIN_INT"]
                    except Exception:  # noqa: BLE001
                        pass
                ocf = None
                if cf_d is not None:
                    try:
                        cdf = _pick(cf_d, adc)
                        if cdf is None:
                            raise ValueError
                        cdf = cdf[cdf["STATEMENT_TYPE"].astype(str) == "1"].sort_values("REPORTING_PERIOD")
                        if len(cdf):
                            ocf = cdf.iloc[-1]["NET_CASH_FLOWS_OPERA_ACT"]
                    except Exception:  # noqa: BLE001
                        pass

                close = close_map.get(adc)
                shares = share_map.get(adc)
                mcap = close * shares / 1e8 if close and shares else None
                pe = mcap / (ttm_np / 1e8) if mcap and ttm_np else None
                pb = mcap / (eq / 1e8) if mcap and eq else None
                ps = mcap / (ttm_rev / 1e8) if mcap and ttm_rev else None
                roe = np_ / eq * 2 * 100 if eq else None  # H1年化

                rows.append({
                    "代码": code6, "名称": name, "组别": grp,
                    "营收H1(亿)": round(rev / 1e8, 2), "营收同比%": round(rev_yoy, 1) if rev_yoy is not None else None,
                    "归母H1(亿)": round(np_ / 1e8, 2), "归母同比%": round(np_yoy, 1) if np_yoy is not None else None,
                    "毛利率%": round(gm, 1) if gm is not None else None,
                    "净利率%": round(nm, 1) if nm is not None else None,
                    "ROE年化%": round(roe, 1) if roe is not None else None,
                    "经营现金流H1(亿)": round(ocf / 1e8, 2) if ocf is not None else None,
                    "收盘价": close, "总市值(亿)": round(mcap, 1) if mcap else None,
                    "PE-TTM": round(pe, 1) if pe and pe > 0 else (None if pe is None else "亏损"),
                    "PB": round(pb, 1) if pb else None,
                    "PS-TTM": round(ps, 1) if ps else None,
                    "备注": "H1未披露,估值按2025年报" if fallback else "",
                })
            except Exception as e:  # noqa: BLE001
                rows.append({"代码": code6, "名称": name, "组别": grp, "备注": f"失败:{e}"})

    res = pd.DataFrame(rows)
    res.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 300)
    print(res.to_string(index=False))
    print(f"\n已保存：{OUT_CSV}")


if __name__ == "__main__":
    main()
