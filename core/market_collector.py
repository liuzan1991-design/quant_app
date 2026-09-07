# -*- coding: utf-8 -*-
"""独立行情采集进程骨架（单点登录，只落盘，不撮合）。

职责边界（依据决策 2）：
- 本进程是唯一持有星耀数智（AmazingData）登录态的进程，负责把当天/增量的
  1 分钟行情拉下来，落盘到 `data/live/`。
- 撮合进程、观察器只读本进程的落盘结果，绝不 import AmazingData、绝不登录。
- 因此星耀「单点登录、避免多进程互踢」的约束天然成立。

两种运行形态（决策待定项，骨架都支持，验证实时数据后二选一）：
- run_once    ：单次拉取当天数据后退出，适合 Windows 计划任务 / cron 定时触发。
- run_forever ：常驻轮询，盘中按 interval 秒拉一次，适合守护进程。

安全约定：
- dry_run=True（默认）不登录、不拉数，只走完目录/文件名/落盘流程的框架自检。
  下一交易日验证 AmazingData 实时数据时，再传 dry_run=False。
- 登录复用 core.data_fetch（内含 suppress_sdk_output，防止 Token 泄漏到控制台）。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
LIVE_DIR = APP_DIR / "data" / "live"

# 交易日盘中轮询默认间隔（秒）
DEFAULT_POLL_INTERVAL = 30


class MarketCollector:
    """行情采集器：登录 -> 拉当天增量分钟数据 -> 原子落盘 live 目录。"""

    def __init__(self, live_dir: Optional[Path] = None, dry_run: bool = True):
        self.live_dir = Path(live_dir) if live_dir else LIVE_DIR
        self.dry_run = dry_run
        self._last_fetch: Optional[str] = None

    # ---- 落盘 -------------------------------------------------------------
    def _target_path(self, code: str) -> Path:
        return self.live_dir / f"stock_{code}_1m.csv"

    def _write_atomic(self, code: str, df) -> int:
        """原子写入：先写 .tmp 再 os.replace，避免撮合进程读到半截文件。"""
        target = self._target_path(code)
        tmp = target.with_suffix(".tmp")
        df.to_csv(tmp, index=False, encoding="utf-8-sig")
        tmp.replace(target)
        return len(df)

    # ---- 拉取（真实登录，dry_run 时跳过） ---------------------------------
    def fetch_today(self, code: str, trade_day: str) -> int:
        """拉取 trade_day 当天（盘中为进行时）的 1 分钟数据并落盘。

        返回落盘行数。dry_run 时只打印将执行的动作，不登录。
        """
        if self.dry_run:
            print(f"[dry_run] 将拉取 {code} 在 {trade_day} 的当天分钟数据，落盘到 {self._target_path(code)}")
            return 0

        from core import data_fetch  # 延迟导入：只有真正拉数才触碰 SDK 模块
        df = data_fetch.fetch_kline(code, trade_day, trade_day, period="min1")
        if df is None or not len(df):
            print(f"[collector] {code} {trade_day} 无新数据（可能非交易日或接口未返回盘中数据）")
            return 0
        return self._write_atomic(code, df)

    def fetch_incremental(self, code: str, start: str, end: str) -> int:
        """增量更新 [start, end] 区间分钟数据并落盘（复用已有增量逻辑）。"""
        if self.dry_run:
            print(f"[dry_run] 将增量更新 {code} [{start} ~ {end}]，落盘到 {self._target_path(code)}")
            return 0
        from core import data_fetch
        _, total, msg = data_fetch.incremental_update(code, start, end, self.live_dir)
        print(f"[collector] {msg}")
        return total

    # ---- 运行入口 ---------------------------------------------------------
    def run_once(self, codes: List[str], trade_day: str) -> dict:
        """单次拉取（定时任务入口）。"""
        result = {}
        self.live_dir.mkdir(parents=True, exist_ok=True)
        for code in codes:
            rows = self.fetch_today(code, trade_day)
            result[code] = rows
            self._last_fetch = datetime.now().isoformat(timespec="seconds")
        return result

    def run_forever(self, codes: List[str], trade_day: str,
                    interval: int = DEFAULT_POLL_INTERVAL) -> None:
        """常驻轮询（守护进程入口）。盘中每 interval 秒拉一次，直到收市。"""
        self.live_dir.mkdir(parents=True, exist_ok=True)
        print(f"[collector] 常驻轮询启动：codes={codes} trade_day={trade_day} interval={interval}s")
        while True:
            self.run_once(codes, trade_day)
            time.sleep(interval)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="独立行情采集进程")
    parser.add_argument("codes", nargs="+", help="股票代码，如 300308 688256")
    parser.add_argument("--trade-day", default=None, help="交易日 YYYY-MM-DD，缺省取今天")
    parser.add_argument("--forever", action="store_true", help="常驻轮询而非单次")
    parser.add_argument("--interval", type=int, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--dry-run", action="store_true", default=True, help="不登录，只走框架")
    parser.add_argument("--live", action="store_true", help="实际登录拉数（覆盖 dry-run）")
    args = parser.parse_args()

    trade_day = args.trade_day or datetime.now().strftime("%Y-%m-%d")
    collector = MarketCollector(dry_run=not args.live)
    if args.forever:
        collector.run_forever(args.codes, trade_day, args.interval)
    else:
        result = collector.run_once(args.codes, trade_day)
        print(f"[collector] 完成：{result}")


if __name__ == "__main__":
    main()
