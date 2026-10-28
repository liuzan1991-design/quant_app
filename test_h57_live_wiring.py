# -*- coding: utf-8 -*-
"""H57-S3：live 执行层读配置的接线测试。

三条必须钉住的语义：
1. **无配置文件 ⇒ 组合与参数与原 COMBOS 完全一致**（行为不变的唯一保证）；
2. 有配置 ⇒ 只跑 enabled 的组合；
3. 配置里有 live 不支持的策略 ⇒ **报错**，绝不静默跳过；
   空配置（全停用）⇒ 也报错，避免"以为停了其实在跑"或"以为在跑其实空转"。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import live_match_loop as L
from core import live_config as lc


@pytest.fixture()
def cfg_path(tmp_path, monkeypatch) -> Path:
    p = tmp_path / "live_config.json"
    monkeypatch.setattr(lc, "CONFIG_PATH", p)
    return p


def _write(p: Path, positions: list[dict]) -> None:
    p.write_text(json.dumps({"version": 1, "risk_pct": 0.10,
                             "positions": positions}, ensure_ascii=False),
                 encoding="utf-8")


# ── ① 无配置 ⇒ 原样 ─────────────────────────────────────────────────────────
def test_no_config_falls_back_to_combos(cfg_path: Path) -> None:
    assert not cfg_path.exists()
    assert L._resolve_combos() == L.COMBOS, "无配置必须逐字返回原 COMBOS"


def test_no_config_params_equal_freeze(cfg_path: Path) -> None:
    """无配置时 `_params_for` 必须等于纯 freeze_params 的结果。"""
    from core.live_params import freeze_params
    from strategies.grid_trade import GridTradeStrategy
    px = 612.63
    expect = freeze_params("grid_trade", px, GridTradeStrategy.default_params())
    got = L._params_for(None, "300308", "grid_trade", px)
    assert got == expect


# ── ② 有配置 ⇒ 只跑 enabled ─────────────────────────────────────────────────
def test_config_drives_combos(cfg_path: Path) -> None:
    _write(cfg_path, [
        {"code": "300308", "strategy_id": "grid_trade", "enabled": True},
        {"code": "300308", "strategy_id": "ma_swing", "enabled": False},
        {"code": "300502", "strategy_id": "grid_trade", "enabled": True},
    ])
    assert L._resolve_combos() == [("300308", "grid_trade"), ("300502", "grid_trade")]


def test_config_override_applied(cfg_path: Path) -> None:
    _write(cfg_path, [{"code": "300308", "strategy_id": "grid_trade", "enabled": True,
                       "param_overrides": {"grid_pct": 3.0}}])
    cfg = lc.load(cfg_path)
    got = L._params_for(cfg, "300308", "grid_trade", 612.63)
    assert got["grid_pct"] == 3.0, "配置覆盖必须生效"
    assert got["grid_levels"] == 5, "未覆盖的参数保留默认"


# ── ③ 拒绝静默跳过 ──────────────────────────────────────────────────────────
def test_unsupported_strategy_raises(cfg_path: Path) -> None:
    # H65：intraday_t 已获支持 → 换 sentiment_t；H71：sentiment_t 也放行 ⇒
    # 四个注册策略全部支持，必须用一个**真实不存在**的名字才能测到这条守卫。
    # （这条用例的价值是"配置里写错策略名必须显式报错、不能静默跳过"，与具体名字无关）
    _write(cfg_path, [{"code": "300308", "strategy_id": "no_such_strategy", "enabled": True}])
    with pytest.raises(RuntimeError) as exc:
        L._resolve_combos()
    assert "尚未支持" in str(exc.value) and "no_such_strategy" in str(exc.value)


def test_all_disabled_raises(cfg_path: Path) -> None:
    _write(cfg_path, [{"code": "300308", "strategy_id": "grid_trade", "enabled": False}])
    with pytest.raises(RuntimeError) as exc:
        L._resolve_combos()
    assert "没有任何 enabled" in str(exc.value)


# ── ④ _base_signals 必须复用 obs.params（不许重新 freeze）──────────────────
def test_base_signals_reuses_obs_params(monkeypatch) -> None:
    captured: dict = {}

    class FakeStrat:
        def run(self, df, cash, params, context=None):
            captured.update(params)
            return SimpleNamespace(
                trades=pd.DataFrame(columns=["time", "direction", "shares"]))

    # H65：策略实例化改走注册表，桩也要打在注册表上（打在 L.GridTradeStrategy 上不再生效）
    monkeypatch.setitem(L._STRATEGY_CLASSES, "grid_trade", lambda: FakeStrat())
    monkeypatch.setattr(L, "_load_live", lambda code: pd.DataFrame({
        "time": pd.to_datetime(["2026-10-23 09:31:00"]),
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [100.0], "amount": [100.0]}))

    obs = SimpleNamespace(code="300308", strategy_id="grid_trade",
                          params={"grid_pct": 3.0, "warmup_end": "2026-09-07"})
    L._base_signals(obs)
    assert captured.get("grid_pct") == 3.0, \
        "基准信号路径丢失了配置覆盖 ⇒ engine_consistency 会比错参照物"


# ── ⑤ 启动留痕（最小审计护栏）──────────────────────────────────────────────
def test_applied_config_recorded(tmp_path, monkeypatch) -> None:
    """每次启动都要留「实际生效的组合与覆盖」——否则事后无法解释差异。"""
    monkeypatch.setattr(L, "LIVE_DIR", tmp_path)
    cfg = lc.from_dict({"risk_pct": 0.1, "positions": [
        {"code": "300308", "strategy_id": "grid_trade", "enabled": True,
         "param_overrides": {"grid_pct": 3.0}}]})
    combos = lc.to_combos(cfg)
    L._record_applied_config(cfg, combos)
    p = tmp_path / "config_applied.json"
    assert p.exists()
    got = json.loads(p.read_text(encoding="utf-8"))
    assert got["source"] == "live_config.json"
    assert got["combos"] == [["300308", "grid_trade"]]
    assert got["overrides"]["300308×grid_trade"]["grid_pct"] == 3.0


def test_applied_config_marks_source_when_no_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(L, "LIVE_DIR", tmp_path)
    L._record_applied_config(None, L.COMBOS)
    got = json.loads((tmp_path / "config_applied.json").read_text(encoding="utf-8"))
    assert "源码默认" in got["source"] and got["overrides"] == {}


def test_applied_config_failure_never_blocks(tmp_path, monkeypatch) -> None:
    """留痕失败不得影响启动（写盘异常要吞掉）。"""
    monkeypatch.setattr(L, "LIVE_DIR", tmp_path)
    monkeypatch.setattr(Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘满")))
    L._record_applied_config(None, [("300308", "grid_trade")])   # 不抛即通过
