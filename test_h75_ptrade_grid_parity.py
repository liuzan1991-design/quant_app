# -*- coding: utf-8 -*-
"""H75/H76：PTrade 移植对账

三件事：
  1. 逻辑等价 —— 同一段 1m 行情喂 `GridSharedEngine`（原）与 `GridCore`（PTrade 移植），
     成交序列（方向/股数/原因/价格）必须逐笔一致。
  2. 语法兼容 —— `ptrade/*.py` 必须能在 Python 3.5 语法下解析（PTrade 多数券商 3.5）。
  3. 安全默认 —— 移植文件必须 `ARMED = False`（不武装不下单）。

运行：D:/Anaconda3/python.exe -m pytest test_h75_ptrade_grid_parity.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import io
import sys
from pathlib import Path

import pandas as pd
import pytest

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backtest_generic as bg  # noqa: E402
from core.grid_shared import GridSharedEngine
from core.live_params import freeze_params
from strategies.grid_trade import GridTradeStrategy

ROOT = Path(__file__).resolve().parent
PTRADE_DIR = ROOT / "ptrade"
LIVE_CSV = ROOT / "data" / "live" / "stock_301717_1m.csv"
SYMBOL = "301717.SZ"
INIT_CASH = 300_000.0
RISK_PCT = 0.25


def _load_ptrade_module(name: str):
    path = PTRADE_DIR / name
    spec = importlib.util.spec_from_file_location("ptrade_" + name.replace(".py", ""), str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _params(init_cash: float = INIT_CASH) -> dict:
    base = GridTradeStrategy.default_params()
    p = freeze_params("grid_trade", 270.0, base, init_cash=INIT_CASH, risk_pct=RISK_PCT)
    # 账户亏损止损的分母，必须与传入账户同口径（H75：live 里这两者一个 1e6 一个 30 万）
    p["_init_cash"] = float(init_cash)
    return p


def _bars() -> list:
    df = pd.read_csv(LIVE_CSV, parse_dates=["time"])
    df = df[(df["time"] >= "2026-10-28") & (df["time"] <= "2026-10-30 23:59")].reset_index(drop=True)
    out = []
    for _, r in df.iterrows():
        out.append({
            "time": r["time"],
            "close": float(r["close"]),
            # 301717 期间无除权：signal_close == close，两条路径同口径
            "signal_close": float(r["close"]),
            "amount": float(r.get("amount", 0) or 0),
            "volume": float(r.get("volume", 0) or 0),
        })
    return out


def _run(engine, bars: list, init_cash: float):
    """返回 (成交序列, 账户, 区间最低权益/初始 - 1)。"""
    acct = bg.SimAccount(init_cash, _params(init_cash))
    trades = []
    worst = 0.0
    for bar in bars:
        for t in engine.on_bar(dict(bar), acct) or ():
            trades.append((t["direction"], int(t["shares"]),
                           round(float(t.get("price") or 0.0), 4),
                           str(t.get("reason", "")),
                           bool(t.get("rejected", False))))
        loss = acct.equity(float(bar["close"])) / init_cash - 1.0
        worst = min(worst, loss)
    return trades, acct, worst


def _compare(init_cash: float) -> tuple:
    bars = _bars()
    assert len(bars) >= 600, f"行情不足：{len(bars)} 根"
    params = _params(init_cash)

    original = GridSharedEngine(dict(params))
    orig_trades, _orig_acct, orig_worst = _run(original, bars, init_cash)

    mod = _load_ptrade_module("grid_301717.py")
    core = mod.GridCore(dict(params))
    port_trades, _port_acct, port_worst = _run(core, bars, init_cash)
    assert port_worst == pytest.approx(orig_worst, abs=1e-9), (
        f"两侧权益路径不一致：原 {orig_worst:.6%} vs 移植 {port_worst:.6%}")
    return original, core, orig_trades, port_trades, orig_worst


@pytest.mark.parametrize("init_cash", [1_000_000.0, INIT_CASH],
                         ids=["基准100万(live现口径)", "基准30万(目标口径)"])
def test_h75_grid_port_matches_original_engine(init_cash: float) -> None:
    """两种资金基准下，移植版与原引擎的成交序列都必须逐笔一致。

    基准不同 ⇒ 行为不同是**正确**的（账户亏损止损按比例），
    但同一基准下两个实现必须完全等价 —— 这才是移植没改逻辑的证据。
    """
    original, core, orig_trades, port_trades, _worst = _compare(init_cash)

    assert port_trades == orig_trades, (
        f"移植版与原引擎成交序列不一致（基准 {init_cash:,.0f}）\n"
        f"原  ({len(orig_trades)} 笔): {orig_trades[:12]}\n"
        f"移植({len(port_trades)} 笔): {port_trades[:12]}"
    )
    assert core.state.grid_bought == original.state.grid_bought
    assert core.state.stopped == original.state.stopped
    assert core.state.base_price == pytest.approx(original.state.base_price, abs=1e-9)
    assert core.state.daily_closes == original.state.daily_closes


def test_h75_account_basis_dilutes_the_5pct_stop() -> None:
    """H75 证据：同一个 `max_grid_loss_pct=5%`，资金基准一换，触发难度差 3.3 倍。

    live 的实际情况：影子账户 100 万、`_init_cash` 100 万，而真实账户只有 30 万。
    持仓 ≈ 200 股 × 273 ≈ 5.5 万，只占 100 万的 5.5%
      ⇒ 要亏掉 5%（5 万），股价得从 273 跌到 22（−92%）⇒ 实际永远不触发。
    换成 30 万基准 ⇒ 同样 5% 只需股价 −27%（趋势行情里完全够得着）。
    """
    _, _, live_like, _, worst_live = _compare(1_000_000.0)
    _, _, real_like, _, worst_real = _compare(INIT_CASH)

    # 本期标的从 279.82 跌到 255.60（−8.7%），账户口径下的实际回撤：
    assert worst_live > -0.01, f"100万基准下账户回撤应接近 0（止损形同虚设），实际 {worst_live:.4%}"
    assert worst_real < -0.01, f"30万基准下同一段行情应有可见回撤，实际 {worst_real:.4%}"
    # 稀释倍数 ≈ 资金基准之比
    assert abs(worst_real / worst_live - 1_000_000.0 / INIT_CASH) < 0.05, (
        f"回撤比例应≈资金基准之比 3.33，实际 {worst_real / worst_live:.2f}")

    # 解析式：触发 5% 所需的价格跌幅
    shares = 200.0
    price = 273.62
    drop_live = 1_000_000.0 * 0.05 / (shares * price)
    drop_real = INIT_CASH * 0.05 / (shares * price)
    assert drop_live > 0.9, f"100万基准：需跌 {drop_live:.1%} 才触发"
    assert drop_real < 0.30, f"30万基准：需跌 {drop_real:.1%} 才触发"
    assert abs(drop_live / drop_real - 1_000_000.0 / INIT_CASH) < 0.05


def test_h75_port_account_uses_real_budget() -> None:
    """移植版必须用真实资金切片做止损分母（这是与纸面的有意差异）。"""
    mod = _load_ptrade_module("grid_301717.py")
    assert float(mod.GRID_PARAMS["_init_cash"]) == float(mod.BUDGET)
    assert mod.BUDGET == INIT_CASH


def test_h75_ptrade_files_parse_under_py35() -> None:
    """PTrade 多数券商内置 3.5：f-string / 变量注解 / 海象都会直接报错。"""
    files = sorted(PTRADE_DIR.glob("*.py"))
    assert files, "ptrade 目录为空"
    bad = []
    for f in files:
        src = io.open(str(f), encoding="utf-8").read()
        try:
            tree = ast.parse(src, filename=str(f), feature_version=(3, 5))
        except SyntaxError as exc:
            bad.append("%s:%s %s" % (f.name, exc.lineno, exc.msg))
            continue
        # 自检发现：feature_version=(3,5) **抓不住 f-string**（静默放过），
        # 必须显式遍历 JoinedStr，否则这条守卫是假绿。
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                bad.append("%s:%s f-string 在 Python 3.5 不可用" % (f.name, node.lineno))
            elif isinstance(node, ast.AnnAssign):
                bad.append("%s:%s 变量注解 在 Python 3.5 不可用" % (f.name, node.lineno))
    assert not bad, "以下写法在 Python 3.5 下不兼容：\n" + "\n".join(bad)


def test_h75_ptrade_grid_is_disarmed_by_default() -> None:
    src = io.open(str(PTRADE_DIR / "grid_301717.py"), encoding="utf-8").read()
    assert "\nARMED = False" in src, "移植文件必须默认不武装"


def test_h75_ptrade_grid_has_no_local_imports() -> None:
    """沙箱里没有我们的 core/：绝不能 import core.* / backtest_generic。"""
    src = io.open(str(PTRADE_DIR / "grid_301717.py"), encoding="utf-8").read()
    for forbidden in ("import core", "from core", "backtest_generic"):
        assert forbidden not in src, f"移植文件不得依赖本地工程：{forbidden}"


def test_h75_guard_itself_catches_fstring() -> None:
    """守卫自证：这条测试防止 `test_h75_ptrade_files_parse_under_py35` 变成假绿。

    2026-10-30 实测：`ast.parse(..., feature_version=(3, 5))` **不会**因 f-string 报错，
    必须靠 JoinedStr 遍历。这里把这个坑钉住。
    """
    src = "x = f'{1}'"
    tree = ast.parse(src, feature_version=(3, 5))
    assert any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree)), (
        "f-string 未被识别 —— py35 守卫已失效")
