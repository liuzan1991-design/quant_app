# -*- coding: utf-8 -*-
"""参数快照：命名保存/加载/删除，供参数组对比使用。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

APP_DIR = Path(__file__).resolve().parents[1]
SNAP_FILE = APP_DIR / "config" / "param_snapshots.json"


def _ensure_file() -> None:
    SNAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SNAP_FILE.exists():
        SNAP_FILE.write_text("{}", encoding="utf-8")


def load_snapshots() -> Dict[str, Dict[str, Any]]:
    _ensure_file()
    try:
        return json.loads(SNAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def list_snapshot_names() -> List[str]:
    return sorted(load_snapshots().keys())


def save_snapshot(name: str, strategy_id: str, params: dict, code: str,
                  start: str, end: str, init_cash: float) -> bool:
    name = name.strip()
    if not name:
        return False
    snaps = load_snapshots()
    snaps[name] = {
        "strategy_id": strategy_id,
        "params": params,
        "code": code,
        "start": start,
        "end": end,
        "init_cash": init_cash,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    SNAP_FILE.write_text(json.dumps(snaps, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def delete_snapshot(name: str) -> bool:
    snaps = load_snapshots()
    if name in snaps:
        del snaps[name]
        SNAP_FILE.write_text(json.dumps(snaps, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    return False
