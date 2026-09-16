# -*- coding: utf-8 -*-
"""心跳看门狗 (W1)：兜住"盘中静默消失"

设计依据
========
背景：观察期已发生三次"盘中静默消失"（09-09 / 09-10 / 09-16）——进程无声消失、
日志干净、无异常，而计划任务的 RestartOnFailure **只对非零退出码生效**，对这类
"退出码 0 / 无痕迹"的死亡完全无效。心跳是唯一可靠的"我还活着"信号。

阈值标定（**从实际分布取，非估算**）
------------------------------------
数据源：`live_outputs/logs/live_*.log` 中 `[collector] 采集完成` 行的相邻间隔
（该行每轮循环写一次，等价于 heartbeat.updated_at 的刷新节奏）

| 日期   | 中位 | P99 | 最大正常间隔 |
|--------|------|-----|--------------|
| 09-16  | 51s  | 86s | **91s**      |
| 09-15  | 49s  | 68s | **74s**      |
| 09-14  | 40s  | 71s | **327s** ← 存在 5.5 分钟的正常间歇 |

取 3 倍余量：3 × 327s = 981s ≈ 16.4 分钟 → **阈值取 20 分钟**
（满足"阈值 ≥ 正常最大间隔 3 倍"的自定纪律，避免把"慢一轮"误判为"死亡"）

存活判据：用 `heartbeat.updated_at`（每轮循环必写，无论采集成功或失败），
**不用 `last_success_at`**。理由：网络中断时采集会连续失败，但循环仍在跑、
updated_at 仍会刷新——那种情况**不该重启**（重启解决不了网络问题，纯属扰动）。
看门狗的职责要窄：只判定"进程还在不在跑"。

三道防误判门槛
--------------
1. **时间窗**：仅在本地 04:35–10:30 生效（盘前到收盘缓冲结束），窗口外一律不动
2. **进程存在性**：若仍有 python 进程持有 live_run.py，判定为"卡死"而非"死亡"
   —— 此时 schtasks /Run 会因 IgnoreNew 静默失效，故**只告警、不动作**
3. **正常收盘识别**：若今日水位已达 14:59，说明已正常收盘，**不重启**

用法
----
    python watchdog.py            # 巡检一次（供计划任务每 5 分钟调用）
    python watchdog.py --dry-run  # 只判断不动作，用于验证
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
# 测试用：允许用环境变量指向沙箱目录，避免测试污染真实状态
LIVE_DIR = Path(os.environ.get("QUANT_LIVE_DIR") or (APP_DIR / "live_outputs"))
HEARTBEAT = LIVE_DIR / "heartbeat.json"
WATCHDOG_LOG = LIVE_DIR / "logs" / "watchdog.log"
TASK_NAME = "QuantLiveRun"

# ---- 阈值（标定依据见模块 docstring）----
STALE_SECONDS = 20 * 60          # 心跳静默超过 20 分钟 → 判定不在跑
WINDOW_START = (4, 35)           # 本地时间窗口起（盘前 5 分钟）
WINDOW_END = (10, 30)            # 本地时间窗口止（收盘缓冲结束后预留）
MARKET_CLOSE_HHMM = "14:59"      # 北京时间的收盘最后一根 bar


def log(msg: str) -> None:
    WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _in_window(now: datetime) -> bool:
    if now.weekday() >= 5:      # 周六周日不巡检
        return False
    return WINDOW_START <= (now.hour, now.minute) <= WINDOW_END


def _parse_dt(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _live_run_processes() -> list:
    """返回持有 live_run.py 的 python 进程 (pid, cmdline) 列表。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        # 用 -EncodedCommand 传参：避免命令行内嵌引号被 Windows 转义破坏
        import base64
        enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        out = (r.stdout or "").strip()
        if not out:
            log(f"[WARN] 进程查询返回空 stdout（rc={r.returncode}, stderr={(r.stderr or '')[:200]!r}）")
            return []
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        return [(d.get("ProcessId"), d.get("CommandLine") or "")
                for d in data if "live_run.py" in (d.get("CommandLine") or "")]
    except Exception as e:
        log(f"[WARN] 进程查询失败: {e!r} | 原始输出片段: {(locals().get('out') or '')[:200]!r}")
        return []


