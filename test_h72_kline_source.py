# -*- coding: utf-8 -*-
"""H72：行情主源（kline_source）可配置 —— 配置层 + live 接线。

钉住四条语义：
1. 配置层默认 `qmt`（新装的 live 一律走本地 QMT，不再踩 AD 单点会话的坑）；
2. 非法值 ⇒ `validate()` 报错，绝不静默回退；
3. `_apply_kline_source()` 有配置 ⇒ 写入 `QUANT_KLINE_SOURCE` 并返回该值；
4. **无配置 / 读配置抛错 ⇒ 不写环境变量，返回 "ad"**（历史行为不变）。
"""
from __future__ import annotations

import json

import pytest

import live_run as R
from core import live_config as lc


@pytest.fixture()
def cfg_path(tmp_path, monkeypatch):
    p = tmp_path / "live_config.json"
    monkeypatch.setattr(lc, "CONFIG_PATH", p)
    return p


def _write(p, kline_source) -> None:
    p.write_text(json.dumps({"version": 1, "kline_source": kline_source},
                            ensure_ascii=False), encoding="utf-8")


# ── ① 配置层 ────────────────────────────────────────────────────────────────
def test_default_source_is_qmt() -> None:
    assert lc.LiveConfig().kline_source == "qmt"


def test_from_dict_normalizes_case() -> None:
    assert lc.from_dict({"kline_source": " QMT "}).kline_source == "qmt"


def test_roundtrip_keeps_source() -> None:
    cfg = lc.from_dict({"kline_source": "ad"})
    assert lc.from_dict(cfg.to_dict()).kline_source == "ad"


def test_invalid_source_rejected() -> None:
    errs = lc.validate(lc.LiveConfig(kline_source="binance"))
    assert errs and any("kline_source" in e for e in errs), f"非法值必须报错：{errs}"


def test_valid_sources_accepted() -> None:
    for s in ("ad", "qmt", "stockdb"):
        assert not [e for e in lc.validate(lc.LiveConfig(kline_source=s))
                    if "kline_source" in e]


# ── ② live 接线 ─────────────────────────────────────────────────────────────
def test_apply_writes_env(cfg_path, monkeypatch) -> None:
    monkeypatch.delenv("QUANT_KLINE_SOURCE", raising=False)
    _write(cfg_path, "qmt")
    assert R._apply_kline_source() == "qmt"
    import os
    assert os.environ["QUANT_KLINE_SOURCE"] == "qmt"


def test_apply_no_config_leaves_env_untouched(cfg_path, monkeypatch) -> None:
    """无配置 ⇒ 一个字节都不能改（否则等于偷偷把全站主源切了）。"""
    monkeypatch.delenv("QUANT_KLINE_SOURCE", raising=False)
    assert not cfg_path.exists()
    assert R._apply_kline_source() == "ad"
    import os
    assert "QUANT_KLINE_SOURCE" not in os.environ


def test_apply_survives_broken_config(cfg_path, monkeypatch) -> None:
    monkeypatch.delenv("QUANT_KLINE_SOURCE", raising=False)
    cfg_path.write_text("{ this is not json", encoding="utf-8")
    assert R._apply_kline_source() == "ad"
    import os
    assert "QUANT_KLINE_SOURCE" not in os.environ
