# -*- coding: utf-8 -*-
"""周一北京时间 9:30 无人值守入口（Windows 计划任务触发）。

流程：交易日检查 -> 星耀单点登录冲突检查 -> 采集当天实时分钟数据 -> 数据可用才撮合。
全程写日志，不打印凭证。日志目录：D:/Codex输出/自动化日志。
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
LOG_DIR = Path(r"D:\Codex输出\自动化日志")
CODES = ["300308", "688256"]


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with (LOG_DIR / f"monday_{datetime.now():%Y%m%d}.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def is_weekday(dt: datetime) -> bool:
    return dt.weekday() < 5


def check_amazingdata_conflict() -> bool:
    """检测是否有其他 python 进程可能正在使用 AmazingData。

    无法精确判断是否已登录 SDK，但通过检查正在运行的长驻 python 进程数量，
    提供一个保守告警。返回 True 表示疑似冲突。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] 无法检测 python 进程：{exc}")
        return False
    lines = [ln for ln in out.splitlines() if ln.strip()]
    # 排除当前进程自身；这里只做保守告警，数量>0 都提示，但不阻断采集
    log(f"[check] 检测到 {len(lines)} 个 python.exe 进程（含自身），如存在其它星耀进程请先停掉")
    return len(lines) > 1


def run_collector(live: bool, trade_day: str) -> dict:
    cmd = [sys.executable, "-X", "utf8", str(APP_DIR / "core" / "market_collector.py"),
           *CODES, "--trade-day", trade_day]
    if live:
        cmd.append("--live")
    log(f"[collector] 执行：{' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    out = proc.stdout or ""
    err = proc.stderr or ""
    log(f"[collector] stdout: {out.strip()}")
    if err.strip():
        log(f"[collector] stderr: {err.strip()}")
    return {"returncode": proc.returncode, "stdout": out, "stderr": err}


def run_live_loop() -> dict:
    cmd = [sys.executable, "-X", "utf8", str(APP_DIR / "live_loop.py")]
    log(f"[loop] 执行：{' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    out = proc.stdout or ""
    err = proc.stderr or ""
    log(f"[loop] stdout: {out.strip()}")
    if err.strip():
        log(f"[loop] stderr: {err.strip()}")
    return {"returncode": proc.returncode, "stdout": out, "stderr": err}


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    trade_day = now.strftime("%Y-%m-%d")
    log("== 周一自动运行启动 ==")

    if not is_weekday(now):
        log(f"[abort] 今天 {now:%Y-%m-%d %A} 不是工作日，停止")
        return

    # 单点登录冲突保守检查
    check_amazingdata_conflict()

    # 第一步：真实采集当天实时数据
    col = run_collector(live=True, trade_day=trade_day)
    if col["returncode"] != 0:
        log("[abort] 采集失败，不进入撮合")
        return

    # 第二步：采集成功才跑撮合
    loop = run_live_loop()
    if loop["returncode"] != 0:
        log("[abort] 撮合循环失败")
        return

    log("== 周一自动运行完成 ==")


if __name__ == "__main__":
    main()