# -*- coding: utf-8 -*-
"""板块过滤语义的专项测试（**A13**，2026-09-20 新增；同日按实测重写）。

为什么需要这个文件
------------------
H23 / A12 决策后，`test_sentiment_t_dual_gate.py` 与
`test_sentiment_t_shared_consistency.py` 都被**显式固定在「无情绪过滤」口径**
（把 `sentiment_data` 的三个入口置空，以消除"口径随缓存新鲜度漂移"）。
⇒ **"板块过滤"逻辑从此不再有任何测试保护** —— 这就是 **A13 空窗**。

本文件专门补这个空窗。

⚠️ 本文件在 2026-09-20 被**重写过一次**，原因见下（值得记）
--------------------------------------------------------
初版设计是"板块 weak vs strong ⇒ 比较交易笔数应不同"，**实测失败**（两侧完全相同）。
查证后得到两条事实：
1. **引擎里 `index_status` 只有 `crash` 与 `strong` 会被读取**
   （`backtest_generic.py:862-863` 禁买特判、`:466` 卖出加分）；
   **`weak` 从未被读取** —— 全仓搜索确认 `'weak'` 只出现在**写入**侧。
   ⇒ 所以 `weak` 与 `flat` 在决策上**完全等价**，初版测试的对照设计天然无判别力。
2. 因此本文件改为**直接截获 `sentiment_t` 传给引擎的 `index_status_map`**，
   断言"**改写本身**"正确 —— 这才是板块过滤唯一真实生效的环节。

⇒ **附带发现（已登记 H24）**：`sentiment_t.py` 的 UI 描述写着"**弱势只卖不买**"
（`:22`）与"**板块弱时更积极卖出**"（`:30`），但**引擎不读 `weak`**
⇒ 这两条描述**与实现背离**；`shared_live.py:96-97` 的"defense ⇒ 降级为 weak"同理是**空操作**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = APP_DIR.parent          # backtest_generic.py 在上一层
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backtest_generic as bg  # noqa: E402
from core import sentiment_data  # noqa: E402
from strategies.sentiment_t import SentimentTStrategy  # noqa: E402

CODE = "300308"
BARS_PER_DAY = 240
LOOKBACK_DAYS = 40          # 截短区间：跑得快（不标 slow，避免落进"分档盲区"）


# ── 受控输入 ──────────────────────────────────────────────────
def _flat_index_df(times) -> pd.DataFrame:
    """构造"大盘恒为 flat"的指数分钟数据（open=close=vwap ⇒ chg=0、dev=0）。"""
    return pd.DataFrame({
        "time": times.values,
        "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
        "volume": 1000.0, "amount": 100000.0,
    })


def _sector_df(kind: str) -> pd.DataFrame:
    """构造板块日线（`compute_sector_status` 只看 close 列）。"""
    if kind == "weak":
        closes = [float(v) for v in range(120, 100, -1)] + [98.0]
    else:
        closes = [float(v) for v in range(100, 120)] + [122.0]
    return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=len(closes)),
                         "close": closes})


def _load_stock() -> pd.DataFrame:
    df = pd.read_csv(APP_DIR / "data" / f"stock_{CODE}_1m.csv", parse_dates=["time"])
    return df.tail(LOOKBACK_DAYS * BARS_PER_DAY).reset_index(drop=True)


def _capture(monkeypatch, *, sector: str | None, use_sector: bool = True) -> dict:
    """跑一次策略，截获它传给引擎的 `index_status_map` / `mode_map`。"""
    df = _load_stock()
    idx = _flat_index_df(df["time"])

    monkeypatch.setattr(sentiment_data, "load_index_min", lambda *a, **k: idx)
    monkeypatch.setattr(sentiment_data, "get_stock_industry_index", lambda *a, **k: "FAKE.SI")
    monkeypatch.setattr(sentiment_data, "load_industry_daily",
                        lambda *a, **k: _sector_df(sector or "flat"))

    captured: dict = {}
    real_run = bg.run_backtest

    def spy(df_, init_cash=100000.0, params=None, index_status_map=None,
            index_change_pct_map=None, mode_map=None):
        captured["status_map"] = index_status_map
        captured["mode_map"] = mode_map
        captured["df"] = df_
        return real_run(df_, init_cash, params,
                        index_status_map=index_status_map,
                        index_change_pct_map=index_change_pct_map,
                        mode_map=mode_map)

    monkeypatch.setattr(bg, "run_backtest", spy)

    strategy = SentimentTStrategy()
    params = strategy.default_params()
    params["use_sector"] = use_sector
    # 关掉 trend_mode：它自己有第三条"defense ⇒ 改 weak"的改写路径
    # （`sentiment_t.py:79-83`），会污染"板块改写"的断言。该路径单独由 H24 看门狗覆盖。
    params["trend_mode"] = False
    strategy.run(df, 1_000_000, params, context={"code": CODE})
    return captured


# ── 前置条件自检（桩必须有效，否则整个用例空转）─────────────
def test_sector_stub_is_valid():
    assert sentiment_data.compute_sector_status(_sector_df("weak")) == "weak"
    assert sentiment_data.compute_sector_status(_sector_df("strong")) == "strong"
    df = _load_stock()
    sm, _ = sentiment_data.compute_market_status(_flat_index_df(df["time"]))
    assert sm and set(sm.values()) == {"flat"}, f"大盘桩不是全 flat：{set(sm.values())}"


# ── 主用例：板块过滤必须真的改写 status_map ───────────────────
def test_sector_weak_rewrites_status_map(monkeypatch):
    """板块 weak ⇒ 非 crash 的状态全部被改写为 weak（`sentiment_t.py:63-65`）。"""
    cap = _capture(monkeypatch, sector="weak")
    sm = cap["status_map"]
    assert sm is not None, "板块 weak 时 status_map 却为 None ⇒ 大盘数据没传进引擎"
    assert set(sm.values()) == {"weak"}, (
        f"板块 weak 未把所有非 crash 状态改写为 weak（实际 {set(sm.values())}）"
        " ⇒ sentiment_t.py 的板块改写逻辑被改坏了")


def test_sector_strong_rewrites_status_map(monkeypatch):
    """板块 strong ⇒ flat/strong 被改写为 strong（`sentiment_t.py:66-68`）。"""
    cap = _capture(monkeypatch, sector="strong")
    sm = cap["status_map"]
    assert sm is not None
    assert set(sm.values()) == {"strong"}, (
        f"板块 strong 未把 flat 改写为 strong（实际 {set(sm.values())}）"
        " ⇒ sentiment_t.py 的板块改写逻辑被改坏了")


def test_sector_disabled_keeps_flat(monkeypatch):
    """`use_sector=False` ⇒ 不得改写（对照组：证明上两个用例的差异确实来自板块分支）。"""
    cap = _capture(monkeypatch, sector="weak", use_sector=False)
    sm = cap["status_map"]
    assert sm is not None
    assert set(sm.values()) == {"flat"}, (
        f"use_sector=False 时状态仍被改写（实际 {set(sm.values())}）⇒ 开关失效")


# ── 防静默失效：status_map 的 key 必须能被引擎查到 ─────────────
def test_status_map_keys_match_engine_timeline(monkeypatch):
    """key 与引擎时间轴必须一致 —— 否则过滤逻辑会**静默失效**（不报错、只是查不到）。

    这是 §6.8「代码事实 ≠ 运行事实」的防线：改写写了，但引擎查不到 ⇒ 等于没写。
    """
    cap = _capture(monkeypatch, sector="strong")
    sm = cap["status_map"]
    df_engine = cap["df"]           # 引擎实收的 df（即 prepare 之后的那份）
    engine_times = set(pd.to_datetime(df_engine["time"]).tolist())
    map_keys = set(sm.keys())
    hit = len(map_keys & engine_times)
    assert hit == len(map_keys) > 0, (
        f"status_map 的 key 与引擎时间轴不匹配：map 有 {len(map_keys)} 个 key，"
        f"仅 {hit} 个能在引擎时间轴里找到 ⇒ 情绪过滤会静默失效")


# ── H24 看门狗：断言"缺陷存在"（修好后本用例应变红以提示更新）─────
def _engine_trade_count(status: str) -> int:
    """把整段区间标成同一个 status，直接跑引擎，返回交易笔数。"""
    from core.adjustment import prepare_signal_prices

    df = _load_stock()
    prepared, _, _ = prepare_signal_prices(df, CODE)
    sm = {pd.Timestamp(t): status for t in prepared["time"]}
    _, trades, _ = bg.run_backtest(prepared, 1_000_000, None, index_status_map=sm)
    return len(trades)


def test_h24_weak_is_equivalent_to_flat():
    """**H24 看门狗**：`weak` 与 `flat` 在引擎决策上等价 ⇒ `weak` 是"只写不读"的状态。

    `sentiment_t.py` 的 UI 描述写着"弱势只卖不买"（`:22`）、"板块弱时更积极卖出"（`:30`），
    但引擎只读 `crash`（`backtest_generic.py:862`）与 `strong`（`:863` / `:466`）
    ⇒ **这两条描述与实现背离**；`shared_live.py:96-97` 的"defense ⇒ 降级为 weak"同理是空操作。

    ⚠️ 本用例**断言缺陷存在**：若将来引擎开始消费 `weak`，它会失败 —— 那时请更新 H24。
    """
    n_flat = _engine_trade_count("flat")
    n_weak = _engine_trade_count("weak")
    assert n_flat == n_weak, (
        f"引擎开始读取 weak 了（flat={n_flat} vs weak={n_weak} 笔）"
        " ⇒ H24「weak 只写不读」的结论已过时，请更新纠偏记录")
    # 顺带钉住对照：strong 必须与 flat 不同（否则连 strong 也没接上，问题更大）
    n_strong = _engine_trade_count("strong")
    assert n_strong != n_flat, (
        f"连 strong 都与 flat 等价（{n_strong} vs {n_flat}）⇒ 情绪过滤整体没接进引擎")
    print(f"\n[H24] 引擎交易笔数 —— flat={n_flat} weak={n_weak} strong={n_strong}")
