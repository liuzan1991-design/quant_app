# -*- coding: utf-8 -*-
"""验证星耀数智增量拉取链路（小范围）。"""
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from core import data_fetch  # noqa: E402
from core.ad_silence import suppress_sdk_output  # noqa: E402


def main():
    import AmazingData as ad
    cred = data_fetch.load_credentials()
    with suppress_sdk_output():
        ad.login(username=cred["username"], password=cred["password"],
                 host=cred["host"], port=int(cred["port"]))
    base = ad.BaseData()
    calendar = base.get_calendar()
    market = ad.MarketData(calendar)
    raw = market.query_kline(["601619.SH"], begin_date=20260811, end_date=20260813,
                             period=ad.constant.Period.min1.value)
    kdf = raw["601619.SH"]
    print("原始 index:", type(kdf.index), kdf.index[:3].tolist())
    print("原始列:", list(kdf.columns))
    print("原始前2行:")
    print(kdf.head(2).to_string())

    df = data_fetch.fetch_kline("601619", "2026-08-11", "2026-08-13")
    print("拉取行数:", len(df))
    print("列:", list(df.columns))
    if len(df):
        print("区间:", df["time"].iloc[0], "→", df["time"].iloc[-1])
        print(df.head(2).to_string(index=False))
        assert {"time", "open", "high", "low", "close", "volume", "amount"} <= set(df.columns)
    print("星耀数据拉取验证通过")

    # 探查复权因子结构
    with suppress_sdk_output():
        ad.login(username=cred["username"], password=cred["password"],
                 host=cred["host"], port=int(cred["port"]))
    base2 = ad.BaseData()
    fac = base2.get_backward_factor(["601619.SH"])
    fdf = fac["601619.SH"]
    print("复权因子列:", list(fdf.columns))
    print(fdf.head(3).to_string())
    print(fdf.tail(3).to_string())


if __name__ == "__main__":
    main()
