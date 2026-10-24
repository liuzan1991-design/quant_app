# -*- coding: utf-8 -*-
"""绩效与过拟合检验指标（H14）。

为什么单独成文件
----------------
项目原先**没有 Sharpe**，annual/calmar/mdd 散落在 4 个策略里各自手写，年化一律用 **252**
（A 股一年约 **243** 个交易日）。本模块把口径**收口到一处**，并提供：

- 基础绩效：年化收益 / 年化波动 / Sharpe / Sortino / 最大回撤 / Calmar
- **block bootstrap** 置信区间（不假设收益独立同分布——日收益有自相关，普通 iid 抽样会低估不确定性）
- **PSR / Deflated Sharpe**（Bailey & López de Prado）：回答「考虑到我试了 N 次，这个 Sharpe 还显著吗」
- **Benjamini–Hochberg FDR**：多策略/多时段筛选后的多重检验校正

不依赖 scipy（项目现有依赖里没有），正态分位用 Acklam 近似，精度 ~1e-9。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 243      # A 股：每年约 243~244 个交易日（不是 252）
_EULER_GAMMA = 0.5772156649015329


# ── 正态分布（无 scipy）──
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float) -> float:
    """标准正态分位（Acklam 近似，|误差| < 1.15e-9）。"""
    if not 0.0 < p < 1.0:
        raise ValueError("p 必须在 (0,1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# ── 基础绩效 ──
def daily_returns(equity: pd.Series, times: pd.Series | None = None) -> pd.Series:
    """从（分钟级）权益曲线取**日末**权益，再算日收益。"""
    s = pd.Series(equity.values, index=pd.DatetimeIndex(times)) if times is not None \
        else pd.Series(equity.values, index=pd.DatetimeIndex(equity.index))
    daily = s.groupby(s.index.normalize()).last()
    return daily.pct_change().dropna()


def annualized_return(returns: pd.Series, periods: int = TRADING_DAYS_PER_YEAR) -> float:
    if len(returns) == 0:
        return 0.0
    total = float((1 + returns).prod()) - 1
    years = len(returns) / float(periods)
    if years <= 0 or total <= -1:
        return -1.0
    return (1 + total) ** (1 / years) - 1


def annualized_vol(returns: pd.Series, periods: int = TRADING_DAYS_PER_YEAR) -> float:
    if len(returns) < 2:
        return 0.0
    return float(returns.std(ddof=1) * math.sqrt(periods))


def sharpe(returns: pd.Series, rf: float = 0.0,
           periods: int = TRADING_DAYS_PER_YEAR) -> float:
    """年化 Sharpe（rf 为**年化**无风险利率；日频换算）。"""
    if len(returns) < 2:
        return 0.0
    sd = float(returns.std(ddof=1))
    if sd == 0:
        return 0.0
    excess = float(returns.mean()) - rf / periods
    return excess / sd * math.sqrt(periods)


def sortino(returns: pd.Series, rf: float = 0.0,
            periods: int = TRADING_DAYS_PER_YEAR) -> float:
    if len(returns) < 2:
        return 0.0
    excess = returns - rf / periods
    downside = excess[excess < 0]
    if len(downside) == 0:
        return 0.0
    dd = float(math.sqrt((downside ** 2).mean()))
    return float(excess.mean()) / dd * math.sqrt(periods) if dd else 0.0


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = equity / peak - 1
    return float(dd.min()) if len(dd) else 0.0


def calmar(equity: pd.Series, returns: pd.Series,
           periods: int = TRADING_DAYS_PER_YEAR) -> float:
    mdd = abs(max_drawdown(equity))
    return annualized_return(returns, periods) / mdd if mdd > 1e-12 else 0.0


# ── Block bootstrap ──
def block_bootstrap_ci(x: np.ndarray | pd.Series, stat, n_boot: int = 2000,
                       block: int = 5, alpha: float = 0.05,
                       seed: int = 20260918) -> dict:
    """移动块自举（overlapping block）给任意统计量算百分位置信区间。

    为什么不用 iid 抽样：日收益有自相关（策略持仓/趋势会延续），iid 重采样会
    **低估**不确定性、把区间做窄，结论比实际更强壮——正是 H14 要防的"看起来显著"。
    """
    arr = np.asarray(x, dtype=float)
    n = len(arr)
    if n < block * 2:
        return {"point": float(stat(arr)) if n else 0.0,
                "lo": None, "hi": None, "n_boot": 0, "block": block}
    rng = np.random.default_rng(seed)
    n_blocks = int(math.ceil(n / block))
    starts_max = n - block
    samples = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, starts_max + 1, size=n_blocks)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n]
        samples[i] = stat(arr[idx])
    lo, hi = np.percentile(samples, [alpha / 2 * 100, (1 - alpha / 2) * 100])
    return {"point": float(stat(arr)), "lo": float(lo), "hi": float(hi),
            "n_boot": int(n_boot), "block": int(block)}


# ── PSR / Deflated Sharpe ──
def probabilistic_sharpe_ratio(sr: float, n_obs: int, skew: float = 0.0,
                               kurt: float = 3.0, benchmark_sr: float = 0.0) -> float:
    """PSR：`P(SR > benchmark)`，**按单期（未年化）Sharpe** 计算。"""
    if n_obs < 2:
        return 0.0
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        return 0.0
    z = (sr - benchmark_sr) * math.sqrt(n_obs - 1) / math.sqrt(denom)
    return norm_cdf(z)


def expected_max_sharpe(n_trials: int, sr_std: float) -> float:
    """N 次独立试验下「最大 Sharpe」的期望（Bailey & LdP）。"""
    if n_trials < 2 or sr_std <= 0:
        return 0.0
    g = _EULER_GAMMA
    return sr_std * ((1 - g) * norm_ppf(1 - 1.0 / n_trials)
                     + g * norm_ppf(1 - 1.0 / (n_trials * math.e)))


def deflated_sharpe_ratio(sr: float, n_obs: int, n_trials: int,
                          skew: float = 0.0, kurt: float = 3.0,
                          sr_std: float | None = None) -> dict:
    """DSR = `P(真实 SR > 0 | 已试 n_trials 次)`；sr 为**单期**Sharpe。

    `sr_std` 为各次试验 Sharpe 的标准差；未提供时按 SR 估计量的标准误近似
    （Bailey 建议实际登记试验分布，此处降级并**显式返回 approximation=True**）。
    """
    if n_obs < 2:
        return {"dsr": 0.0, "sr0": 0.0, "psr": 0.0, "n_trials": int(n_trials),
                "approximation": True}
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        denom = 1.0
    approx = sr_std is None
    if approx:
        sr_std = math.sqrt(denom / max(n_obs - 1, 1))
    sr0 = expected_max_sharpe(max(int(n_trials), 2), sr_std)
    z = (sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom)
    return {"dsr": norm_cdf(z), "sr0": sr0, "psr": probabilistic_sharpe_ratio(sr, n_obs, skew, kurt),
            "n_trials": int(n_trials), "approximation": approx}


# ── 多重检验 ──
def bh_fdr(pvalues: list[float] | np.ndarray, alpha: float = 0.05) -> dict:
    """Benjamini–Hochberg FDR：返回 q 值与"通过"标记（保持输入顺序）。"""
    p = np.asarray(pvalues, dtype=float)
    m = len(p)
    if m == 0:
        return {"q": [], "reject": [], "alpha": alpha, "n_tested": 0}
    order = np.argsort(p)
    ranked = p[order]
    q_sorted = ranked * m / np.arange(1, m + 1)
    q_sorted = np.minimum.accumulate(q_sorted[::-1])[::-1]      # 单调化
    q_sorted = np.clip(q_sorted, 0, 1)
    q = np.empty(m)
    q[order] = q_sorted
    return {"q": q.tolist(), "reject": (q <= alpha).tolist(), "alpha": alpha, "n_tested": m}


def summarize(equity: pd.Series, times: pd.Series | None = None,
              rf: float = 0.0) -> dict:
    """一次算全：日收益 → 年化/波动/Sharpe/Sortino/MDD/Calmar。"""
    ret = daily_returns(equity, times)
    eq_daily = pd.Series(equity.values, index=pd.DatetimeIndex(times)) if times is not None \
        else pd.Series(equity.values, index=pd.DatetimeIndex(equity.index))
    eq_daily = eq_daily.groupby(eq_daily.index.normalize()).last()
    return {
        "days": int(len(ret)),
        "annual_return": annualized_return(ret),
        "annual_vol": annualized_vol(ret),
        "sharpe": sharpe(ret, rf),
        "sortino": sortino(ret, rf),
        "max_drawdown": max_drawdown(eq_daily),
        "calmar": calmar(eq_daily, ret),
        "total_return": float(eq_daily.iloc[-1] / eq_daily.iloc[0] - 1) if len(eq_daily) > 1 else 0.0,
    }
