# -*- coding: utf-8 -*-
"""UI 打磨后快速验证：渲染 + 回测 + 指标卡。"""
from streamlit.testing.v1 import AppTest


def main():
    at = AppTest.from_file(r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app\app.py",
                           default_timeout=240)
    at.run()
    print("渲染异常:", at.exception)
    assert not at.exception, at.exception

    btn = [b for b in at.button if "开始回测" in (b.label or "")]
    btn[0].click()
    at.run()
    print("回测后异常:", at.exception)
    assert not at.exception, at.exception

    res = at.session_state["result"] if "result" in at.session_state else None
    assert res is not None
    print("指标卡数据:", f"总收益{res.total_return*100:+.2f}% "
          f"回撤{res.max_drawdown*100:.2f}% 交易{len(res.trades)}笔")

    # 切换策略到"网格交易"再回测
    strat = [s for s in at.selectbox if s.label == "策略"]
    assert strat, "未找到策略下拉框"
    strat[0].set_value("grid_trade")
    at.run()
    btn2 = [b for b in at.button if "开始回测" in (b.label or "")]
    btn2[0].click()
    at.run()
    print("网格策略回测后异常:", at.exception)
    assert not at.exception, at.exception
    res2 = at.session_state["result"] if "result" in at.session_state else None
    assert res2 is not None and res2.strategy_id == "grid_trade"
    print("网格策略回测:", f"总收益{res2.total_return*100:+.2f}% "
          f"交易{len(res2.trades)}笔")

    # 切换策略到"大盘情绪做T"再回测（会拉指数/行业数据）
    strat_new = [s for s in at.selectbox if s.label == "策略"]
    strat_new[0].set_value("sentiment_t")
    at.run()
    print("调试: 切换后策略下拉值 =", [s.value for s in at.selectbox if s.label == "策略"])
    btn3 = [b for b in at.button if "开始回测" in (b.label or "")]
    btn3[0].click()
    at.run()
    print("情绪做T回测后异常:", at.exception)
    assert not at.exception, at.exception
    res3 = at.session_state["result"] if "result" in at.session_state else None
    run_sig = at.session_state["run_sig"] if "run_sig" in at.session_state else "无"
    print("调试: res3 strategy_id =", None if res3 is None else res3.strategy_id,
          "| run_sig 前40 =", str(run_sig)[:40])
    assert res3 is not None and res3.strategy_id == "sentiment_t"
    print("情绪做T回测:", f"总收益{res3.total_return*100:+.2f}% "
          f"交易{len(res3.trades)}笔")

    # 新增：均线波段插件也应能由同一策略下拉框动态加载并完成回测
    strat_swing = [s for s in at.selectbox if s.label == "策略"]
    strat_swing[0].set_value("ma_swing")
    at.run()
    btn4 = [b for b in at.button if "开始回测" in (b.label or "")]
    btn4[0].click()
    at.run()
    print("均线波段回测后异常:", at.exception)
    assert not at.exception, at.exception
    res4 = at.session_state["result"] if "result" in at.session_state else None
    assert res4 is not None and res4.strategy_id == "ma_swing"
    print("均线波段回测:", f"总收益{res4.total_return*100:+.2f}% "
          f"交易{len(res4.trades)}笔")
    print("UI 快速验证通过")


if __name__ == "__main__":
    main()
