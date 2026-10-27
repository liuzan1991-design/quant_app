# -*- coding: utf-8 -*-
"""涨跌停价计算（H61）—— 让 `risk_manager` 的涨停禁买不再是"死保护"。

## 为什么要有它

`core/risk_manager.py:121` 的「涨停状态禁止按最新价模拟买入」依赖 `MarketSnapshot.limit_up`，
但全仓库只有 **回放**（`paper_replay.py:84`，读 bar 的 `limit_up` 列）和 **压测**
（`stress_execution.py:44`，自算）会填这个字段 —— **live 路径恒为 None** ⇒ 风控规则存在但从未生效。
（§6.9 第 11 形态：机制存在 ≠ 机制被调用。2026-10-27 核出。）

## 分工

- 本模块只做"按交易所规则算价"，不碰风控、不下单；
- live 侧在 `_process_new_bars` 里给 bar 补列，走既有的 `paper_replay` 通道（不新增调用点）；
- 压测侧（`stress_execution`）改为复用本模块，避免两套规则各自漂移（H38 的教训：改一处漏两处）。

## 规则表（2026-10 现状）

| 代码前缀 | 板块 | 涨跌幅 |
|---|---|---|
| `30` / `68` | 创业板 / 科创板 | 20% |
| `4` / `8` | 北交所 | 30% |
| 其余（`60` / `00` 等） | 主板 | 10% |
| 名称含 `ST` | 风险警示 | 5%（覆盖上面） |

## 已知边界（明确标注，不当结论用）

1. **除权除息日**：涨跌停按**除权后**前收计算，本模块用原始前收 ⇒ 除权日的限价会偏（当日边界，隔日自愈）。
2. **新股上市前 5 个交易日无涨跌幅限制**：需调用方传 `unlimited_first_days`；
   本模块无法自行判断"数据首日 = 上市首日"（历史可能被截断），**默认 0 并保留该参数**。
3. 限价按 **0.01 元四舍五入**（`round(x, 2)`）—— 与交易所口径存在 ≤0.005 元的理论偏差，对"是否触及涨停"判定无实质影响。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# 板块涨跌幅上限
_LIMIT_BY_PREFIX = (("30", 0.20), ("68", 0.20), ("4", 0.30), ("8", 0.30))
_LIMIT_DEFAULT = 0.10
_LIMIT_ST = 0.05


def limit_ratio(symbol: str, name: str | None = None) -> float:
    """返回该标的的涨跌幅比例（0.20 = 20%）。`symbol` 可带后缀（300308.SZ）。"""
    code = (symbol or "").strip().split(".")[0]
    if name and "ST" in str(name).upper():
        return _LIMIT_ST
    for prefix, ratio in _LIMIT_BY_PREFIX:
        if code.startswith(prefix):
            return ratio
    return _LIMIT_DEFAULT


def add_limit_columns(df: pd.DataFrame, symbol: str, name: str | None = None,
                      unlimited_first_days: int = 0) -> pd.DataFrame:
    """给分钟/日线 DataFrame 补 `limit_up` / `limit_down` 两列（按前收 × 比例）。

    - 前收取**上一交易日的最后一根 close**（同一 `time` 列，按自然日分组）；
    - 数据首日无前收 ⇒ 两列 NaN（`paper_replay` 会当 None 处理，即当轮不启涨停保护）；
    - `unlimited_first_days=N` ⇒ 前 N 个交易日也置 NaN（次新股上市初期无涨跌幅限制）。
    """
    if df is None or len(df) == 0:
        return df
    out = df.copy()
    if "time" not in out.columns:
        raise ValueError("add_limit_columns 需要 time 列")
    out["time"] = pd.to_datetime(out["time"])
    days = out["time"].dt.normalize()

    daily_last = out.groupby(days)["close"].last()
    prev_close = daily_last.shift(1)
    mapping = days.map(prev_close)

    ratio = limit_ratio(symbol, name)
    out["limit_up"] = (mapping * (1.0 + ratio)).round(2)
    out["limit_down"] = (mapping * (1.0 - ratio)).round(2)
    # 首日（无前收）本身已是 NaN；显式写一遍，避免 round 把 NaN 变成别的
    out.loc[mapping.isna(), ["limit_up", "limit_down"]] = np.nan

    if unlimited_first_days and unlimited_first_days > 0:
        first_days = list(daily_last.index)[: int(unlimited_first_days)]
        mask = days.isin(first_days)
        out.loc[mask, ["limit_up", "limit_down"]] = np.nan
    return out
