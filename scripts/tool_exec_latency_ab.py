# -*- coding: utf-8 -*-
"""A1：执行延迟 A/B —— 量化「同根 bar 收盘价成交」带来的绩效偏差。

用法：`"D:/Anaconda3/python.exe" scripts/tool_exec_latency_ab.py`

方法（**只改成交价，不改任何信号**）：
  1) 用生产策略原样跑一遍，拿到成交流水。
  2) 把每一笔的**成交价**换成"信号之后第 K 根 bar 的开盘价"，股数与顺序完全不动。
  3) **用生产账户 `bg.SimAccount` 本身回放**这笔（改价后的）流水 —— 不自己重写费率，
     也不自己重写 T+1，因此 K=0 必须**逐笔恒等**复现生产结果（对照契约，8/8 通过才可信）。

⚠️ 为什么必须用生产账户回放：第 1 版自己重算费用与持仓，对照契约在 4 处失败。
   查明原因是生产 `trades` 里含**未真正成交的幻影记录**（`acc.sell()` 返回值未被检查，
   T+1 不可卖时仍追加一条记录，且 price/fee 取自上一笔）。改用生产账户回放后，
   幻影记录被 `SimAccount` 的 closeable 约束自然挡掉，契约恒等 ——
   这也顺带证明**幻影记录不影响资金与持仓，只污染 trades 表**。

**2026-09-18 首轮实测结论**：grid_trade 在 K=1（次根开盘）下收益变动 ≤0.05%（四个窗口），
即「同根成交」在本策略上**几乎没有虚高**；ma_swing 的 K=1 变动 ±1%（1 分钟噪声量级）。

对照实验：ma_swing 本来就 `shift(1)` + 次日开盘成交，两条策略的执行口径本就不同。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from core.data import load_data                      # noqa: E402
from strategies import get_strategy                  # noqa: E402

ENGINE_DIR = APP_DIR.parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))
import backtest_generic as bg                         # noqa: E402

OUT_DIR = APP_DIR / "test_outputs" / "exec_latency_ab"

CASH = 1_000_000.0
CODE = "300308"
SLIP = 5          # 对齐 validate_robustness.py 基准档


def normalized_params(sid: str, df: pd.DataFrame, cash: float, slip: int) -> dict:
    """与 validate_robustness.py:23-40 完全一致，保证口径可比。"""
    s = get_strategy(sid)
    p = s.default_params()
    p["slippage_bps"] = slip
    if sid == "ma_swing":
        p["position_pct"] = 30.0
    else:
        px = float(df["close"].iloc[0])
        shares = int((cash * 0.30 / px) // 100 * 100)
        if shares < 100:
            raise ValueError("资金不足100股")
        p["base_position"] = shares
        p["max_position"] = shares * 2
        if "fixed_shares" in p:
            p["fixed_shares"] = max(100, int(shares * 0.1 // 100 * 100))
        if "trade_shares" in p:
            p["trade_shares"] = max(100, int(shares * 0.1 // 100 * 100))
    return p


def raw_price(recorded: float, side: str, slip: int) -> float:
    """从"含滑点的成交价"反推原始价（SimAccount 买入加价、卖出减价）。"""
    return recorded / (1 + slip / 10000.0) if side == "BUY" else recorded / (1 - slip / 10000.0)


def replay_with_account(trades: pd.DataFrame, params: dict, df: pd.DataFrame,
                        delayed: pd.Series, k: int, init_cash: float) -> dict:
    """用生产 SimAccount 回放改价后的流水。k=0 时用原成交价（应恒等复现生产）。"""
    d = df.sort_values("time").reset_index(drop=True)
    price_by_time = dict(zip(d["time"], delayed))
    last_close = float(d["close"].iloc[-1])

    acc = bg.SimAccount(init_cash, params)
    prev_day = None
    n_skip = 0
    for _, t in trades.iterrows():
        ts = pd.Timestamp(t["time"])
        day = ts.date()
        if day != prev_day:
            acc.new_day()
            prev_day = day
        side, qty = t["direction"], int(t["shares"])
        if k == 0:
            px = raw_price(float(t["price"]), side, params["slippage_bps"])
        else:
            px = price_by_time.get(ts, np.nan)
            if not np.isfinite(px):            # 末尾不足 k 根 → 退回原价并计数
                px = raw_price(float(t["price"]), side, params["slippage_bps"])
                n_skip += 1
        ok = acc.buy(px, qty) if side == "BUY" else acc.sell(px, qty)
        if not ok:
            n_skip += 1
    return {"equity": acc.equity(last_close), "cash": acc.cash, "pos": acc.total,
            "fees": acc.total_fees, "n_skip": n_skip}


def count_phantoms(trades: pd.DataFrame, params: dict, df: pd.DataFrame,
                   init_cash: float) -> list[dict]:
    """幻影成交检测：用生产账户回放原流水，逐笔比对"记录的价格"是否等于"账户实际成交价"。
    账户没成交（返回 0/False）却被记进 trades 的，即幻影记录。"""
    d = df.sort_values("time").reset_index(drop=True)
    last_close = float(d["close"].iloc[-1])
    acc = bg.SimAccount(init_cash, params)
    prev_day, found = None, []
    for _, t in trades.iterrows():
        ts = pd.Timestamp(t["time"])
        if ts.date() != prev_day:
            acc.new_day()
            prev_day = ts.date()
        side, qty = t["direction"], int(t["shares"])
        px = raw_price(float(t["price"]), side, params["slippage_bps"])
        before_price = acc.last_fill_price
        ok = acc.buy(px, qty) if side == "BUY" else acc.sell(px, qty)
        if (not ok) or (acc.last_fill_price == before_price):
            found.append({"time": ts, "direction": side, "recorded_price": float(t["price"]),
                          "note": "账户未成交，但 trades 里被记了一笔"})
    return found


def run_case(sid: str, df: pd.DataFrame, label: str) -> tuple[list[dict], list[dict]]:
    s = get_strategy(sid)
    p = normalized_params(sid, df, CASH, SLIP)
    r = s.run(df, CASH, p, {"code": CODE})
    trades = r.trades.copy()
    if trades.empty:
        return [{"策略": sid, "窗口": label, "情形": "无成交"}], []
    trades["time"] = pd.to_datetime(trades["time"])

    d = df.sort_values("time").reset_index(drop=True)
    out = []
    prod_eq = float(r.final_equity)
    cases = [("K=0 当根（生产口径）", 0, None),
             ("K=1 次根开盘（可实现）", 1, "open"),
             ("K=1 次根收盘", 1, "close"),
             ("K=2 次根开盘", 2, "open")]
    for name, k, use in cases:
        delayed = d["close"].shift(0) if use is None else d[use].shift(-k)
        res = replay_with_account(trades, p, df, delayed, k, CASH)
        ret = (res["equity"] / CASH - 1) * 100
        out.append({"策略": sid, "窗口": label, "情形": name,
                    "期末权益": round(res["equity"], 2),
                    "收益%": round(ret, 3),
                    "相对K0变动%": None,  # 下面回填
                    "手续费": round(res["fees"], 2),
                    "期末持仓": res["pos"], "跳过笔数": res["n_skip"]})
    base = out[0]["期末权益"]
    for row in out:
        row["相对K0变动%"] = round((row["期末权益"] / base - 1) * 100, 4)

    ph = count_phantoms(trades, p, df, CASH)
    contract_ok = abs(out[0]["期末权益"] - prod_eq) < 0.01 and out[0]["期末持仓"] == int(
        r.equity["position"].iloc[-1])
    out.append({"策略": sid, "窗口": label,
                "情形": f"[对照契约{'✅通过' if contract_ok else '❌失败'}] "
                        f"生产期末权益={prod_eq:.2f} vs K0复算={base:.2f} | "
                        f"成交{len(trades)}笔，幻影记录{len(ph)}笔",
                "期末权益": prod_eq})
    return out, ph


def main() -> None:
    full, _ = load_data(CODE, "2023-01-01", "2026-08-21")
    full["time"] = pd.to_datetime(full["time"])
    last = pd.Timestamp(full["time"].max()).normalize()
    windows = [
        ("全区间", pd.Timestamp("2023-01-01"), last),
        ("滚动12月_202406", pd.Timestamp("2024-06-01"), pd.Timestamp("2025-05-31")),
        ("样本外2025", pd.Timestamp("2025-01-01"), pd.Timestamp("2025-12-31")),
        ("样本外2026YTD", pd.Timestamp("2026-01-01"), last),
    ]
    rows, phantom_rows = [], []
    for label, begin, end in windows:
        df = full[(full["time"] >= begin) & (full["time"] < end + pd.Timedelta(days=1))].copy()
        df.attrs.update(full.attrs)
        if df.empty or pd.to_datetime(df["time"]).dt.date.nunique() < 80:
            print("跳过（数据不足）", label, flush=True)
            continue
        for sid in ("grid_trade", "ma_swing"):
            try:
                res, ph = run_case(sid, df, label)
                rows += res
                for x in ph:
                    phantom_rows.append({"策略": sid, "窗口": label, **x})
            except Exception as exc:  # noqa: BLE001
                import traceback
                rows.append({"策略": sid, "窗口": label, "情形": f"错误: {exc}"})
                traceback.print_exc()
            print("完成", label, sid, flush=True)

    out = pd.DataFrame(rows)
    pd.set_option("display.width", 240)
    pd.set_option("display.max_columns", 30)
    print("\n================ A1 结果 ================")
    print(out.to_string(index=False))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_DIR / "result.csv", index=False, encoding="utf-8-sig")

    ph_df = pd.DataFrame(phantom_rows)
    print("\n================ 幻影成交（账户未成交但被记进 trades）================")
    if ph_df.empty:
        print("未发现")
    else:
        print(f"合计 {len(ph_df)} 笔")
        print(ph_df.groupby(["策略", "窗口"]).size().to_string())
        print(ph_df.head(20).to_string(index=False))
        ph_df.to_csv(OUT_DIR / "phantoms.csv", index=False, encoding="utf-8-sig")
    print(f"\n已写 {OUT_DIR}/result.csv")


if __name__ == "__main__":
    main()
