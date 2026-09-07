# -*- coding: utf-8 -*-
"""探查行业成分结构，确认个股→行业映射可行。"""
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
    info = ad.InfoData()
    base = info.get_industry_base_info()
    print("行业表列:", list(base.columns))
    # 只看一级行业（LEVEL_TYPE==1）
    l1 = base[base["LEVEL_TYPE"] == 1] if "LEVEL_TYPE" in base.columns else base
    print("一级行业数:", len(l1))
    print(l1[["INDEX_CODE", "INDUSTRY_CODE", "LEVEL1_NAME"]].head(8).to_string()
          if "LEVEL1_NAME" in l1.columns else l1.head(8).to_string())
    codes = l1["INDEX_CODE"].tolist()[:3]
    for kwargs in ({}, {"is_local": False}):
        try:
            cons = info.get_industry_constituent(codes, **kwargs)
            print(f"行业成分(kwargs={kwargs}) 类型:", type(cons), "长度:", len(cons) if cons else 0)
            if cons:
                for c, d in cons.items():
                    print(f"  成分[{c}]: {len(d)} 行, 列={list(d.columns)}")
                    print(d.head(3).to_string())
                    break
            break
        except Exception as e:
            print(f"行业成分(kwargs={kwargs}) 失败:", str(e)[:150])

    try:
        cons2 = info.get_index_constituent(["000300.SH"])
        print("指数成分 类型:", type(cons2), "长度:", len(cons2) if cons2 else 0)
        if cons2:
            for c, d in cons2.items():
                print(f"  [{c}]: {len(d)} 行, 列={list(d.columns)}")
                print(d.head(3).to_string())
                break
    except Exception as e:
        print("指数成分 失败:", str(e)[:150])


if __name__ == "__main__":
    main()
