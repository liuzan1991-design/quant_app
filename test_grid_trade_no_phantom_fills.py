# -*- coding: utf-8 -*-
"""H17 回归：grid_trade 不得产生"未真正成交的幻影记录"。

缺陷背景（2026-09-18 登记 H17）
------------------------------
`SimAccount.sell()` 第一行是 `shares = min(shares, self.closeable)`；T+1 下
`closeable == 0` 时**直接 `return 0`** —— 既不成交，**也不更新**
`last_fill_price` / `last_fee` / `last_t_pnl`。

调用方若**不检查返回值**就 `trades.append(..., price=acc.last_fill_price, fee=acc.last_fee)`，
写出来的就是一条**用上一笔的陈旧数据伪造出来的成交**。实测（2026-01-05）：

| 时间 | 方向 | price | fee |
|---|---|---|---|
| 13:15:00 | **BUY** | **604.70220** | **7.26** |
| 13:47:00 | SELL | **604.70220** | **7.26** |  ← 一字不差，且 7.26 是**买入口径**（无印花税）

修复：`strategies/grid_trade.py` 三处卖出路径补 `sold = acc.sell(...)` + `if sold:` 守卫，
参考实现 `core/grid_shared.py:139/149/161`（live 路径早已如此）。

本用例的两段设计
----------------
1. `test_detector_has_discriminating_power`：**判别力自证**（§6.9）——
   人为注入一条伪造幻影，检测器**必须报出来**。否则"检测器恒返回 0"也能让回归通过。
2. `test_no_phantom_in_real_window`：对**历史上真出现过幻影**的窗口做回归
   （修复前该窗口幻影 = 1 笔）。检测器已由第 1 段证明有判别力，第 2 段的 0 才有意义。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP.parent))

import backtest_generic as bg                          # noqa: E402
from strategies import get_strategy                    # noqa: E402

CODE = "300308"
CASH = 1_000_000.0
LIVE_CSV = APP / "data" / "live" / f"stock_{CODE}_1m.csv"
# 修复前实测含 1 笔幻影的窗口（2026-01-05 13:47）
PHANTOM_WINDOW = ("2026-01-01", "2026-08-21")


def _raw_price(recorded: float, side: str, slippage_bps: float) -> float:
    """从含滑点的成交价反推原始价。"""
    if side == "BUY":
        return recorded / (1 + slippage_bps / 10000.0)
    return recorded / (1 - slippage_bps / 10000.0)


def phantom_fills(trades: pd.DataFrame, params: dict, df: pd.DataFrame,
                  init_cash: float = CASH) -> pd.DataFrame:
    """用生产账户回放成交流水，挑出"账户没成交、却被记进 trades"的记录。

    判据：`acc.buy/sell` 返回假值，**或** `last_fill_price` 与调用前完全相同
    （后者说明这一笔根本没改过账户状态，读到的必然是上一笔的值）。
    """
    if trades is None or not len(trades):
        return pd.DataFrame(columns=["time", "direction", "shares"])
    last_close = float(df.sort_values("time")["close"].iloc[-1])
    acc = bg.SimAccount(init_cash, params)
    prev_day, found = None, []
    for _, t in trades.iterrows():
        ts = pd.Timestamp(t["time"])
        if ts.date() != prev_day:
            acc.new_day()
            prev_day = ts.date()
        side, qty = t["direction"], int(t["shares"])
        px = _raw_price(float(t["price"]), side, params["slippage_bps"])
        before = acc.last_fill_price
        ok = acc.buy(px, qty) if side == "BUY" else acc.sell(px, qty)
        if (not ok) or (acc.last_fill_price == before):
            found.append({"time": ts, "direction": side, "shares": qty,
                          "price": float(t["price"]), "fee": float(t["fee"])})
    _ = last_close
    return pd.DataFrame(found, columns=["time", "direction", "shares", "price", "fee"])


def _params(df: pd.DataFrame) -> dict:
    """与 validate_robustness.py:23-40 同口径。"""
    p = get_strategy("grid_trade").default_params()
    p["slippage_bps"] = 5
    px = float(df["close"].iloc[0])
    shares = int((CASH * 0.30 / px) // 100 * 100)
    p["base_position"] = shares
    p["max_position"] = shares * 2
    p["trade_shares"] = max(100, int(shares * 0.1 // 100 * 100))
    return p


# ── 1. 判别力自证：注入一条伪造幻影，检测器必须报出来 ──
def test_detector_has_discriminating_power() -> None:
    """§6.9：检查手段必须先证明"注入缺陷时它报得出来"，否则通过无意义。"""
    params = {"commission_rate": 0.0001, "min_commission": 0.0,
              "stamp_tax_rate": 0.001, "transfer_fee_rate": 0.00002,
              "slippage_bps": 5}
    df = pd.DataFrame({"time": pd.to_datetime(["2026-01-05 09:30", "2026-01-05 09:31",
                                               "2026-01-05 09:32"]),
                       "close": [600.0, 601.0, 602.0]})
    # 伪造：日初买入 400（T+1 锁定），随后记录一笔"卖出 100" —— 实际成交不了
    fake = pd.DataFrame({
        "time": pd.to_datetime(["2026-01-05 09:30", "2026-01-05 09:31"]),
        "direction": ["BUY", "SELL"],
        "shares": [400, 100],
        "price": [600.3, 600.3],      # 第二笔抄了第一笔的成交价
        "fee": [28.8144, 28.8144],    # 也抄了第一笔的
    })
    got = phantom_fills(fake, params, df)
    assert len(got) == 1, f"检测器失去判别力：注入 1 条伪造幻影却报出 {len(got)} 条"
    assert got.iloc[0]["direction"] == "SELL"

    # 反证：真实成交的流水不得被误报
    clean = pd.DataFrame({
        "time": pd.to_datetime(["2026-01-05 09:30"]),
        "direction": ["BUY"], "shares": [400], "price": [600.3], "fee": [28.8144],
    })
    assert len(phantom_fills(clean, params, df)) == 0, "检测器误报：把真实成交判成幻影"


# ── 2. 回归：真出现过幻影的窗口现在必须为 0 ──
def test_no_phantom_in_real_window() -> None:
    """修复前该窗口幻影 = 1（2026-01-05 13:47）；修复后必须为 0。"""
    if not LIVE_CSV.exists():
        pytest.skip(f"缺本地行情 {LIVE_CSV}（data/ 不入库，换机需先准备数据）")
    df = pd.read_csv(LIVE_CSV)
    df["time"] = pd.to_datetime(df["time"])
    begin, end = (pd.Timestamp(x) for x in PHANTOM_WINDOW)
    df = df[(df["time"] >= begin) & (df["time"] <= end)].reset_index(drop=True)
    assert len(df) > 1000, "窗口内数据过少，用例失去意义"

    p = _params(df)
    r = get_strategy("grid_trade").run(df, CASH, p, {"code": CODE})
    trades = r.trades.copy()
    assert len(trades) > 0, "该窗口应有成交，否则用例失去意义"

    got = phantom_fills(trades, p, df)
    assert len(got) == 0, (
        f"H17 回归失败：该窗口出现 {len(got)} 笔幻影成交（账户未成交却被记进 trades）\n"
        f"{got.to_string(index=False)}\n"
        f"检查 strategies/grid_trade.py 三处卖出是否都有 `if sold:` 守卫。"
    )
