# -*- coding: utf-8 -*-
"""批量回测界面测试：多股票 × 多策略。

UI 测试（streamlit AppTest），分钟级，标记 slow —— 不进日常回归。

⚠️ 离线约束（H19 / A7）
---------------------
`core/batch.py:32` 每次都会调 `data_fetch.incremental_update(code, start, end)`
—— 缓存覆盖不到时即 `_login()` 真实登录。本测试选的是预置池里的
`601619` / `688256`，属高触发面（2026-09-19 实测确认会真登录）。

为什么用"桩化 incremental_update"而不是"把区间设进缓存"
--------------------------------------------------------
本想通过 `at.date_input[0].set_value(...)` 把区间收到缓存内，让
`incremental_update` 走"已覆盖"分支。**实测失败**，根因是 **H20**：

    app.py:196  start = ... if isinstance(d_range, list) ...
    app.py:197  end   = ... if isinstance(d_range, list) ...

而 `st.date_input` 返回的是 **tuple**（实测 `type=tuple`）⇒ 两个分支
**恒为 False** ⇒ **用户选的回测区间被静默丢弃，永远回退到默认值**
（今天-365 ~ 今天）。这是**生产代码 bug**，已单独登记为 H20，
**不在 A7（只改测试文件）范围内**，故本测试改用测试层桩化绕开它。

桩化方式：把 `data_fetch.incremental_update` 换成"只读本地缓存"的桩 ——
与生产里 `incremental_update` 命中"已覆盖"分支时的行为一致
（返回 `0, 总行数, 提示`），因此**被测的批量回测逻辑完全真实**，
只有"要不要联网"这一点被固定为离线。

守卫（`tests_offline_guard.py`）仍作为第二道保险：桩若被绕过，
守卫会抛错而不是真登录。
"""
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

APP_DIR = Path(r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app")


def _offline_incremental_update(code, start, end, data_dir=None):
    """incremental_update 的离线桩：只读本地 CSV，绝不联网。

    行为对齐生产实现命中"本地数据已覆盖"分支时的返回值
    （见 core/data_fetch.py:126-134）。
    """
    d = Path(data_dir) if data_dir else APP_DIR / "data"
    target = d / f"stock_{code}_1m.csv"
    if not target.exists():
        return 0, 0, f"[离线桩] 无本地数据：{code}"
    df = pd.read_csv(target, usecols=["time"], parse_dates=["time"])
    if not len(df):
        return 0, 0, f"[离线桩] 本地数据为空：{code}"
    return (0, len(df),
            f"[离线桩] 本地数据已覆盖 {df['time'].min():%Y-%m-%d} ~ "
            f"{df['time'].max():%Y-%m-%d}，无需更新")


def main():
    # 桩化：批量回测会为每只票调 incremental_update，这里是唯一联网入口
    from core import data_fetch
    data_fetch.incremental_update = _offline_incremental_update

    at = AppTest.from_file(str(APP_DIR / "app.py"), default_timeout=300)
    at.run()
    print("渲染异常:", at.exception)
    assert not at.exception, at.exception

    pool = [m for m in at.multiselect if m.label == "选择股票（预置池）"]
    assert pool, "未找到批量股票选择"
    options = pool[0].options
    print("预置池可选:", len(options), "只")
    pick = [o for o in options if o.startswith("601619") or o.startswith("688256")]
    pool[0].set_value(pick[:2])

    sids = [m for m in at.multiselect if m.label == "选择策略"]
    assert sids, "未找到策略选择"
    sids[0].set_value(["intraday_t", "grid_trade"])
    at.run()

    btn = [b for b in at.button if b.label == "开始批量回测"]
    assert btn, "未找到批量回测按钮"
    btn[0].click()
    at.run()
    print("批量回测后异常:", at.exception)
    assert not at.exception, at.exception

    b = at.session_state["batch"] if "batch" in at.session_state else None
    assert b is not None and len(b["df"]) == 4, (
        f"批量结果应为 2股×2策略=4 行，实际 "
        f"{0 if b is None else len(b['df'])} 行"
        "（若为 0 行：检查本地 data/stock_{601619,688256}_1m.csv 是否存在）")
    print("批量回测结果行数:", len(b["df"]))
    print(b["df"][["股票", "策略", "总收益%", "股票涨跌%", "超额收益%"]].to_string(index=False))
    print("批量回测界面测试通过")


@pytest.mark.slow
def test_batch_ui_smoke():
    """pytest 入口：包装原 main()（AppTest UI 冒烟，分钟级，进慢档）。"""
    main()


if __name__ == "__main__":
    main()