def _today_watermark_reached_close() -> bool:
    """今日水位是否已到收盘最后一根 bar（用来识别正常收盘）。"""
    try:
        wm = LIVE_DIR / "300308.SZ" / "grid_trade" / "watermark.json"
        if not wm.exists():
            return False
        text = wm.read_text(encoding="utf-8", errors="replace")
        return f"{datetime.now():%Y-%m-%d}T{MARKET_CLOSE_HHMM}" in text
    except Exception as e:
        log(f"[WARN] 水位读取失败: {e!r}")
        return False


def main() -> int:
    dry = "--dry-run" in sys.argv
    force = "--ignore-window" in sys.argv   # 测试用：跳过时间窗限制
    now = datetime.now()

    if not force and not _in_window(now):
        # 窗口外静默退出，不写日志（避免每天几十条噪声）
        return 0

    suffix = " [DRY-RUN]" if dry else ""

    # ---- 读心跳 ----
    if not HEARTBEAT.exists():
        log(f"[ALERT] heartbeat.json 不存在 → 今日从未启动{suffix}")
        if not dry:
            _restart("心跳文件缺失")
        return 0

    try:
        hb = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[ALERT] heartbeat.json 解析失败: {e!r}{suffix}")
        return 0

    hb_date = hb.get("date")
    updated_at = _parse_dt(hb.get("updated_at") or "")
    last_success = hb.get("last_success_at")
    age = (now - updated_at).total_seconds() if updated_at else None

    log(f"[检查] date={hb_date} status={hb.get('status')} "
        f"updated_at={hb.get('updated_at')} 距今={f'{age:.0f}s' if age is not None else 'N/A'} "
        f"last_success={last_success} collect_err={hb.get('collect_errors')}{suffix}")

    # ---- 判据：用 updated_at（循环存活），不用 last_success_at ----
    if age is not None and age <= STALE_SECONDS:
        return 0   # 健康

    # ---- 心跳静默：分三种情况处置 ----
    procs = _live_run_processes()
    log(f"[判定] 心跳静默 {age:.0f}s；检出 live_run 进程 {len(procs)} 个"
        f"{[p[0] for p in procs] if procs else ''}{suffix}")

    if procs:
        # 进程还在但心跳停刷 → 卡死。schtasks /Run 会被 IgnoreNew 忽略，故只告警。
        log(f"[ALERT-HANG] 心跳静默 {age:.0f}s 但仍有 live_run 进程存活 "
            f"{[p[0] for p in procs]} → 疑为卡死，**不自动重启**（IgnoreNew 会使其失效）。"
            f"需人工处置{suffix}")
        return 0

    if _today_watermark_reached_close():
        log(f"[OK] 心跳静默 {age:.0f}s，但今日水位已达 {MARKET_CLOSE_HHMM} → 正常收盘，不重启{suffix}")
        return 0

    log(f"[ALERT-DEAD] 心跳静默 {age:.0f}s 且无 live_run 进程、今日数据未到收盘 → 判定静默死亡{suffix}")
    if not dry:
        _restart(f"心跳静默 {age:.0f}s 且无进程")
    return 0


def _restart(reason: str) -> None:
    log(f"[动作] 触发重启 {TASK_NAME}（原因：{reason}）")
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME],
                           capture_output=True, timeout=60)
        log(f"[动作] schtasks 返回码={r.returncode}")
    except Exception as e:
        log(f"[ERROR] 触发失败: {e!r}")
        return

    # 验证：进程启动后立刻会写 status=starting 的心跳
    import time
    time.sleep(20)
    try:
        hb = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        ua = _parse_dt(hb.get("updated_at") or "")
        if ua and (datetime.now() - ua).total_seconds() < 120:
            log(f"[动作] ✅ 重启生效，心跳已刷新至 {hb.get('updated_at')} status={hb.get('status')}")
        else:
            log(f"[动作] ⚠️ 重启后心跳仍未刷新（updated_at={hb.get('updated_at')}）→ 需人工介入")
    except Exception as e:
        log(f"[动作] ⚠️ 重启验证失败: {e!r}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        log(f"[ERROR] 看门狗自身异常: {traceback.format_exc()}")
        raise SystemExit(1)
