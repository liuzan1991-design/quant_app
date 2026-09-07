# -*- coding: utf-8 -*-
"""生成 A股 代码+名称+拼音首字母 清单 → quant_app/data/stock_list.csv

优先 akshare 全量（约 5500 只，含名称，可重试多次）；akshare 失败且本地已有带名称
的旧清单时保留旧清单；实在没有才退回 AmazingData 纯代码清单。
运行：python build_stock_list.py
"""
from __future__ import annotations

import sys
import time
import unicodedata
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent / "data" / "stock_list.csv"


def _pinyin_initials(name: str) -> str:
    """名称的拼音首字母，如 寒武纪→hwj、中际旭创→zjxc；去掉"A/Ａ"后缀，失败返回空串。"""
    try:
        from pypinyin import lazy_pinyin
        name = name.replace("Ａ", "").replace("A", "").strip()
        segs = lazy_pinyin(name)
        initials = []
        for s in segs:
            s = unicodedata.normalize("NFKC", s)  # 全角→半角（如 Ａ→A）
            if s and s[0].isalpha():
                initials.append(s[0].lower())
        return "".join(initials)
    except Exception:
        return ""


def build_from_akshare(retries: int = 5) -> pd.DataFrame:
    import akshare as ak
    last = None
    for i in range(retries):
        try:
            df = ak.stock_info_a_code_name()
            df["code"] = df["code"].astype(str).str.zfill(6)
            df["name"] = df["name"].astype(str).str.strip()
            df["pinyin"] = df["name"].map(_pinyin_initials)
            return df[["code", "name", "pinyin"]]
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  akshare 第 {i+1} 次失败，3 秒后重试：{str(e)[:80]}")
            time.sleep(3)
    raise last


def build_from_amazingdata() -> pd.DataFrame:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from core.data_fetch import _login
    ad = _login()
    codes = sorted({c.split(".")[0].zfill(6) for c in ad.BaseData().get_code_list()})
    df = pd.DataFrame({"code": codes})
    df["name"] = ""
    df["pinyin"] = ""
    return df[["code", "name", "pinyin"]]


def _has_names(df: pd.DataFrame) -> bool:
    return "name" in df.columns and df["name"].astype(str).str.len().sum() > 0


if __name__ == "__main__":
    old = pd.read_csv(OUT, dtype={"code": str}) if OUT.exists() else None

    df, src = None, ""
    try:
        df = build_from_akshare()
        src = "akshare"
    except Exception as e:  # noqa: BLE001
        print(f"akshare 全部失败：{str(e)[:100]}")
        if old is not None and _has_names(old):
            df, src = old, "existing（保留旧清单）"
        else:
            df, src = build_from_amazingdata(), "amazingdata（纯代码）"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    df = (df.drop_duplicates(subset="code")
            .sort_values("code").reset_index(drop=True))
    df.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"已写入 {OUT}（来源 {src}，共 {len(df)} 条，{len(df)} 条带名称）" if _has_names(df)
          else f"已写入 {OUT}（来源 {src}，共 {len(df)} 条，无名称）")
    for _, r in df.head(6).iterrows():
        print(f"  {r['code']}  {str(r['name']):<10}  {r['pinyin']}")
