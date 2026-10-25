# -*- coding: utf-8 -*-
"""UI 主题一致性的防回归测试（2026-10-25 统一视觉时立）。

为什么值得单独钉
----------------
`app.toggle_theme()` 不是读写主题 API，而是**对 config.toml 做精确字符串替换**：

    .replace('backgroundColor = "#FFFFFF"', 'backgroundColor = "#141922"')

所以只要有人把 config.toml 里的色值改成别的写法（比如 "#F6F8FB"），
替换就会**匹配不到、静默不生效** —— 点"浅色模式"没反应，也不报错。
这种坑不写测试，下一次改配色必定再踩。

同时钉住"统一视觉"的关键锚点，防止日后被无意改回青蓝色。

⚠️ `.streamlit/` 在 `.gitignore` 里（第 23 行）—— 因为 `toggle_theme()` 运行时会改写
config.toml，跟踪它会让每次切主题都脏一次仓库。所以**新克隆的机器上可能根本没有这个文件**：
凡是读 config 的用例必须 `skip` 而不是报错，否则换机就红一片。
"""
from __future__ import annotations

from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent
CONFIG = APP / ".streamlit" / "config.toml"
APP_PY = APP / "app.py"

needs_config = pytest.mark.skipif(
    not CONFIG.exists(),
    reason=".streamlit/ 被 gitignore（toggle_theme 会改写它），本机无 config.toml")


def _cfg() -> str:
    return CONFIG.read_text(encoding="utf-8")


@needs_config
def test_primary_color_is_unified_navy() -> None:
    """主色 = 参照页的深藏青（#1B2B3A），不再是青蓝 #00B4D8。"""
    assert 'primaryColor = "#1B2B3A"' in _cfg()
    assert "#00B4D8" not in _cfg(), "config 里不该再出现旧青蓝主色"


@needs_config
def test_toggle_theme_replacement_pairs_match_config() -> None:
    """toggle_theme 的替换串必须能在 config.toml 里**真的匹配到**。

    三对色值是硬编码在两处的耦合点：config.toml 写什么，app.py 就替换什么。
    任何一边改了写法，深色切换就静默失效。
    """
    cfg = _cfg()
    pairs = [
        ('backgroundColor = "#FFFFFF"', 'backgroundColor = "#141922"'),
        ('secondaryBackgroundColor = "#F0F2F6"', 'secondaryBackgroundColor = "#1A2029"'),
        ('textColor = "#31333F"', 'textColor = "#EDF0F4"'),
    ]
    for light, dark in pairs:
        assert light in cfg, f"config 缺少 toggle 期望的浅色串：{light}"
        assert dark not in cfg, f"config 不该含深色串（说明当前不是浅色态）：{dark}"


def test_inject_css_has_unified_tokens() -> None:
    """统一视觉靠占位符做明暗分支，占位符与替换都必须存在，否则卡片会硬编码白。"""
    src = APP_PY.read_text(encoding="utf-8")
    for token in ("__PAGE_BG__", "__CARD_BG__", "__SIDEBAR_BG__"):
        assert token in src, f"inject_css 缺少占位符 {token}"
    assert '.replace("__PAGE_BG__", page_bg)' in src
    assert '.replace("__CARD_BG__", card_bg)' in src


def test_core_visual_rules_present() -> None:
    """四条统一规则至少各出现一次：白卡片 / 深色主按钮 / 浅蓝表头 / 深色选中下划线。"""
    src = APP_PY.read_text(encoding="utf-8")
    assert '[data-testid="stVerticalBlockBorderWrapper"]' in src
    assert "background:#1B2B3A !important" in src
    assert "background:#EEF4FB !important" in src
    assert "border-bottom:2px solid #1B2B3A" in src
