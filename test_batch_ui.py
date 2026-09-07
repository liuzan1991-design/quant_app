# -*- coding: utf-8 -*-
"""批量回测界面测试：多股票 × 多策略。"""
from streamlit.testing.v1 import AppTest


def main():
    at = AppTest.from_file(r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app\app.py",
                           default_timeout=300)
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
    assert b is not None and len(b["df"]) == 4, "批量结果应为 2股×2策略=4 行"
    print("批量回测结果行数:", len(b["df"]))
    print(b["df"][["股票", "策略", "总收益%", "股票涨跌%", "超额收益%"]].to_string(index=False))
    print("批量回测界面测试通过")


if __name__ == "__main__":
    main()
