# -*- coding: utf-8 -*-
"""AppTest 冒烟测试：渲染 + 点击开始回测。"""
from pathlib import Path

from streamlit.testing.v1 import AppTest


def main():
    at = AppTest.from_file(r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app\app.py",
                           default_timeout=180)
    at.run()
    print("首次渲染异常:", at.exception)
    assert not at.exception, at.exception

    # 点击"开始回测"
    btn = [b for b in at.button if "开始回测" in (b.label or "")]
    assert btn, "未找到开始回测按钮"
    btn[0].click()
    at.run()
    print("回测后异常:", at.exception)
    assert not at.exception, at.exception

    res = at.session_state["result"] if "result" in at.session_state else None
    print("回测结果:", None if res is None else
          f"总收益{res.total_return*100:+.2f}% 回撤{res.max_drawdown*100:.2f}% "
          f"交易{len(res.trades)}笔 手续费{res.total_fees:.2f}")
    assert res is not None

    tabs_labels = [t.label for t in at.tabs]
    print("标签页:", tabs_labels)

    # ── P1 参数组对比：保存快照 → 选择 → 运行对比 ──
    from core import param_store
    at.text_input(key="snap_name_input").set_value("测试组A")
    at.run()
    save_btn = [b for b in at.button if b.label == "保存当前参数"]
    assert save_btn, "未找到保存按钮"
    save_btn[0].click()
    at.run()
    names = param_store.list_snapshot_names()
    print("快照列表:", names)
    assert "测试组A" in names

    at.multiselect(key="snap_compare_select").set_value(["测试组A"])
    at.run()
    run_btn = [b for b in at.button if b.label == "运行对比"]
    assert run_btn, "未找到运行对比按钮"
    run_btn[0].click()
    at.run()
    comp = at.session_state["compare"] if "compare" in at.session_state else None
    print("对比结果:", None if comp is None else
          f"组数{len(comp['metrics'])} 指标行{len(comp['metrics'])}")
    assert comp is not None and len(comp["metrics"]) >= 1

    # 清理测试快照
    param_store.delete_snapshot("测试组A")
    print("P1 参数组对比测试通过")

    # ── P1 HTML 报告导出 ──
    rep_btn = [b for b in at.button if b.label == "导出 HTML 报告"]
    assert rep_btn, "未找到导出按钮"
    rep_btn[0].click()
    at.run()
    reports = sorted(Path(r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app\reports").glob("*.html"))
    print("报告文件:", [p.name for p in reports])
    assert reports and reports[-1].stat().st_size > 10000
    print("P1 报告导出测试通过")

    # ── P1 数据增量更新（真实调用星耀API，已有数据只拉最近几天）──
    fetch_btn = [b for b in at.button if b.label == "拉取最新数据"]
    assert fetch_btn, "未找到拉取按钮"
    fetch_btn[0].click()
    at.run(timeout=300)
    print("拉取后异常:", at.exception)
    assert not at.exception, at.exception
    data_file = Path(r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app\data\stock_601619_1m.csv")
    assert data_file.exists() and data_file.stat().st_size > 1000000
    print("P1 数据增量更新测试通过")


if __name__ == "__main__":
    main()
