# -*- coding: utf-8 -*-
"""从 stockdb 取「日频展示字段」：换手率 / 市值 / PE / PB / 是否ST。

为什么单独一个模块
------------------
`core/stockdb_kline.py` 是**行情源适配器**，走 `STOCKDB_CALIBRATED` 校准闸门，
因为分钟 K 的标签/成交量/复权口径会直接影响策略信号。
本模块只服务于**数据看板展示**，取的是日频元数据（换手率、市值、估值），
**不参与任何信号计算、不进回测、不进实盘** ⇒ 不受校准闸门约束。
这是刻意区分，不是漏掉。

设计约束
--------
1. **可退化**：stockdb 服务没起 / SDK 没装 / 查询报错 ⇒ 返回 None，页面照旧渲染，
   绝不因为"想多显示几列"把整个看板弄崩。
2. **不伪造**：源没有的日期就留空（NaN），不填 0、不前向填充冒充真实值。
3. **字段单位明确**：市值统一换成「亿元」，避免 21 列大数字看不出量级。
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from core.stockdb_kline import _import_rd

# stockdb 日K 字段 → 看板列名（右侧是展示名）
FIELD_MAP = {
    "turnover": "换手率%",
    "total_mv": "总市值(亿)",
    "float_mv": "流通市值(亿)",
    "pe_ttm": "市盈率TTM",
    "pb": "市净率",
}
ST_COL = "是否ST"
DAY_COL = "_day"


def normalize_daily_fields(raw) -> pd.DataFrame:
    """stockdb 日线原始表 → 展示用字段表（按自然日索引）。纯函数，可单测。

    ⚠️ `date` 是**整数** `20261023`：直接 `pd.to_datetime(整数)` 会按 epoch 纳秒
    解成 1970 年**且不报错**（本项目已踩过），所以按位数显式指定格式。
    """
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    df = raw.copy()
    cols = {c.lower(): c for c in df.columns}
    dc = next((cols[k] for k in ("date", "time", "datetime", "day") if k in cols), None)
    if dc is None:
        return pd.DataFrame()

    s = df[dc]
    if pd.api.types.is_numeric_dtype(s):
        v = s.astype("int64").astype(str)
        fmt = "%Y%m%d" if v.str.len().max() == 8 else "%Y%m%d%H%M%S"
        parsed = pd.to_datetime(v, format=fmt, errors="coerce")
    else:
        parsed = pd.to_datetime(s.astype(str).str.replace("-", ""),
                                format="%Y%m%d", errors="coerce")

    out = pd.DataFrame({DAY_COL: parsed.dt.date})
    for src, name in FIELD_MAP.items():
        if src in cols:
            val = pd.to_numeric(df[cols[src]], errors="coerce")
            if "mv" in src:          # 市值：元 → 亿元
                val = val / 1e8
            out[name] = val
    if "is_st" in cols:
        # 布尔列在表格里会渲染成 <NA>，对看板不友好 ⇒ 转成「是 / 否 / 空」
        v = df[cols["is_st"]]
        out[ST_COL] = v.map(
            lambda x: None if pd.isna(x) else ("是" if bool(x) else "否")
        ).astype(object)
    out = out.dropna(subset=[DAY_COL]).drop_duplicates(subset=[DAY_COL], keep="last")
    return out.sort_values(DAY_COL).reset_index(drop=True)


def fetch_daily_fields(code: str, begin: str, end: str) -> pd.DataFrame | None:
    """向 stockdb 取某标的的日频展示字段。

    返回 None 表示**不可用**（服务未起/未安装/查询失败），调用方应原样渲染、不加列。
    返回空 DataFrame 表示**可用但该区间没有数据**（例如库只覆盖近期）。
    """
    try:
        rd = _import_rd()
    except Exception:
        return None
    try:
        raw = rd.get_data(code=code, start=begin.replace("-", ""),
                          end=end.replace("-", ""), frequency="1d",
                          fq=None, as_df=True)
        return normalize_daily_fields(raw)
    except Exception:
        return None


def enrich(table: pd.DataFrame, fields: pd.DataFrame | None,
           time_col: str) -> pd.DataFrame:
    """把日频字段按「自然日」贴到看板表上（分时口径下整天共享同一组值）。

    `fields is None`（源不可用）⇒ 原样返回，**列结构一行不变**。
    """
    if fields is None or fields.empty or table.empty:
        return table
    key = table[time_col].astype(str).str[:8]
    day = pd.to_datetime(key, format="%Y%m%d", errors="coerce").dt.date
    m = fields.copy()
    m[DAY_COL] = pd.to_datetime(m[DAY_COL].astype(str)).dt.date
    out = table.copy()
    out[DAY_COL] = day
    out = out.merge(m, on=DAY_COL, how="left")
    return out.drop(columns=[DAY_COL])


def coverage_note(fields: pd.DataFrame | None, days) -> str:
    """给界面用的一句话：数据从哪来、覆盖多少天、没覆盖的怎么显示。

    ⚠️ 两侧日期格式不同：`fields[DAY_COL]` 是 `date` 对象，而看板传进来的是
    `"20261023"` 这类 8 位字符串 ⇒ 必须统一成 `date` 再比，否则永远判"无匹配"。
    """
    if fields is None:
        return "市值/换手率/估值：stockdb 不可用，已隐藏这些列（不影响其它列）。"
    if fields.empty:
        return "市值/换手率/估值：stockdb 已连接，但该区间无数据，相关列留空。"

    have = set(fields[DAY_COL])
    keys = set()
    for x in days:
        d = pd.to_datetime(str(x)[:8], format="%Y%m%d", errors="coerce")
        if pd.notna(d):
            keys.add(d.date())
    total = max(1, len(keys))
    hit = len(keys & have)
    if hit == 0:
        return "市值/换手率/估值：stockdb 该区间无匹配日期，相关列留空。"
    return (f"市值/换手率/估值来自 stockdb：覆盖 {hit}/{total} 个交易日，"
            f"未覆盖的行留空（不填充）。")
