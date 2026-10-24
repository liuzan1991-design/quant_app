# -*- coding: utf-8 -*-
"""告警送达（H36）：把 `watchdog.log` 里"没人看"的告警，送到**人一定会看到**的地方。

背景
----
D11 教训原文：「72+ 条日志忠实记录了却无人被通知」。H36 的判据（`last_success` 陈旧 >900s）
10-19 已补上，但**送达**一直没做 —— 告警仍只写进 `watchdog.log`。

三条送达通道（按"人能不能看到"排序）
----------------------------------
1. **桌面告警文件** `~/Desktop/量化系统告警.txt` —— 一开机必然看到；
   告警清除时**自动删除**（不残留、不堆积）。可用环境变量 `QUANT_ALERT_DESKTOP=0` 关闭。
2. **`live_outputs/ALERT_ACTIVE.json`** —— 机器可读的"当前未清除告警"状态，
   供每日巡检/摘要读取（`active_alerts()`），不依赖人去翻日志。
3. **`live_outputs/logs/alerts.jsonl`** —— 追加式审计流水（谁在什么时候被叫醒过）。

红线
----
- **告警不得反过来搞坏主流程**：所有写盘/通知都是 best-effort，异常一律吞掉并返回 None。
- **不自动重启**：本模块只负责"让人知道"，动作面仍由 H36 的窄口径告警策略决定（只告警不重启）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
LIVE_DIR = APP_DIR / "live_outputs"
LOG_DIR = LIVE_DIR / "logs"
ACTIVE_FILE = LIVE_DIR / "ALERT_ACTIVE.json"
JSONL_FILE = LOG_DIR / "alerts.jsonl"
_DESKTOP_NAME = "量化系统告警.txt"


def resolve_desktop_dir() -> Path | None:
    """真实桌面目录。

    🚨 不能写死 `~/Desktop`：本机注册表把桌面重定向到了 **D:////Desktop**，
    `C:////Users////<u>////Desktop` 并不存在 ⇒ 告警会静默写到一个没人看的地方
    （正是 H36 要消灭的那类"看起来送了、其实没人收到"）。
    顺序：注册表 `User Shell Folders` → 常见候选 → None（关闭桌面通道，其余通道照写）。
    """
    cands: list[Path] = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            raw, _ = winreg.QueryValueEx(k, "Desktop")
            cands.append(Path(os.path.expandvars(raw)))
    except Exception:  # noqa: BLE001 —— 非 Windows / 键缺失
        pass
    home = Path.home()
    cands += [home / "Desktop", home / "桌面", home / "OneDrive" / "Desktop",
              home / "OneDrive" / "桌面"]
    for c in cands:
        try:
            if c.is_dir():
                return c
        except OSError:
            continue
    return None


_desktop_dir = resolve_desktop_dir()
DESKTOP_FILE: Path | None = (
    _desktop_dir / _DESKTOP_NAME if _desktop_dir is not None else None)

ENV_DESKTOP = "QUANT_ALERT_DESKTOP"     # "0" ⇒ 不写桌面文件


def _desktop_enabled() -> bool:
    return os.environ.get(ENV_DESKTOP, "1").strip() not in ("0", "false", "False", "")


def _now(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _load() -> dict:
    try:
        if ACTIVE_FILE.exists():
            data = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 —— 坏文件当作空
        return {}
    return {}


def _save(active: dict) -> None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACTIVE_FILE.with_name(ACTIVE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(active, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ACTIVE_FILE)


def _render_desktop(active: dict) -> None:
    if not _desktop_enabled() or DESKTOP_FILE is None:
        return   # 桌面通道不可用（如找不到桌面目录）⇒ 其余通道照写，不阻断
    try:
        if not active:
            if DESKTOP_FILE.exists():
                DESKTOP_FILE.unlink()
            return
        lines = ["量化系统告警（自动生成，告警清除后本文件自动删除）", "=" * 46]
        for kind, item in sorted(active.items()):
            lines += [f"【{kind}】自 {item.get('since')}（最近 {item.get('last_seen')}）",
                      f"  {item.get('message')}"]
        lines += ["", "处置：确认采集已恢复后，本文件会自动消失；若持续出现请联系助手排查。"]
        DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        DESKTOP_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 —— 桌面不可写不影响告警本身
        pass


def raise_alert(kind: str, message: str, now: datetime | None = None) -> dict | None:
    """登记并送达一条告警；`since` 保持首次触发时间（幂等）。失败返回 None，绝不抛。"""
    try:
        ts = _now(now)
        active = _load()
        prev = active.get(kind) or {}
        active[kind] = {"since": prev.get("since", ts), "last_seen": ts,
                        "message": message}
        _save(active)
        JSONL_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(JSONL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts, "kind": kind, "message": message},
                               ensure_ascii=False) + "\n")
        _render_desktop(active)
        return active[kind]
    except Exception:  # noqa: BLE001
        return None


def clear_alert(kind: str) -> None:
    """清除某类告警；若已无未清除告警 ⇒ 桌面文件一并删除。失败不抛。"""
    try:
        active = _load()
        if kind not in active:
            return
        active.pop(kind, None)
        _save(active)
        _render_desktop(active)
    except Exception:  # noqa: BLE001
        pass


def active_alerts() -> dict:
    """当前未清除的告警（供巡检/摘要读取）。读失败返回空 dict。"""
    return _load()
