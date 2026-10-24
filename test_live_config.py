# -*- coding: utf-8 -*-
"""live 配置层测试：校验必须**真的拦住错配置**，而不是摆设。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import live_config as lc


def _cfg(**kw) -> lc.LiveConfig:
    return lc.LiveConfig(positions=[
        lc.LivePosition(code="300308", strategy_id="grid_trade", **kw)])


# ── 合法路径 ────────────────────────────────────────────────────────────────
def test_valid_config_passes() -> None:
    cfg = lc.LiveConfig(positions=[
        lc.LivePosition("300308", "grid_trade"),
        lc.LivePosition("300502", "intraday_t"),
    ])
    assert lc.validate(cfg) == []


def test_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "live_config.json"
    cfg = lc.LiveConfig(positions=[
        lc.LivePosition("300308", "grid_trade", param_overrides={"grid_pct": 2.5})])
    lc.save(cfg, p)
    back = lc.load(p)
    assert back is not None
    assert lc.to_combos(back) == [("300308", "grid_trade")]
    assert lc.overrides_for(back, "300308", "grid_trade") == {"grid_pct": 2.5}
    assert back.updated_at, "保存时必须写 updated_at"


def test_missing_file_returns_none(tmp_path: Path) -> None:
    """向后兼容：没有配置文件 ⇒ None，调用方回退原 COMBOS。"""
    assert lc.load(tmp_path / "nope.json") is None


# ── 必须拦住的错配置 ────────────────────────────────────────────────────────
def test_rejects_unknown_strategy() -> None:
    errs = lc.validate(lc.LiveConfig(positions=[
        lc.LivePosition("300308", "no_such_strategy")]))
    assert any("未注册" in e for e in errs), errs


def test_rejects_bad_code() -> None:
    errs = lc.validate(lc.LiveConfig(positions=[
        lc.LivePosition("300308.SZ", "grid_trade")]))
    assert any("6 位数字" in e for e in errs), errs


def test_rejects_unknown_param_key() -> None:
    """§6.9「机制存在≠被调用」：拼错的参数名若被静默接受，就是配置没生效还以为改了。"""
    errs = lc.validate(lc.LiveConfig(positions=[
        lc.LivePosition("300308", "grid_trade",
                        param_overrides={"grid_pctt": 2.0})]))
    assert any("未知参数" in e for e in errs), errs


def test_rejects_out_of_range_param() -> None:
    """grid_pct 的 ParamMeta 上限是 10.0（strategies/grid_trade.py:26）。"""
    errs = lc.validate(lc.LiveConfig(positions=[
        lc.LivePosition("300308", "grid_trade", param_overrides={"grid_pct": 99.0})]))
    assert any("高于上限" in e for e in errs), errs


def test_rejects_duplicate_combo() -> None:
    errs = lc.validate(lc.LiveConfig(positions=[
        lc.LivePosition("300308", "grid_trade"),
        lc.LivePosition("300308", "grid_trade"),
    ]))
    assert any("重复配置" in e for e in errs), errs


def test_rejects_bad_risk_pct() -> None:
    assert any("risk_pct" in e for e in lc.validate(lc.LiveConfig(risk_pct=1.5)))


def test_save_refuses_invalid(tmp_path: Path) -> None:
    """校验不过必须**拒绝落盘**，不能写进去再让执行侧踩坑。"""
    p = tmp_path / "c.json"
    with pytest.raises(ValueError):
        lc.save(lc.LiveConfig(positions=[lc.LivePosition("300308", "nope")]), p)
    assert not p.exists(), "非法配置不得写盘"


# ── 消费侧语义 ──────────────────────────────────────────────────────────────
def test_disabled_excluded_from_combos() -> None:
    cfg = lc.LiveConfig(positions=[
        lc.LivePosition("300308", "grid_trade", enabled=True),
        lc.LivePosition("300308", "ma_swing", enabled=False),
    ])
    assert lc.to_combos(cfg) == [("300308", "grid_trade")], "停用的不得进入执行组合"


def test_effective_params_merges_override() -> None:
    cfg = lc.LiveConfig(positions=[
        lc.LivePosition("300308", "grid_trade", param_overrides={"grid_pct": 3.0})])
    eff = lc.effective_params(cfg, "300308", "grid_trade")
    assert eff["grid_pct"] == 3.0
    assert eff["grid_levels"] == 5, "未覆盖的必须保留默认值"


def test_effective_params_unknown_combo_returns_defaults() -> None:
    cfg = lc.LiveConfig(positions=[])
    eff = lc.effective_params(cfg, "300308", "grid_trade")
    assert eff["grid_pct"] == 2.0
