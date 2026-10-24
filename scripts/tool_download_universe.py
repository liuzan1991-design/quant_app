# -*- coding: utf-8 -*-
"""全市场日线补齐（可断点续跑）。

背景：实测全市场 5225 只中**仅 100 只有本地日线**（中位数 0 根），
其余全部需要从服务器下载；实测 100 只 ≈ 186s ⇒ 全量约 2.7 小时。

设计要点
--------
- **分批**（默认 250 只/批）降低单次请求规模；
- **断点续跑**：每批完成后把已完成的 code 追加到 `_done.txt`，重跑自动跳过；
- **进度可观测**：写 `_progress.log`，不依赖 stdout（后台任务 stdout 可能被吞）；
- **只读行情**，不触碰任何交易接口。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

OUT = APP / "test_outputs" / "market_screen"
DONE = OUT / "_daily_done.txt"
PROGRESS = OUT / "_daily_progress.log"

BATCH = 250
START = "20230101"
END = "20261023"


def _log(msg: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    with open(PROGRESS, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    from xtquant import xtdata

    OUT.mkdir(parents=True, exist_ok=True)
    codes = xtdata.get_stock_list_in_sector("沪深A股")
    done = set()
    if DONE.exists():
        done = {ln.strip() for ln in DONE.read_text(encoding="utf-8").splitlines() if ln.strip()}
    todo = [c for c in codes if c not in done]
    _log(f"启动：全市场 {len(codes)} 只，已完成 {len(done)}，待下载 {len(todo)}")

    total = len(todo)
    t_all = time.time()
    for i in range(0, total, BATCH):
        batch = todo[i:i + BATCH]
        t0 = time.time()
        try:
            xtdata.download_history_data2(batch, period="1d",
                                          start_time=START, end_time=END)
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            _log(f"批次 {i//BATCH} 失败 {type(exc).__name__}: {exc}")
        if ok:
            with open(DONE, "a", encoding="utf-8") as f:
                f.write("\n".join(batch) + "\n")
        dt = time.time() - t0
        spent = time.time() - t_all
        eta = spent / (i + len(batch)) * (total - i - len(batch))
        _log(f"批次 {i//BATCH + 1}/{(total + BATCH - 1)//BATCH} "
             f"{len(batch)} 只 {dt:.0f}s | 已用 {spent/60:.1f}min | 预计剩余 {eta/60:.0f}min")
        print(f"[{i + len(batch)}/{total}] {dt:.0f}s 剩余≈{eta/60:.0f}min", flush=True)

    _log(f"完成：耗时 {(time.time() - t_all)/60:.1f}min")
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
