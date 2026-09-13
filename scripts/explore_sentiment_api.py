# -*- coding: utf-8 -*-
"""探查大盘/板块/情绪数据可用性：指数分钟、行业指数、涨跌统计。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import data_fetch  # noqa: E402
from core.ad_silence import suppress_sdk_output  # noqa: E402


def main():
    cred = data_fetch.load_credentials()
    import AmazingData as ad
    with suppress_sdk_output():
        ad.login(username=cred["username"], password=cred["password"],
                 host=cred["host"], port=int(cred["port"]))
    base = ad.BaseData()
    calendar = base.get_calendar()
    market = ad.MarketData(calendar)

    # 1) 指数 1 分钟（上证 000001.SH、创业板指 399006.SZ）
    try:
        k = market.query_kline(["000001.SH", "399006.SZ"], begin_date=20260813,
                               end_date=20260814, period=ad.constant.Period.min1.value)
        for c, d in k.items():
            print(f"[指数1分钟] {c}: {len(d)} 行, 列={list(d.columns)[:5]}, "
                  f"时间 {d['kline_time'].iloc[0]} → {d['kline_time'].iloc[-1]}")
    except Exception as e:
        print("[指数1分钟] 失败:", str(e)[:200])

    # 2) 行业指数日线
    try:
        info = ad.InfoData()
        ind = info.get_industry_daily(["801010.SI"], is_local=False)
        print("[行业指数] 类型:", type(ind))
        if isinstance(ind, dict):
            for c, d in ind.items():
                print(f"  {c}: {len(d)} 行, 列={list(d.columns)[:6]}")
        else:
            print("  ", ind.head(2).to_string())
    except Exception as e:
        print("[行业指数] 失败:", str(e)[:200])

    # 2b) 行业指数代码表（看有哪些行业）
    try:
        codes = info.get_industry_base_info()
        print("[行业列表] 类型:", type(codes))
        if isinstance(codes, list):
            print("  前5个:", codes[:5], "共", len(codes))
        else:
            print("  ", str(codes)[:200])
    except Exception as e:
        print("[行业列表] 失败:", str(e)[:200])

    # 2c) 行业成分股（个股→行业映射用）
    try:
        cons = info.get_industry_constituent(["801010.SI"])
        print("[行业成分] 类型:", type(cons))
        if isinstance(cons, dict):
            for c, d in cons.items():
                print(f"  {c}: {len(d)} 行, 列={list(d.columns)[:6]}")
                print("  ", d.head(2).to_string())
        else:
            print("  ", str(cons)[:300])
    except Exception as e:
        print("[行业成分] 失败:", str(e)[:200])

    # 2d) 个股行业归属（从证券信息拿）
    try:
        basic = info.get_stock_basic(["601619.SH", "688256.SH"])
        print("[个股基础信息] 类型:", type(basic))
        if isinstance(basic, dict):
            for c, d in basic.items():
                print(f"  {c}: 列={list(d.columns)}")
                print("  ", d.head(1).to_string())
        else:
            print("  ", str(basic)[:300])
    except Exception as e:
        print("[个股基础信息] 失败:", str(e)[:200])

    # 3) 探查 SDK 涨跌统计接口
    cands = [m for m in dir(base) if any(x in m.lower() for x in ("up", "down", "limit", "spread"))]
    print("[涨跌统计候选方法]:", cands[:20])
    cands2 = [m for m in dir(ad) if any(x in m.lower() for x in ("up", "down", "limit"))]
    print("[ad模块候选]:", cands2[:20])


if __name__ == "__main__":
    main()
