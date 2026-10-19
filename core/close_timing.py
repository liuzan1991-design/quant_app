# -*- coding: utf-8 -*-
"""收盘时点判定（纯函数、无副作用，可脱离 live_run 单测）。

为什么单独成文件
================
`live_run.py` 在模块级就打开日志文件并重定向 `sys.stdout/stderr`，任何
`import live_run` 的测试都会污染输出与日志目录。收盘判定又恰好是
H35 / H38-B 两条缺陷的交汇点，必须能被离线精确测试，故抽到这里。

两条缺陷与本文档的关系
----------------------
**H38-B（假收盘）**：旧判据只比较 `latest.time() >= 14:59`，不看日期归属。
上一交易日 14:59 的末根会让"今天"立刻满足收盘条件 —— 非交易日空跑
（2026-09-25 中秋）、开盘前中断重启（2026-09-30）都被当成"已收盘"，
直接写出摘要。产物是幂等覆盖，账面上看不出异常，只能靠事后核对发现。

**H35（兜底不可达）**：旧代码把硬收盘（本地 10:10）写成
`if latest.time() >= 14:59: ... if quiet >= N or _past_hard_close():`，
硬收盘嵌在 bar 条件内部。采集全天失败、latest 停在今日 14:30 时，
外层条件根本不成立 ⇒ 硬收盘永远走不到 ⇒ 摘要 / health 全不写。
本模块只负责"这一根 bar 算不算今日收盘信号"，硬收盘通道由调用方
提为独立分支（见 live_run.py），二者共同修复 H35。

为什么是 `>=` 而不是 `==`
-------------------------
极端数据会给 15:00 标签，用 `==` 会漏掉收盘。`>=` 配合日期归属校验，
既不会把昨日末根当今日，也不会漏掉晚一分钟的标签。
"""
from __future__ import annotations

from datetime import date, datetime, time

# 北京时间最后一根分钟 bar 的标签（数据实测：无 15:00 标签，末根标 14:59）
MARKET_CLOSE_TIME = time(14, 59)


def is_today_close_bar(latest: datetime | None, today: date | None = None) -> bool:
    """`latest` 是否为**今日**收盘时点（或之后）的 bar。

    Args:
        latest: 已采集数据的最新 bar 时间；None 表示本轮没读到数据。
        today: 用于比对的"今天"（本地日期）；默认取 `datetime.now().date()`。
            显式传参是为了让测试不依赖运行时钟。

    Returns:
        True 仅当 latest 非空、时间不早于 14:59、且日期等于 today。
    """
    if latest is None:
        return False
    if latest.time() < MARKET_CLOSE_TIME:
        return False
    return latest.date() == (today or datetime.now().date())
