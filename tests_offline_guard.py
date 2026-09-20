# -*- coding: utf-8 -*-
"""离线守卫（H19 / A7）：禁止**测试路径**真实登录 AmazingData。

为什么需要
----------
`test_fetch.py` 文件头由项目自己写明红线：`ad.login()` 占用 AmazingData
**单点登录**，与观察期 `live_run.py` 的采集进程**互斥**，一旦被测试执行会
**踢掉当天采集**。该脚本因此被排除在 pytest 之外 —— 但**那份排除名单是按
"文件名"列的**，而真实的触发路径是"策略内部依赖"：

- `test_ui_quick.py` → 切策略到 `sentiment_t` → `strategies/sentiment_t.py:50`
  `load_index_min` / `:59-61` `get_stock_industry_index` → `sentiment_data` →
  `data_fetch._login()`；
- `test_app.py` → 点「拉取最新数据」→ `incremental_update` → `_login()`；
- `tests/test_scripts_smoke.py` → 以 **subprocess** 拉起
  `test_sentiment_t_dual_gate.py` / `test_sentiment_t_shared_consistency.py`
  → 同样经 `SentimentTStrategy.run()` 踩到同一条红线。

2026-09-19 用计数探针实测（`_login` 被替换为抛错，不触网）：
默认区间 `incremental_update` **1 次**、`sentiment_t` 三个标的合计 **4 次**，
共 **5 次**真实登录尝试。

三道锁
------
| 锁 | 机制 | 覆盖范围 |
|---|---|---|
| 1 | 同进程 monkeypatch（`conftest.py` 调用 `install()`） | AppTest / 直接调用 |
| 2 | `PYTHONPATH` + `sitecustomize.py`（`test_support/`） | **subprocess 子进程** |
| 3 | 本机时钟门禁 | 兜底：**任何**测试路径一律拒绝（窗口信息用于提示与排障） |

关于第 3 道锁（采集窗口 = **本机时间**）
------------------------------------
采集窗口**不是**北京时概念，而是**本机时间**概念 —— 因为它由计划任务决定：

- 计划任务 `QuantLiveRun` 的 `StartBoundary` = **`04:30:00+03:00`**
  （**本机 GMT+3 的 04:30**，等同**北京 09:30 / A 股开盘**），`DaysOfWeek` = 周一至周五；
- 实测运行时长（本机时间）：09-17 `04:32:08 → 10:09:03`、09-18 `04:32:02 → 10:08:58`
  ⇒ **真实窗口 ≈ 本机 04:30 ~ 10:10**。

🚨 **2026-09-20 修正**：本模块初版把窗口写成"**北京时** 04:30~10:10"，
那对应**本机 23:30 ~ 05:10**，与真实窗口**几乎不重叠**（仅 04:30~05:10 这 40 分钟）
⇒ 会在**采集实际运行期间**（本机 05:10~10:10）误报"不在采集窗口"。
现改为**按本机时间**判断（与任务配置同源）。

（`live_run.py` 早期注释里的"内罗毕=北京-5h"已随搬迁失效；本模块**不再拿时区去推算窗口**，
窗口直接对齐任务配置的本机时刻。）

本模块**不在 import 时做任何事**，只有显式 `install()` 才生效
⇒ 手工跑 `python test_fetch.py` 完全不受影响。
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone

# 采集窗口（**本机时间**）：周一至周五 04:30 ~ 10:10 —— 与计划任务 QuantLiveRun 同源
COLLECT_WINDOW = (dtime(4, 30), dtime(10, 10))
BJ = timezone(timedelta(hours=8))  # 仅用于在提示文案里附上北京时，便于对照 A 股

_installed = False


def beijing_now() -> datetime:
    """当前北京时（由 UTC 换算，**仅供提示文案对照 A 股**）。"""
    return datetime.now(BJ)


def in_collect_window(dt: datetime | None = None) -> bool:
    """是否落在观察期采集窗口内（**本机时间**、周一至周五 04:30~10:10）。

    窗口与计划任务 `QuantLiveRun` 同源（`StartBoundary 04:30:00+03:00` + 周一至周五）
    ⇒ 传入的 `dt` 应为**本机时间**（默认 `datetime.now()`），**不要传北京时**。
    """
    dt = dt or datetime.now()
    if dt.weekday() >= 5:  # 周六 / 周日：采集不开
        return False
    t = dt.timetz().replace(tzinfo=None)
    return COLLECT_WINDOW[0] <= t <= COLLECT_WINDOW[1]


def _make_guard(where: str):
    def blocked(*_args, **_kwargs):
        now = beijing_now()
        danger = in_collect_window(now)
        raise RuntimeError(
            "H19 离线守卫：测试路径禁止真实登录 AmazingData"
            "（单点登录会与观察期采集互斥，可能踢掉当天采集）。\n"
            f"  触发位置：{where}\n"
            f"  触发时刻：{now:%Y-%m-%d %H:%M:%S} 北京（本机 {datetime.now():%Y-%m-%d %H:%M:%S}）\n"
            f"  是否采集窗口：{'是 —— 已拦下，否则会踢掉当天采集' if danger else '否（周末，或本机时间不在 04:30~10:10）'}\n"
            "  处理方式：让被测代码走本地缓存 / 桩数据；确需联网的脚本请"
            "手工运行（见 test_fetch.py 文件头），不要包进 pytest。"
        )
    return blocked


def install(reason: str = "pytest") -> None:
    """把登录收口点替换为守卫。幂等，可重复调用。

    覆盖两个入口：
    - `core.data_fetch._login`：全项目唯一收口点
      （`fetch_kline` / `sentiment_data` / `adjustment` 都经它）
    - `AmazingData.login`：`test_fetch.py` 这类直接 `import AmazingData as ad`
      再自己调 `ad.login()` 的写法
    """
    global _installed
    if _installed:
        return
    from core import data_fetch

    data_fetch._login = _make_guard(f"core.data_fetch._login [{reason}]")
    try:
        import AmazingData as ad

        ad.login = _make_guard(f"AmazingData.login [{reason}]")
    except Exception:  # noqa: BLE001  SDK 未安装时不影响其它测试
        pass
    _installed = True
