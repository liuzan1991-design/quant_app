# -*- coding: utf-8 -*-
"""H79 故障注入演练：验证"杀进程 → 拉起 → **数据真的恢复**"的完整链路。

为什么需要它（用户 2026-11-10 口径 4）：
    周末只能验"启动链路"——没有新行情时，`last_success_at` 前进不代表采数恢复。
    真实复采路径必须用**隔离的故障注入**验证：造一个会卡死的采集进程，让它写
    CSV 与心跳，再让看门狗按 H79 判据杀它、拉起新实例，最后检查
    "新实例身份 + last_success 前进 + CSV 水位推进"三者是否同时成立。

隔离边界（不碰生产）：
    - 所有文件写在 `live_outputs/_drill_h79_<时间戳>/` 沙箱；
    - 进程是桩进程（`stub_collector.py`），不是 `live_run.py`；
    - 重启动作被替换成"再起一个桩"，不调用 `schtasks`；
    - 只复用 watchdog 的真实判据与真实 `taskkill`。

用法：
    python scripts/drill_h79_fault_injection.py          # 跑两种场景
    python scripts/drill_h79_fault_injection.py --keep   # 保留沙箱现场
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

STUB_SRC = r'''# -*- coding: utf-8 -*-
"""桩采集进程：mode=healthy 时每 0.6s 写一根 bar；mode=stalled 时只刷心跳不写数据。"""
import json, sys, time
from datetime import datetime, timedelta
from pathlib import Path

root = Path(sys.argv[1]); mode = sys.argv[2]; stall_after = int(sys.argv[3])
csv = root / "data" / "stock_300319_1m.csv"
hb = root / "heartbeat.json"
csv.parent.mkdir(parents=True, exist_ok=True)
started = datetime.now()
errs = 0
n = 0

def last_idx():
    if not csv.exists():
        return -1
    last = None
    for line in csv.read_text(encoding="utf-8").splitlines()[1:]:
        if line.strip():
            last = line.split(",", 1)[0]
    if not last:
        return -1
    t = datetime.fromisoformat(last)
    m = t.hour * 60 + t.minute
    return (m - 570) if m < 690 else (120 + m - 780)

idx = last_idx()
base_day = datetime.now().date()
if not csv.exists():
    csv.write_text("time,open,high,low,close,volume,amount\n", encoding="utf-8")

def beat(last_ok=None):
    now = datetime.now()
    body = {"date": now.strftime("%Y-%m-%d"),
            "started_at": started.isoformat(timespec="seconds"),
            "status": "running",
            "last_success_at": (last_ok or started).isoformat(timespec="seconds"),
            "collect_errors": errs,
            "match_errors": 0,
            "updated_at": now.isoformat(timespec="seconds")}
    hb.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")

last_ok = started
beat()
while True:
    if mode == "healthy" and n < stall_after:
        idx += 1
        m = 570 + idx if idx < 120 else 780 + (idx - 120)
        label = datetime(base_day.year, base_day.month, base_day.day, m // 60, m % 60)
        with csv.open("a", encoding="utf-8") as f:
            f.write(f"{label:%Y-%m-%d %H:%M:%S},1,1,1,1,100,100\n")
        last_ok = datetime.now()
        n += 1
    else:
        errs += 1
    beat(last_ok)
    time.sleep(0.6)
'''


def _sh(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _spawn(sandbox: Path, mode: str, stall_after: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(sandbox / "stub_collector.py"), str(sandbox), mode,
         str(stall_after)],
        cwd=str(sandbox), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait(pred, timeout: float, what: str) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.3)
    raise TimeoutError(f"等待超时：{what}")


def _hb(sandbox: Path) -> dict:
    try:
        return json.loads((sandbox / "heartbeat.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _bars(sandbox: Path) -> int:
    p = sandbox / "data" / "stock_300319_1m.csv"
    if not p.exists():
        return 0
    return max(0, len(p.read_text(encoding="utf-8").strip().splitlines()) - 1)


def _prepare(sandbox: Path) -> None:
    (sandbox / "data").mkdir(parents=True, exist_ok=True)
    (sandbox / "logs").mkdir(parents=True, exist_ok=True)
    (sandbox / "stub_collector.py").write_text(STUB_SRC, encoding="utf-8")


def run_case(name: str, sandbox: Path, relaunch_mode: str, expect_recovered: bool) -> bool:
    import watchdog as w

    sandbox.mkdir(parents=True, exist_ok=True)
    _prepare(sandbox)

    # —— 全部指向沙箱（不碰生产）——
    w.LIVE_DIR = sandbox
    w.HEARTBEAT = sandbox / "heartbeat.json"
    w.WATCHDOG_LOG = sandbox / "logs" / "watchdog.log"
    w.STALL_DATA_DIR = sandbox / "data"
    w.PROC_QUERY_TRUSTED = True
    w.STALL_VERIFY_TIMEOUT = 8
    w.STALL_VERIFY_POLL = 1
    w._combos = lambda: [("300319", "grid_trade")]
    w._today_watermark_reached_close = lambda: False
    # 演练不得写生产告警文件
    w.raise_alert = lambda *a, **k: None
    w.clear_alert = lambda *a, **k: None

    live = []

    def _procs():
        # 生产用 cmdline 含 "live_run.py" 过滤；演练用桩，故按桩名 + PID 存活过滤
        alive = []
        for p in live:
            if p.poll() is None:
                alive.append((p.pid, "stub_collector.py"))
        return alive

    def _restart(reason, wait=True):
        live.append(_spawn(sandbox, relaunch_mode, 3 if relaunch_mode == "healthy" else 0))

    w._live_run_processes = _procs
    w._restart = _restart

    old = _spawn(sandbox, "healthy", 2)
    live.append(old)
    _wait(lambda: _bars(sandbox) >= 2, 15, "桩进程先写 2 根 bar")
    _wait(lambda: int(_hb(sandbox).get("collect_errors") or 0) >= 5, 15, "桩进程累积失败 ≥5")
    before = {"pid": old.pid, "bars": _bars(sandbox),
              "last_success": _hb(sandbox).get("last_success_at")}

    hb = dict(_hb(sandbox))
    ok = w._maybe_restart_stall(hb, 3600.0, " [DRILL]")
    led = w._load_stall_ledger(hb["date"])
    after = {"bars": _bars(sandbox), "last_success": _hb(sandbox).get("last_success_at"),
             "old_alive": old.poll() is None}

    advanced = after["bars"] > before["bars"]
    checks = {
        "判定结果符合预期": ok is expect_recovered,
        "旧进程已被杀": after["old_alive"] is False,
        "账本 attempts=1": int(led.get("attempts") or 0) == 1,
        "successes 与结果一致": int(led.get("successes") or 0) == (1 if expect_recovered else 0),
        "CSV 水位变化符合预期": advanced is expect_recovered,
        "last_success 前进": str(after["last_success"]) > str(before["last_success"]),
    }
    if not expect_recovered:
        checks["失败后已停止重试"] = bool(led.get("stop_reason"))
        n_restart = len(live)
        w._maybe_restart_stall(hb, 3600.0, " [DRILL-2]")
        checks["第二轮不再拉起"] = len(live) == n_restart

    for p in live:
        if p.poll() is None:
            p.kill()

    print(f"\n== 场景：{name} ==")
    for k, v in checks.items():
        print(f"  {'✅' if v else '❌'} {k}")
    print(f"  现场：bars {before['bars']} → {after['bars']}，"
          f"last_success {before['last_success']} → {after['last_success']}，"
          f"ledger={led.get('attempts')}/{led.get('successes')} {led.get('stop_reason') or ''}")
    return all(checks.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留沙箱现场")
    args = ap.parse_args()

    root = APP_DIR / "live_outputs" / f"_drill_h79_{datetime.now():%Y%m%d_%H%M%S}"
    root.mkdir(parents=True, exist_ok=True)
    _prepare(root)

    ok1 = run_case("卡死后拉起新实例并恢复采数", root / "case_a", "healthy", True)
    (root / "case_b").mkdir(parents=True, exist_ok=True)
    _prepare(root / "case_b")
    ok2 = run_case("拉起后仍不产数据 ⇒ 必须判失败并停止重试", root / "case_b", "stalled", False)

    print(f"\n沙箱：{root}")
    print("结论：" + ("两个场景均通过" if ok1 and ok2 else "存在失败项，需修复"))
    if not args.keep and ok1 and ok2:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
