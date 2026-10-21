#!/usr/bin/env python
"""skill 同步工具：仓库（唯一真源） <-> C 盘部署副本（WorkBuddy 运行时读取）。

背景
----
`quant-close-inspection` 这个 skill 在被 WorkBuddy 加载时，读的是
    C:\\Users\\<user>\\.workbuddy\\skills\\quant-close-inspection\\
而仓库里的副本在
    <repo>/tools/skills/quant-close-inspection/
两份副本天然会漂移。本工具不试图消灭第二份副本（运行时路径改不了），
而是把「漂移」从**静默**变成**可检测**：

    改 skill 的标准动作 = 改仓库 -> python scripts/skill_sync.py --push
    每天收盘巡检跑一次 python scripts/skill_sync.py（只读），漂移就 exit 1

用法
----
    python scripts/skill_sync.py                # 只读检查（默认），一致 exit 0 / 漂移 exit 1
    python scripts/skill_sync.py --push         # 仓库 -> 部署目录（改完 skill 必须执行）
    python scripts/skill_sync.py --pull         # 部署目录 -> 仓库（救回直接改在 C 盘的改动）
    python scripts/skill_sync.py --src A --dst B   # 覆盖路径（自测用，不碰真实目录）

范围
----
只比对/同步 `SKILL.md` 与 `scripts/**/*.py`。
`*.bak*` 是本地留档（已被 .gitignore 忽略），既不进仓库、也不参与比对 —— 它们存在不算漂移。

退出码
------
    0  一致（check）/ 同步成功（push|pull）
    1  存在漂移（check）
    2  路径缺失或参数冲突
    3  同步后校验失败（哈希不一致）
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import time
from pathlib import Path

SKILL_NAME = "quant-close-inspection"
REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_SKILL = REPO_ROOT / "tools" / "skills" / SKILL_NAME
DEPLOY_SKILL = Path.home() / ".workbuddy" / "skills" / SKILL_NAME
BACKUP_ROOT = Path.home() / "quant_app_backups"


def tracked_files(root: Path) -> dict[str, Path]:
    """返回 {相对路径: 绝对路径}，只含参与同步的文件。"""
    out: dict[str, Path] = {}
    top = root / "SKILL.md"
    if top.is_file():
        out["SKILL.md"] = top
    for p in sorted((root / "scripts").rglob("*.py")):
        if p.is_file():
            out[str(p.relative_to(root)).replace("\\", "/")] = p
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def diff(src: Path, dst: Path) -> list[tuple[str, str]]:
    """返回 [(状态, 相对路径)]，状态 ∈ {仅源侧, 仅部署侧, 内容不同}。"""
    a, b = tracked_files(src), tracked_files(dst)
    rows: list[tuple[str, str]] = []
    for rel in sorted(set(a) | set(b)):
        if rel not in b:
            rows.append(("仅源侧", rel))
        elif rel not in a:
            rows.append(("仅部署侧", rel))
        elif sha256(a[rel]) != sha256(b[rel]):
            rows.append(("内容不同", rel))
    return rows


def report(src: Path, dst: Path, rows: list[tuple[str, str]]) -> None:
    print(f"  源  (真源) : {src}")
    print(f"  部署(运行时): {dst}")
    n = len(tracked_files(src))
    print(f"  受管文件   : 源 {n} 个 | 部署 {len(tracked_files(dst))} 个")
    if not rows:
        print("  结论: [OK] 两份完全一致")
        return
    print(f"  结论: [DRIFT] {len(rows)} 处不一致")
    for state, rel in rows:
        print(f"    - {state:6s} {rel}")


def snapshot_deploy(dst: Path) -> Path | None:
    """push 前把部署目录整包留档（含 .bak，出问题可整体回滚）。"""
    if not dst.exists():
        return None
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    out = BACKUP_ROOT / f"skill_push_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(dst, arcname=dst.name)
    print(f"  [留档] 部署目录已打包: {out}  ({out.stat().st_size} B)")
    return out


def copy_side(from_dir: Path, to_dir: Path) -> int:
    src_files = tracked_files(from_dir)
    if not src_files:
        print(f"  [ERROR] 源目录没有受管文件: {from_dir}", file=sys.stderr)
        return 2
    for rel, sp in src_files.items():
        dp = to_dir / rel
        dp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sp, dp)
        print(f"  [COPY] {rel}  {sha256(sp)[:12]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="skill 仓库副本与部署副本的同步/漂移检查")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--push", action="store_true", help="仓库 -> 部署目录")
    g.add_argument("--pull", action="store_true", help="部署目录 -> 仓库")
    ap.add_argument("--src", type=Path, default=None, help="覆盖真源目录（测试用）")
    ap.add_argument("--dst", type=Path, default=None, help="覆盖部署目录（测试用）")
    args = ap.parse_args()

    src = args.src or REPO_SKILL
    dst = args.dst or DEPLOY_SKILL

    print(f"=== skill_sync: {SKILL_NAME} ===")
    if not src.exists():
        print(f"  [ERROR] 真源目录不存在: {src}", file=sys.stderr)
        return 2
    if not dst.exists():
        why = "，拒绝 push（避免凭空造出一个 WorkBuddy 永不读取的目录）" if args.push else ""
        print(f"  [ERROR] 部署目录不存在{why}: {dst}", file=sys.stderr)
        return 2

    if args.push:
        print("  [MODE] push: 仓库 -> 部署")
        snapshot_deploy(dst)
        rc = copy_side(src, dst)
        if rc:
            return rc
    elif args.pull:
        print("  [MODE] pull: 部署 -> 仓库")
        rc = copy_side(dst, src)
        if rc:
            return rc
    else:
        print("  [MODE] check（只读）")

    rows = diff(src, dst)
    report(src, dst, rows)
    if not rows:
        return 0
    if args.push or args.pull:
        print("  [FAIL] 同步后仍不一致 -> exit 3", file=sys.stderr)
        return 3
    print("  [HINT] 改的是仓库侧 -> --push；改的是 C 盘侧 -> --pull", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
