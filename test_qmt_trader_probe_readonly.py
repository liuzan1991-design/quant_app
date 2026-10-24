# -*- coding: utf-8 -*-
"""P1-b 交易侧：只读探针的**只读性**必须被测试钉住（不是口头承诺）。"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parent / "scripts" / "tool_qmt_trader_probe.py"
FORBIDDEN = ("order_stock", "cancel_order", "order_stock_async")


def test_probe_has_no_order_calls() -> None:
    tree = ast.parse(PROBE.read_text(encoding="utf-8"))
    bad = [n.func.attr for n in ast.walk(tree)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr in FORBIDDEN]
    assert not bad, f"只读探针出现下单/撤单调用: {bad}"


def test_probe_guard_rejects_mutation(monkeypatch, tmp_path) -> None:
    """把源码替换成含 `trader.order_stock(...)` 的版本，自检必须拒绝运行。"""
    import importlib.util

    evil = tmp_path / "evil_probe.py"
    src = PROBE.read_text(encoding="utf-8")
    evil.write_text(src.replace("def main() -> int:", "def _evil():\n    trader.order_stock(None)\n\n\ndef main() -> int:"),
                    encoding="utf-8")
    spec = importlib.util.spec_from_file_location("evil_probe", evil)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # 只导入，不执行 main
    with pytest.raises(SystemExit) as exc:
        mod._assert_readonly()
    assert exc.value.code == 2
