# -*- coding: utf-8 -*-
"""底仓资金不足的前置拦截测试。

背景（2026-10-26 真实事故）：
  用户在 tickflow 回测页对 301717.SZ（约 527 元/股）用 10 万初始资金回测，
  得到 `WORKER_FAILED`。查下来不是资金"不够做T"，而是**底仓 is 固定股数**：
  base_position=1000 股 × 527 元 = 52.7 万，10 万根本建不了底仓。

  引擎 `backtest_generic.py:721-728` 在建底仓失败时只 `print` 一句警告然后
  `continue`（每根 bar 都跳），于是 `equity_curve` 全程为空 ⇒
  `pd.DataFrame([])` 没有 `equity` 列 ⇒ `_build_result` 抛 `KeyError: 'equity'`。

  这个 KeyError 对用户毫无意义，而且只在"钱不够"时触发 —— 属于典型的
  "错误信息指向错误方向"，所以改成在 `run()` 开头前置拦下并说清差多少。
"""
from __future__ import annotations

import pandas as pd
import pytest

from strategies import get_strategy


def _bars(n: int = 40, price: float = 100.0) -> pd.DataFrame:
    t = pd.date_range("2026-08-03 09:30", periods=n, freq="1min")
    return pd.DataFrame({
        "time": t,
        "open": price, "high": price * 1.001, "low": price * 0.999,
        "close": price, "volume": 1000.0, "amount": price * 1000.0,
    })


def test_insufficient_cash_raises_actionable_error() -> None:
    """资金不够建底仓 ⇒ ValueError，且必须说清"差多少、怎么改"。"""
    s = get_strategy("intraday_t")
    p = s.default_params()          # base_position=1000
    df = _bars(price=100.0)         # 底仓需 100,000
    with pytest.raises(ValueError) as ei:
        s.run(df, 50_000, p, {"code": "301717"})
    msg = str(ei.value)
    assert "初始资金不足" in msg
    assert "100,000" in msg, "应算出底仓总成本"
    assert "50,000" in msg, "应指出当前资金"
    assert "底仓股数" in msg, "应给出可执行的下一步"
    # 可买股数 = 50000 // 100 = 500
    assert "500 股" in msg


def test_error_is_value_error_not_keyerror() -> None:
    """**回归钉子**：不能退化成 KeyError('equity')（那正是修复前的表现）。"""
    s = get_strategy("intraday_t")
    with pytest.raises(ValueError) as ei:
        s.run(_bars(price=100.0), 1_000, s.default_params(), {"code": ""})
    # 异常类型必须精确是 ValueError —— 若有人把前置校验删了，
    # 这里会变成 KeyError('equity')，直接红。
    assert isinstance(ei.value, ValueError)
    assert not isinstance(ei.value, KeyError)


def test_sufficient_cash_still_runs() -> None:
    """资金够时必须照常跑通（不能因为加校验把正常路径拦掉）。

    注意权益条数**少于 bar 数**是正常的：指标预热期（min_bars）内的 bar
    在建底仓后仍会 continue，不会写权益点。所以只断言"有记录且不超过总 bar 数"。
    """
    s = get_strategy("intraday_t")
    df = _bars(n=120, price=100.0)
    r = s.run(df, 1_000_000, s.default_params(), {"code": ""})
    assert r.final_equity > 0
    assert 0 < len(r.equity) <= len(df), "权益点应在 (0, bar 数] 之间"
    assert r.equity["equity"].notna().all(), "权益值不能是 NaN"


def test_empty_equity_raises_readable_error() -> None:
    """_build_result 兜底：空权益表要给可读原因，不能是 KeyError。"""
    s = get_strategy("intraday_t")

    class _Acc:
        init_cash = 100_000.0

        def equity(self, price):  # noqa: ANN001
            return 100_000.0

    df = _bars(n=5, price=100.0)
    with pytest.raises(ValueError) as ei:
        s._build_result(_Acc(), [], pd.DataFrame(), df, 100_000.0, s.default_params())
    assert "权益记录" in str(ei.value)
