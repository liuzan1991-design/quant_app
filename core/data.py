# -*- coding: utf-8 -*-
"""本地 1 分钟数据加载：多目录查找 + 区间筛选；全量 A 股清单加载与搜索。"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = APP_DIR.parent

# 数据查找顺序：APP 自带缓存 -> 项目回测数据 -> 旧输出目录 -> 星耀官方数据
DATA_DIRS = [
    APP_DIR / "data",
    PROJECT_DIR / "backtest_data",
    Path(r"D:\Codex输出\backtest_data"),
    PROJECT_DIR / "ad_backtest" / "data",
    Path(r"D:\Codex输出\ad_backtest_data"),
]

# 内置常用池（搜索列表无法加载时的兜底，也用于 stock_name 快速命中）
STOCK_POOL = {
    "688256": "寒武纪", "300308": "中际旭创", "300502": "新易盛",
    "300418": "昆仑万维", "300364": "中文在线", "300319": "麦捷科技",
    "601619": "嘉泽新能",
}

STOCK_LIST_FILE = APP_DIR / "data" / "stock_list.csv"


def _clean_name(name: str) -> str:
    name = unicodedata.normalize("NFKC", str(name))  # 全角→半角（Ａ→A）
    name = re.sub(r"\s+", "", name)                  # 去空白（万  科→万科）
    return name


def _load_universe() -> pd.DataFrame:
    """从 stock_list.csv 加载全量 A 股清单（代码/名称/拼音）；缺失时退回常用池。"""
    try:
        df = pd.read_csv(STOCK_LIST_FILE, dtype={"code": str})
        df["code"] = df["code"].astype(str).str.zfill(6)
        df["name"] = df["name"].fillna("").map(_clean_name)
        df["pinyin"] = df["pinyin"].fillna("").astype(str).str.lower()
        return df.reset_index(drop=True)
    except Exception:  # noqa: BLE001
        return pd.DataFrame(
            [{"code": c, "name": n, "pinyin": ""} for c, n in STOCK_POOL.items()])


_UNIVERSE: Optional[pd.DataFrame] = None


def get_universe() -> pd.DataFrame:
    global _UNIVERSE
    if _UNIVERSE is None:
        _UNIVERSE = _load_universe()
    return _UNIVERSE


def find_data_file(code: str) -> Optional[Path]:
    for d in DATA_DIRS:
        f = d / f"stock_{code}_1m.csv"
        if f.exists():
            return f
    return None


def has_local_data(code: str) -> bool:
    return find_data_file(code) is not None


def local_data_stocks() -> list:
    """本地已有 1 分钟数据的代码（默认建议 / 多股票对比候选）。"""
    seen = set()
    for d in DATA_DIRS:
        if d.exists():
            for f in d.glob("stock_*_1m.csv"):
                seen.add(f.name[len("stock_"):-len("_1m.csv")])
    return sorted(seen)


def stock_name(code: str) -> str:
    if code in STOCK_POOL:
        return STOCK_POOL[code]
    uni = get_universe()
    row = uni[uni["code"] == code]
    if len(row):
        return row.iloc[0]["name"] or code
    return code


PRESET_POOL_FILE = APP_DIR / "data" / "preset_pool.txt"
DEFAULT_PRESET_CODES = ["601619", "688256", "300308", "300502",
                        "300418", "300364", "300319", "000572", "300122"]


def load_preset_pool() -> list:
    """预置股票池：data/preset_pool.txt（每行 '代码' 或 '代码 名称'，# 开头为注释）。
    文件缺失时用内置默认池；返回 [{code, name, has_data}]。"""
    codes, name_map = [], {}
    if PRESET_POOL_FILE.exists():
        for line in PRESET_POOL_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            codes.append(parts[0].zfill(6))
            if len(parts) > 1:
                name_map[parts[0].zfill(6)] = " ".join(parts[1:])
    else:
        codes = list(DEFAULT_PRESET_CODES)
    return [{"code": c, "name": name_map.get(c) or stock_name(c),
             "has_data": has_local_data(c)} for c in codes]


def search_stocks(query: str, limit: int = 30) -> list:
    """按 代码前缀 / 名称 / 拼音首字母 搜索，返回 [{code, name, has_data}]。
    空查询返回本地有数据的股票（优先）再补其他。"""
    uni = get_universe()
    q = str(query).strip().lower()

    if not q:
        have = local_data_stocks()
        rows = [{"code": c, "name": stock_name(c), "has_data": True} for c in have]
        for _, r in uni.iterrows():
            if r["code"] in have:
                continue
            rows.append({"code": r["code"], "name": r["name"], "has_data": False})
            if len(rows) >= limit:
                break
        return rows[:limit]

    code_hit = uni["code"].str.startswith(q)
    name_pref = (uni["name"].str.lower().str.startswith(q)
                 | uni["pinyin"].str.startswith(q))
    name_sub = uni["name"].str.lower().str.contains(q, na=False)
    mask = code_hit | name_pref | name_sub
    hits = uni[mask].copy()
    hits["_pri"] = (code_hit.astype(int) * 10
                    + name_pref.astype(int) * 5
                    + name_sub.astype(int))
    hits = hits.sort_values(["_pri", "code"], ascending=[False, True])

    out = []
    for _, r in hits.head(limit).iterrows():
        out.append({"code": r["code"], "name": r["name"],
                    "has_data": has_local_data(r["code"])})
    return out


def load_data(code: str, start: Optional[str] = None, end: Optional[str] = None) -> Tuple[pd.DataFrame, Optional[Path]]:
    f = find_data_file(code)
    if f is None:
        return pd.DataFrame(), None
    df = pd.read_csv(f, parse_dates=["time"])
    if start:
        df = df[df["time"] >= pd.Timestamp(start)]
    if end:
        df = df[df["time"] <= pd.Timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)]
    df = df.reset_index(drop=True)
    df.attrs["code"] = code
    df.attrs["adjustment"] = "不复权（成交价）"
    return df, f
