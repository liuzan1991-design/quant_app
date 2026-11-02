# -*- coding: utf-8 -*-
"""AppTest 冒烟测试：渲染 + 点击开始回测。

UI 测试（streamlit AppTest），分钟级，标记 slow —— 不进日常回归，
需要时用 `pytest -m "not external"` 跑（含慢档）。

⚠️ 离线约束（H19 / A7）
---------------------
本测试**禁止真实登录 AmazingData**（守卫见 `tests_offline_guard.py`）。
原先末尾"拉取最新数据"段会经 `incremental_update` → `_login()` 真实登录，
与观察期采集互斥；且它的断言只看"生产 CSV 存在且够大"，**拉取失败也会绿**
（假绿）。现改为：断言离线守卫确实拦下、且生产数据文件**未被改写**。
真实拉取验证仍在 `test_fetch.py`（手工运行，见其文件头红线）。
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_DIR = Path(r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app")


def main():
    at = AppTest.from_file(str(APP_DIR / "app.py"), default_timeout=180)
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
    try:
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
        # ⚠️ app.py 里有**两个** label 均为「运行对比」的按钮：
        #   [4] 多股票对比（expander「多股票对比（同一参数）」）
        #   [7] 参数组对比（expander「⚙ 更多工具」，即本段要点的那个）
        # 原实现取 run_btn[0] ⇒ 点成多股票对比 ⇒ 它要求 ≥2 只股票，
        # 只弹 warning、从不设 compare_req ⇒ 下面断言恒为 None（死断言，
        # 实测确认）。这里按顺序取最后一个，并断言"恰好两个"以免再点错。
        run_btn = [b for b in at.button if b.label == "运行对比"]
        assert len(run_btn) == 2, (
            f"预期恰好 2 个「运行对比」按钮（多股票/参数组），实际 {len(run_btn)} 个"
            "—— app.py 布局变了，需重新确认该点哪个")
        run_btn[-1].click()
        at.run()
        comp = at.session_state["compare"] if "compare" in at.session_state else None
        print("对比结果:", None if comp is None else
              f"组数{len(comp['metrics'])} 指标行{len(comp['metrics'])}")
        assert comp is not None and len(comp["metrics"]) >= 1
    finally:
        # 必须 try/finally：若上面断言失败，快照会残留在生产文件
        # config/param_snapshots.json 里（H19 同类"测试污染生产"问题）
        if param_store.delete_snapshot("测试组A"):
            print("已清理测试快照「测试组A」")
        else:
            print("测试快照「测试组A」不存在（无需清理）")
    print("P1 参数组对比测试通过")

    # ── P1 HTML 报告导出 ──
    rep_btn = [b for b in at.button if b.label == "导出 HTML 报告"]
    assert rep_btn, "未找到导出按钮"
    rep_btn[0].click()
    at.run()
    from core import report
    reports = sorted(report.REPORTS_DIR.glob("*.html"))
    print("报告文件:", [p.name for p in reports])
    assert reports and reports[-1].stat().st_size > 10000
    print("P1 报告导出测试通过")

    # ── P1 数据增量更新：离线守卫必须拦下，且生产数据不得被改写 ──
    # （原实现在此真实拉取星耀 API —— 与观察期采集抢单点登录，H19）
    data_file = APP_DIR / "data" / "stock_601619_1m.csv"
    mtime_before = data_file.stat().st_mtime if data_file.exists() else None
    size_before = data_file.stat().st_size if data_file.exists() else None

    fetch_btn = [b for b in at.button if b.label == "拉取最新数据"]
    assert fetch_btn, "未找到拉取按钮"
    fetch_btn[0].click()
    at.run(timeout=300)

    mtime_after = data_file.stat().st_mtime if data_file.exists() else None
    size_after = data_file.stat().st_size if data_file.exists() else None
    print(f"拉取段: 异常={at.exception} mtime {mtime_before} -> {mtime_after}")

    # 1) 离线守卫生效：界面应显示"数据拉取失败"，而不是静默成功
    err_text = " ".join(e.value for e in at.error)
    assert not at.exception, at.exception
    assert "数据拉取失败" in err_text, (
        f"离线守卫未生效：界面未报拉取失败（errors={err_text!r}）"
        "—— 说明测试真的联网了")
    assert "H19" in err_text, f"未看到 H19 守卫标识（errors={err_text!r}）"

    # 2) 生产数据文件未被改写（这是本段存在的真正意义）
    assert mtime_before == mtime_after and size_before == size_after, (
        f"生产数据被测试改写！{data_file.name} "
        f"mtime {mtime_before}->{mtime_after} size {size_before}->{size_after}")
    print("P1 离线守卫 + 生产数据未改写：通过")

    # ── P0 「回测区间」控件必须真正生效（H20 回归防线）──
    # 背景：app.py:196 原写 isinstance(d_range, list)，而 st.date_input 返回的是
    # **tuple** ⇒ 两个三元分支恒走 else ⇒ start/end 永远是默认"近一年"，
    # 用户在界面上改区间**完全无效**（H20，生产功能缺陷）。
    # 断言锚真正用于回测的数据与权益曲线，缓存不应影响验收。
    import core.data as core_data
    import pandas as pd
    from datetime import date as _date
    _set_start, _set_end = _date(2024, 1, 1), _date(2024, 6, 30)
    ranges = [d for d in at.date_input if d.label == "回测区间"]
    assert len(ranges) == 1, "必须唯一定位侧栏「回测区间」，不能误改数据看板的日期控件"
    ranges[0].set_value((_set_start, _set_end))
    at.run()
    btn2 = [b for b in at.button if "开始回测" in (b.label or "")]
    assert btn2, "未找到开始回测按钮（区间变更后）"
    btn2[0].click()
    at.run()
    assert not at.exception, at.exception

    # 检查实际数据，不用缓存内部调用次数代替行为证据。
    actual = at.session_state["df"]
    expected, _ = core_data.load_data(at.session_state["preset_sel"],
                                     _set_start.isoformat(), _set_end.isoformat())
    assert len(actual) and len(expected), "目标区间无数据，不能验收日期控件"
    pd.testing.assert_frame_equal(actual.reset_index(drop=True), expected.reset_index(drop=True))
    eq_time = pd.to_datetime(at.session_state["result"].equity["time"])
    assert len(eq_time) and eq_time.min() >= pd.Timestamp(_set_start)
    assert eq_time.max() < pd.Timestamp(_set_end) + pd.Timedelta(days=1)
    print(f"回测区间段: 设定 {_set_start}~{_set_end}，实际 {len(actual)} 根行情与目标切片完全一致")
    print("P0 回测区间生效：通过")


@pytest.mark.slow
def test_app_ui(tmp_path, monkeypatch):
    """pytest 入口：包装原 main()（AppTest UI 冒烟，分钟级，进慢档）。"""
    from core import param_store, report
    monkeypatch.setattr(param_store, "SNAP_FILE", tmp_path / "param_snapshots.json")
    monkeypatch.setattr(report, "REPORTS_DIR", tmp_path / "reports")
    main()


if __name__ == "__main__":
    main()
