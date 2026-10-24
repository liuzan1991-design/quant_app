# -*- coding: utf-8 -*-
"""H14：绩效指标与过拟合检验（Sharpe / PSR / Deflated Sharpe / bootstrap / BH-FDR）。

原则：**公式对拍**，不是"跑通了就算对"。每个统计量都用可手算的样本或已知性质验证。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from core import performance as pf


def test_h14_normal_distribution_accuracy() -> None:
    assert pf.norm_cdf(0.0) == pytest.approx(0.5)
    assert pf.norm_ppf(0.5) == pytest.approx(0.0, abs=1e-9)
    assert pf.norm_ppf(0.975) == pytest.approx(1.959963985, abs=1e-7)
    for x in (-2.5, -0.7, 0.3, 1.9):
        assert pf.norm_ppf(pf.norm_cdf(x)) == pytest.approx(x, abs=1e-7)


def test_h14_sharpe_matches_manual_calc() -> None:
    ret = pd.Series([0.01, -0.005, 0.002, 0.0, 0.007])
    sd = ret.std(ddof=1)
    want = ret.mean() / sd * math.sqrt(pf.TRADING_DAYS_PER_YEAR)
    assert pf.sharpe(ret) == pytest.approx(want)
    assert pf.TRADING_DAYS_PER_YEAR == 243, "A 股年化必须用 243，不是 252"


def test_h14_annualized_return_one_year() -> None:
    ret = pd.Series([0.001] * pf.TRADING_DAYS_PER_YEAR)
    assert pf.annualized_return(ret) == pytest.approx(1.001 ** 243 - 1, rel=1e-9), "一年 243 天，复利"
    assert pf.annualized_vol(ret) == pytest.approx(0.0, abs=1e-12)


def test_h14_max_drawdown_and_calmar() -> None:
    eq = pd.Series([100.0, 120.0, 90.0, 110.0])
    assert pf.max_drawdown(eq) == pytest.approx(-0.25), "120 → 90 = -25%"
    ret = eq.pct_change().dropna()
    assert pf.calmar(eq, ret) == pytest.approx(pf.annualized_return(ret) / 0.25)


def test_h14_daily_returns_uses_day_end() -> None:
    d1 = pd.date_range("2026-10-20 09:30", periods=240, freq="1min")
    d2 = pd.date_range("2026-10-21 09:30", periods=240, freq="1min")
    times = d1.append(d2)
    equity = pd.Series(np.linspace(1_000_000, 1_010_000, 480))
    ret = pf.daily_returns(equity, pd.Series(times))
    assert len(ret) == 1, "两天的日末权益只产生 1 个日收益（pct_change 丢首日）"
    assert ret.iloc[0] > 0


def test_h14_block_bootstrap_properties() -> None:
    rng = np.random.default_rng(7)
    x = rng.normal(0.0005, 0.01, 300)
    a = pf.block_bootstrap_ci(x, lambda v: float(np.mean(v) / np.std(v, ddof=1)), n_boot=300)
    b = pf.block_bootstrap_ci(x, lambda v: float(np.mean(v) / np.std(v, ddof=1)), n_boot=300)
    assert a["lo"] == pytest.approx(b["lo"]) and a["hi"] == pytest.approx(b["hi"]), "同种子必须可复现"
    assert a["lo"] <= a["point"] <= a["hi"], "点估计必须落在区间内"

    flat = np.full(100, 1e-4)
    c = pf.block_bootstrap_ci(flat, lambda v: float(np.mean(v)), n_boot=50)
    assert c["lo"] == pytest.approx(c["hi"]) == pytest.approx(1e-4)


def test_h14_psr_and_dsr_monotonicity() -> None:
    base = pf.probabilistic_sharpe_ratio(0.05, 500, 0, 3, benchmark_sr=0.05)
    assert base == pytest.approx(0.5), "SR = benchmark ⇒ PSR = 50%"
    assert pf.probabilistic_sharpe_ratio(0.10, 500) > pf.probabilistic_sharpe_ratio(0.05, 500)

    d1 = pf.deflated_sharpe_ratio(0.10, 500, n_trials=1)["dsr"]
    d20 = pf.deflated_sharpe_ratio(0.10, 500, n_trials=20)["dsr"]
    d200 = pf.deflated_sharpe_ratio(0.10, 500, n_trials=200)["dsr"]
    assert d1 > d20 > d200, "试得越多，门槛越高（DSR 越低）"
    assert pf.deflated_sharpe_ratio(0.30, 500, 20)["dsr"] > d20, "SR 越高 DSR 越高"
    assert pf.deflated_sharpe_ratio(0.1, 500, 20)["sr0"] > 0


def test_h14_bh_fdr_known_example() -> None:
    out = pf.bh_fdr([0.005, 0.5], alpha=0.05)
    assert out["q"][0] == pytest.approx(0.01) and out["q"][1] == pytest.approx(0.5)
    assert out["reject"] == [True, False]

    out2 = pf.bh_fdr([0.01, 0.02, 0.03, 0.9], alpha=0.05)
    assert out2["reject"][0] and out2["reject"][1] and out2["reject"][2]


def test_h14_summarize_end_to_end() -> None:
    times = pd.date_range("2026-01-05 09:30", periods=240, freq="1min").append(
        pd.date_range("2026-01-06 09:30", periods=240, freq="1min"))
    equity = pd.Series(1_000_000 * np.cumprod(np.full(len(times), 1.0002)))
    got = pf.summarize(equity, pd.Series(times))
    assert got["days"] == 1, "两个日末点 ⇒ 1 个日收益"
    assert got["sharpe"] == 0.0, "恒定收益率 ⇒ 波动为 0 ⇒ Sharpe 记 0（不是 inf）"
    assert got["total_return"] > 0
