"""`scripts/skill_sync.py` 的回归防线。

这个工具的价值全在**"能抓住漂移"**上 —— 一个永远返回 `[OK]` 的检查器等于没有检查。
所以本文件的核心不是"一致时返回 0"，而是"**不一致时一定返回 1 且能说出哪一处**"。

全部用 tmp_path 造目录，**不碰真实仓库与 C 盘部署目录**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent


@pytest.fixture()
def sync(tmp_path, monkeypatch):
    """加载 skill_sync 模块，并把留档目录改道到 tmp（避免测试污染 ~/quant_app_backups）。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import skill_sync  # noqa: WPS433

    monkeypatch.setattr(skill_sync, "BACKUP_ROOT", tmp_path / "backups")
    return skill_sync


@pytest.fixture()
def pair(tmp_path):
    """造一对内容完全相同的 源/部署 目录。"""
    src, dst = tmp_path / "repo" / "skill", tmp_path / "deploy" / "skill"
    for base in (src, dst):
        (base / "scripts").mkdir(parents=True)
        (base / "SKILL.md").write_text("v1\n", encoding="utf-8")
        (base / "scripts" / "check_task_state.py").write_text("print(1)\n", encoding="utf-8")
    return src, dst


def run(sync, src: Path, dst: Path, *flags: str) -> int:
    argv = ["skill_sync.py", "--src", str(src), "--dst", str(dst), *flags]
    old = sys.argv
    sys.argv = argv
    try:
        return sync.main()
    finally:
        sys.argv = old


def test_identical_is_ok(sync, pair):
    """基准：完全一致 -> 0 处不一致，退出码 0。"""
    src, dst = pair
    assert sync.diff(src, dst) == []
    assert run(sync, src, dst) == 0


def test_content_drift_is_detected(sync, pair):
    """核心防线：一个字节不同就必须被抓到（不是"看起来一样"）。"""
    src, dst = pair
    (dst / "SKILL.md").write_text("v1 \n", encoding="utf-8")  # 只多一个空格
    assert sync.diff(src, dst) == [("内容不同", "SKILL.md")]
    assert run(sync, src, dst) == 1


def test_missing_on_deploy_side_is_detected(sync, pair):
    """部署侧缺文件（最危险：运行时静默少一个脚本）必须报出来。"""
    src, dst = pair
    (dst / "scripts" / "check_task_state.py").unlink()
    assert sync.diff(src, dst) == [("仅源侧", "scripts/check_task_state.py")]
    assert run(sync, src, dst) == 1


def test_extra_on_deploy_side_is_detected(sync, pair):
    """部署侧多出一个受管 .py（仓库没收录）同样算漂移。"""
    src, dst = pair
    (dst / "scripts" / "rogue.py").write_text("x = 1\n", encoding="utf-8")
    assert sync.diff(src, dst) == [("仅部署侧", "scripts/rogue.py")]
    assert run(sync, src, dst) == 1


def test_bak_files_are_not_drift(sync, pair):
    """`.bak` 是本地留档，存在不算漂移 —— 否则每天都会假红。"""
    src, dst = pair
    (dst / "SKILL.md.bak_20261020_preh46").write_text("old\n", encoding="utf-8")
    assert sync.diff(src, dst) == []
    assert run(sync, src, dst) == 0


def test_push_publishes_and_leaves_backup(sync, pair):
    """改仓库 -> --push：部署侧被覆盖，且旧内容有留档。"""
    src, dst = pair
    (src / "SKILL.md").write_text("v2\n", encoding="utf-8")
    assert run(sync, src, dst, "--push") == 0
    assert (dst / "SKILL.md").read_text(encoding="utf-8") == "v2\n"
    assert list((sync.BACKUP_ROOT).glob("skill_push_*.tar.gz")), "push 前必须先留档"


def test_pull_captures_deploy_side_edits(sync, pair):
    """直接改了 C 盘 -> --pull 把改动收回仓库（救回误改）。"""
    src, dst = pair
    (dst / "SKILL.md").write_text("edited on C\n", encoding="utf-8")
    assert run(sync, src, dst, "--pull") == 0
    assert (src / "SKILL.md").read_text(encoding="utf-8") == "edited on C\n"


def test_push_refuses_missing_deploy_dir(sync, tmp_path, pair):
    """部署目录不存在时拒绝 push —— 不能凭空造一个 WorkBuddy 永远不会读的目录。"""
    src, _ = pair
    ghost = tmp_path / "nowhere" / "skill"
    assert run(sync, src, ghost, "--push") == 2
    assert not ghost.exists()
