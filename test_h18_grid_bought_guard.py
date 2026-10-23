# -*- coding: utf-8 -*-
"""H18 回归：fast 路径的 `grid_bought` 必须在「真正成交」后才更新（与 live 对齐）。

背景（`量化研究规范_纠偏记录.md` H18）
------------------------------------
`strategies/grid_trade.py` 原先在 `if sold:` 守卫**外**更新 `grid_bought`
（尾盘 `=0`、网格卖出 `-=1`），而 live（`core/grid_shared.py`）在**守卫内**；
`SimAccount.sell` 在 T+1 不可卖时返回 0 ⇒ fast 路径**没卖掉却把档位扣了**，
档位与真实 T 仓脱钩 ⇒ 后续买卖点位错乱。

实测命中场景（300308，2026 年样本外首日）
--------------------------------------
  2026-01-05 13:15  BUY  100 @604.7022（网格买入 −1 档）
  当日涨回卖出线 → 卖出被 T+1 挡住（closeable=0）
  · 旧代码：grid_bought 0→1→**0（假递减）** ⇒ 该笔 T 仓的卖出信号丢失
           ⇒ 一直压到 2026-01-09 才以 **570.59 网格硬止损**出场（t_pnl **−3,410.76**）
  · 新代码：档位保留 1 ⇒ 2026-01-06 09:31 正常「网格卖出(回到0档)」@604.6975（t_pnl −0.47）
窗口整体：157 → 161 笔、期末权益 +5,527（+0.55pp）。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from core.data import load_data
from strategies import get_strategy

APP = Path(__file__).resolve().parent
LIVE_CSV = APP / "data" / "live" / "stock_300308_1m.csv"

BEGIN, END = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-08-21")
BUY_DAY = pd.Timestamp("2026-01-05")
EXPECTED_SELL_DAY = pd.Timestamp("2026-01-06")


@pytest.fixture(scope="module")
def trades_2026ytd() -> pd.DataFrame:
    if not LIVE_CSV.exists():
        pytest.skip(f"缺本地行情 {LIVE_CSV}")
    df, _ = load_data("300308", str(BEGIN.date()), str(END.date()))
    df["time"] = pd.to_datetime(df["time"])
    df = df[(df["time"] >= BEGIN) & (df["time"] <= END)].reset_index(drop=True)
    assert len(df) > 20_000, "窗口内数据过少，用例失去意义"
    r = get_strategy("grid_trade").run(df.copy(), 1_000_000.0,
                                       get_strategy("grid_trade").default_params(),
                                       {"code": "300308"})
    t = r.trades.copy()
    t["time"] = pd.to_datetime(t["time"])
    return t


def test_h18_buy_happened_as_expected(trades_2026ytd: pd.DataFrame) -> None:
    """前置：确认回归场景存在（否则断言失去意义）。"""
    day = trades_2026ytd[trades_2026ytd["time"].dt.normalize() == BUY_DAY]
    buys = day[day["direction"] == "BUY"]
    assert len(buys) >= 1, "2026-01-05 应有网格买入（回归场景的前提）"


def test_h18_t1_block_keeps_grid_level(trades_2026ytd: pd.DataFrame) -> None:
    """核心：T+1 挡住卖出的次日，档位不能被吞掉 —— 必须正常「回到0档」平 T。

    旧代码下此断言失败（该笔卖出信号丢失，只能等 01-09 硬止损）。
    """
    after = trades_2026ytd[trades_2026ytd["time"].dt.normalize() == EXPECTED_SELL_DAY]
    sells = after[after["direction"] == "SELL"]
    assert len(sells) >= 1, (
        "2026-01-06 没有卖出 ⇒ H18 复发：2026-01-05 那笔 -1 档买入的档位被 T+1 阻挡吞掉了"
    )
    row = sells.iloc[0]
    assert "回到0档" in str(row["reason"]), f"卖出档位语义不符：{row['reason']}"
    assert abs(float(row["price"]) - 604.6975) < 1.0, f"卖出价异常：{row['price']}"
    assert float(row["t_pnl"]) > -100, f"该笔 T 不应被拖成大幅亏损：t_pnl={row['t_pnl']}"


def test_h18_lost_level_would_force_stop_loss(trades_2026ytd: pd.DataFrame) -> None:
    """反证：档位丢失的后果是「拖到 01-09 硬止损」。

    修复后窗口内网格硬止损应为 2 笔（旧代码为 4 笔）；若又变成 4 笔，
    说明档位再次被吞 —— 与上一条互为交叉校验。
    """
    stops = trades_2026ytd[trades_2026ytd["reason"] == "网格硬止损"]
    assert len(stops) == 2, f"2026YTD 窗口网格硬止损应为 2 笔，实际 {len(stops)} 笔"


@pytest.mark.slow
def test_h18_fast_live_paths_identical() -> None:
    """两路径端到端一致：fast（grid_trade.run）vs live（GridSharedLiveEngine）。

    同数据、同 params，逐笔比 (time, direction, shares) 且期末权益必须相等。
    这是 H18 的最终验收口径 —— 修复前 fast 157 笔 vs live 161 笔（档位被吞导致分叉），
    修复后 161/161 逐笔一致、权益完全相同。
    """
    from core.grid_shared_live import GridSharedLiveEngine

    df, _ = load_data("300308", str(BEGIN.date()), str(END.date()))
    df["time"] = pd.to_datetime(df["time"])
    df = df[(df["time"] >= BEGIN) & (df["time"] <= END)].reset_index(drop=True)
    assert len(df) > 20_000, "数据不足，用例失去意义"

    p = get_strategy("grid_trade").default_params()
    fast = get_strategy("grid_trade").run(df.copy(), 1_000_000.0, p, {"code": "300308"})
    f = fast.trades.copy()
    f["time"] = pd.to_datetime(f["time"])
    fast_rows = [(str(t), str(d), int(s)) for t, d, s in
                 zip(f["time"], f["direction"], f["shares"])]

    eng = GridSharedLiveEngine("300308.SZ", p, strategy_id="grid_trade", df=df)
    live_rows = []
    for _, bar in df.iterrows():
        for sig in eng.on_bar(bar, eng.account):
            live_rows.append((str(pd.Timestamp(sig.signal_time)), sig.side.value, int(sig.quantity)))

    assert len(fast_rows) == len(live_rows), (
        f"两路径成交笔数不同：fast {len(fast_rows)} vs live {len(live_rows)} ⇒ 档位/风控口径再次分叉"
    )
    diff = [i for i in range(len(fast_rows)) if fast_rows[i] != live_rows[i]]
    assert not diff, (
        f"两路径有 {len(diff)} 笔不一致，首个：\n  fast={fast_rows[diff[0]]}\n  live={live_rows[diff[0]]}"
    )
    live_equity = eng.account.equity(float(df["close"].iloc[-1]))
    assert abs(live_equity - fast.final_equity) < 0.01, (
        f"期末权益不同：fast {fast.final_equity:.2f} vs live {live_equity:.2f}"
    )
