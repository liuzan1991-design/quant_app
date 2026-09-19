# -*- coding: utf-8 -*-
"""H20 最小复现：`st.date_input` 返回 tuple，而 app.py:196-197 判的是 list。

背景
----
app.py:195-197 实际代码：
    d_range = st.date_input("回测区间", [default_start, today])
    start = d_range[0].isoformat() if isinstance(d_range, list) and d_range else default_start.isoformat()
    end   = d_range[1].isoformat() if isinstance(d_range, list) and len(d_range) > 1 else today.isoformat()

本文件复刻这三行并打印类型与取值，用来证明：
**`isinstance(d_range, list)` 恒为 False ⇒ 用户选的回测区间被静默丢弃。**

怎么跑
------
    D:\\Anaconda3\\python.exe -X utf8 scripts\\tool_h20_dateinput_repro.py

预期输出（2026-09-19 实测）：
    type=tuple
    isinstance_list=False
    START=2025-09-19      <- 永远是默认值，不随控件变化
    END=2026-09-19
"""
from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

today = date.today()
default_start = today - timedelta(days=365)
d_range = st.date_input("回测区间", [default_start, today])

# ↓↓↓ 与 app.py:196-197 逐字一致 ↓↓↓
start = d_range[0].isoformat() if isinstance(d_range, list) and d_range else default_start.isoformat()
end = d_range[1].isoformat() if isinstance(d_range, list) and len(d_range) > 1 else today.isoformat()
# ↑↑↑ 与 app.py:196-197 逐字一致 ↑↑↑

st.write(f"type={type(d_range).__name__}")
st.write(f"isinstance_list={isinstance(d_range, list)}")
st.write(f"raw={d_range!r}")
st.write(f"START={start}")
st.write(f"END={end}")
