# -*- coding: utf-8 -*-
"""H60：live 采集「当日重建额度耗尽」的退出策略（可导入、无副作用）。

背景（2026-10-27 实测，非推测）
==============================
  04:32 首枪 `kline_empty` → 全天 **422 次失败**（circuit_open 234 / kline_empty 136 /
  fetch_timeout 52）；会话重建 20 次触顶 `REBUILD_DAILY_CAP` 后 `rebuild_rejected=252`，
  进程继续每 30 秒撞一次，**空转到 10:10 才死**，白丢 6 小时盘中数据。
  15:30 看门狗重启后 `collect_errors=0`，一次拉回 240 根 —— 证明"换进程"有效。

为什么必须换进程
----------------
  `_rebuild_session()` 只做 `_AD_CACHE.clear()`（清 Python 引用），函数注释明确写
  "**不主动登录**"，也清不掉原生登录线程。当天进程内累计 `login_threads_started=11`、
  控制台 `logon Id 2→3→4` 递增（同账号多会话并存）⇒ 同进程内再重建无解。

设计约束
--------
1. **只由 live 进程注册**（`live_run.main`）。`data_fetch.set_exhausted_hook(None)` 是默认值，
   回测/批量补数永不注册 ⇒ 不可能因额度耗尽退出。
2. **每日退出上限**：默认 2 次。节假日/异常日若一直失败，超过上限就退回"只告警"，
   避免形成"启动→20 分钟触顶→退出→再启动"的重启风暴。
3. **退出码 70**：与崩溃码区分，事后可按码归因（任务历史里一眼看出是主动退出）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

EXHAUST_EXIT_MAX = 2     # 每日最多主动退出 2 次
EXHAUST_EXIT_CODE = 70   # 70 = 额度耗尽主动退出（区别于点计划的 0 与异常崩溃）


def marker_path(log_dir, day: str) -> Path:
    """当日留痕文件路径：<log_dir>/collect_exhausted_YYYYMMDD.json。"""
    return Path(log_dir) / f"collect_exhausted_{str(day).replace('-', '')}.json"


def should_exit(count: int, max_exits: int = EXHAUST_EXIT_MAX) -> bool:
    """第 count 次耗尽是否仍允许退出（超过上限就转持续告警）。"""
    return int(count) <= int(max_exits)


def record_exhaustion(log_dir, detail: dict) -> dict:
    """累加当日耗尽计数并落盘留痕，返回最新状态（含 count）。

    读取失败按 count=0 重新开始：留痕文件坏了不该阻断"该退出就退出"。
    """
    day = str(detail.get("day") or f"{datetime.now():%Y-%m-%d}")
    p = marker_path(log_dir, day)
    state = {"count": 0}
    try:
        if p.exists():
            state = json.loads(p.read_text(encoding="utf-8")) or {"count": 0}
    except Exception:  # noqa: BLE001
        state = {"count": 0}
    state["count"] = int(state.get("count") or 0) + 1
    state["last_at"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    state["detail"] = detail
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def make_hook(log_dir, logger, flush=None, exit_fn=None, max_exits: int = EXHAUST_EXIT_MAX):
    """构造给 `data_fetch.set_exhausted_hook()` 的回调。

    flush / exit_fn 显式注入，测试可完全绕开真实退出与真实日志。
    """
    _exit = exit_fn or os._exit

    def hook(detail: dict) -> None:
        try:
            state = record_exhaustion(log_dir, detail)
        except Exception:  # noqa: BLE001 —— 留痕失败不阻断退出
            state = {"count": 1}
            logger.exception("[exhausted] 留痕写入失败（继续退出流程）")
        count = int(state.get("count") or 0)
        logger.error("[exhausted] 当日重建额度耗尽（第 %d 次）detail=%s", count, detail)
        if not should_exit(count, max_exits):
            logger.error("[exhausted] 今日已退出 %d 次（上限 %d）⇒ 不再退出，转持续告警",
                         max_exits, max_exits)
            return
        logger.error("[exhausted] ⇒ 主动退出进程（exit=%d），交计划任务/看门狗拉起新会话",
                     EXHAUST_EXIT_CODE)
        if flush is not None:
            try:
                flush()
            except Exception:  # noqa: BLE001
                pass
        _exit(EXHAUST_EXIT_CODE)

    return hook
