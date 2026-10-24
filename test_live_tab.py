# -*- coding: utf-8 -*-
"""H57-S2：「实盘控制」tab 的读取逻辑测试。

原则：**界面可以崩（人看得见），但纯函数不许崩**——文件缺失/损坏必须降级，
否则一个坏 json 就能让整个平台的实盘页白屏。

2026-10-25 重写为只读仪表盘后追加两条纪律：
* **回撤口径**：E1 累计回撤必须来自 `daily_history.jsonl`，`daily_summary.json`
  只是兜底（后者曾给出 −25.16%，真值 −3.56%，差约 7 倍）。
* **只读**：模块不得再暴露任何写入入口（配置编辑器已移除）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ui import live_tab

APP = Path(__file__).resolve().parent
LIVE_ROOT = APP / "live_outputs"
SRC = APP / "ui" / "live_tab.py"


def _mk_unit(root: Path, sym="300308.SZ", sid="grid_trade",
             summary=None, history=None) -> Path:
    d = root / sym / sid
    d.mkdir(parents=True, exist_ok=True)
    if summary is not None:
        (d / "daily_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    if history is not None:
        (d / "daily_history.jsonl").write_text(
            "\n".join(json.dumps(h, ensure_ascii=False) for h in history),
            encoding="utf-8")
    return d


# ── 纯函数：collect_status ───────────────────────────────────────────────────
def test_collect_status_missing_dir_returns_empty(tmp_path: Path) -> None:
    got = live_tab.collect_status(tmp_path / "nope")
    assert got["heartbeat"] is None and got["health"] is None
    assert got["alerts"] == {}
    assert got["units"] == []


def test_collect_status_survives_corrupt_json(tmp_path: Path) -> None:
    """坏 json 不得抛 —— 否则界面白屏，人连"哪里坏了"都看不到。"""
    (tmp_path / "heartbeat.json").write_text("{ this is not json", encoding="utf-8")
    (tmp_path / "300308.SZ" / "grid_trade").mkdir(parents=True)
    (tmp_path / "300308.SZ" / "grid_trade" / "daily_summary.json").write_text(
        "broken", encoding="utf-8")
    got = live_tab.collect_status(tmp_path)
    assert got["heartbeat"] is None
    assert len(got["units"]) == 1
    assert got["units"][0]["summary"] == {}


def test_collect_status_reads_real_live_outputs() -> None:
    """真实目录：能读到心跳与至少一个观察单元（只读，不改）。"""
    got = live_tab.collect_status(LIVE_ROOT)
    assert got["heartbeat"] is not None, "live_outputs/heartbeat.json 应可读"
    assert "last_success_at" in got["heartbeat"]
    assert got["units"], "应至少有一个 标的/策略 单元"


def test_collect_status_skips_logs_and_underscore_dirs(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "_preKill").mkdir()
    (tmp_path / "300308.SZ" / "grid_trade").mkdir(parents=True)
    got = live_tab.collect_status(tmp_path)
    names = {u["symbol"] for u in got["units"]}
    assert names == {"300308.SZ"}, f"logs/_ 开头目录必须跳过，实际 {names}"


def test_collect_status_reads_history(tmp_path: Path) -> None:
    _mk_unit(tmp_path, history=[{"date": "2026-10-22", "equity": 975000.0},
                                {"date": "2026-10-23", "equity": 977470.0}])
    got = live_tab.collect_status(tmp_path)
    assert len(got["units"][0]["history"]) == 2


# ── 纯函数：history_frame ────────────────────────────────────────────────────
def test_history_frame_sorts_dedups_and_survives_bad_line(tmp_path: Path) -> None:
    d = _mk_unit(tmp_path)
    # 乱序 + 重复日期（应取最后一条）+ 一条坏行（应跳过，不报废整份）
    (d / "daily_history.jsonl").write_text(
        "\n".join([
            json.dumps({"date": "2026-10-23", "equity": 977470.0,
                        "cumulative_drawdown": -0.035581}),
            "{ 坏行",
            json.dumps({"date": "2026-10-22", "equity": 975000.0,
                        "cumulative_drawdown": -0.033000}),
            json.dumps({"date": "2026-10-23", "equity": 977470.0,
                        "cumulative_drawdown": -0.035581}),
        ]), encoding="utf-8")
    unit = live_tab.collect_status(tmp_path)["units"][0]
    df = live_tab.history_frame(unit)
    assert list(df["日期"]) == ["2026-10-22", "2026-10-23"], list(df["日期"])
    assert df.iloc[-1]["累计回撤%"] == pytest.approx(-3.558)  # 展示口径 3 位小数


def test_history_frame_empty_is_safe() -> None:
    assert live_tab.history_frame({"history": []}).empty


# ── 纯函数：snapshot_table ──────────────────────────────────────────────────
def test_snapshot_table_flattens_fields(tmp_path: Path) -> None:
    d = _mk_unit(tmp_path, summary={
        "date": "2026-10-23", "equity": 977470.0, "cash": 916207.0, "cum_pnl": -24902.0,
        "data_quality": {"status": "complete", "gap_pct": 0.0},
        "position_cap": {"ratio": 0.0627},
    })
    (d / "paper_state.json").write_text(json.dumps({
        "position": {"shares": 100, "cost": 837.93}}, ensure_ascii=False), encoding="utf-8")
    (d / "watermark.json").write_text(json.dumps({
        "processed_upto_by_day": {"2026-10-22": "2026-10-22T14:59:00",
                                  "2026-10-23": "2026-10-23T14:59:00"}},
        ensure_ascii=False), encoding="utf-8")

    df = live_tab.snapshot_table(live_tab.collect_status(tmp_path))
    assert len(df) == 1
    r = df.iloc[0]
    assert r["权益"] == 977470.0
    assert r["持仓股数"] == 100
    assert r["数据质量"] == "complete"
    assert abs(r["集中度%"] - 6.27) < 1e-9
    assert r["水位天数"] == 2
    assert r["水位末日"] == "2026-10-23"


def test_snapshot_table_empty_is_safe() -> None:
    df = live_tab.snapshot_table({"units": []})
    assert df.empty


def test_drawdown_prefers_history_over_summary(tmp_path: Path) -> None:
    """E1 口径防回归：summary 给 −25.16%（错），history 给 −3.56%（真），
    界面必须显示 −3.56%。"""
    _mk_unit(
        tmp_path,
        summary={"date": "2026-10-23", "equity": 977470.0,
                 "cumulative_drawdown": -0.2516},   # 错误口径
        history=[{"date": "2026-10-23", "equity": 977470.0,
                  "cumulative_drawdown": -0.035581}],  # 权威口径
    )
    df = live_tab.snapshot_table(live_tab.collect_status(tmp_path))
    assert df.iloc[0]["累计回撤%"] == pytest.approx(-3.56)


# ── 纯函数：语义参数识别（仅用于标注，界面不改参）────────────────────────────
def test_semantic_keys_excludes_runtime() -> None:
    sem = live_tab.semantic_override_keys("grid_trade", {
        "slippage_bps": 5,          # 运行参数 ⇒ 不算
        "grid_pct": 2.5,            # 语义参数 ⇒ 算
        "hard_stop_pct": 12.0,      # 语义参数 ⇒ 算
    })
    assert sem == ["grid_pct", "hard_stop_pct"], sem


def test_semantic_keys_empty_when_only_runtime() -> None:
    assert live_tab.semantic_override_keys("grid_trade", {"slippage_bps": 8}) == []


# ── 纪律防回归：本页只读 ─────────────────────────────────────────────────────
def test_module_has_no_write_entry() -> None:
    """界面不得再出现写配置/写状态的入口（2026-10-25 拍板：UI 只读）。

    判据两条，缺一不可：① 没有 render_config_editor；② 源码不出现 save/button。
    """
    assert not hasattr(live_tab, "render_config_editor"), "配置编辑器必须移除"
    src = SRC.read_text(encoding="utf-8")
    for bad in ("lc.save", "live_config.save", "st.button", "st.form_submit_button"):
        assert bad not in src, f"只读页不应出现写入/提交入口：{bad}"


# ── 防回归：控件 label 不得与侧边栏重复 ──────────────────────────────────────
@pytest.mark.slow
def test_no_duplicate_select_label_with_sidebar() -> None:
    """H57 回归：tab6 曾用 `st.selectbox("策略", …)`，与侧边栏同名，
    导致 `test_ui_quick.py` 按 label 定位时取到错误控件（慢档 1 例失败）。

    ⇒ 钉住：全 app 中 label == "策略" 的下拉必须**有且只有一个**。
    """
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP / "app.py"), default_timeout=300)
    at.run()
    assert len(at.exception) == 0, [str(e.value)[:200] for e in at.exception]
    labels = [s.label for s in at.selectbox]
    assert labels.count("策略") == 1, f"存在同名下拉（侧边栏 vs 实盘tab）：{labels}"
