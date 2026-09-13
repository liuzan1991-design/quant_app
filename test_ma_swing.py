# -*- coding: utf-8 -*-
"""均线波段策略冒烟测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.data import load_data  # noqa: E402
from strategies import get_strategy  # noqa: E402


def main() -> int:
    strategy = get_strategy("ma_swing")
    assert strategy.id == "ma_swing"
    ran = 0
    for code in ("601619", "688256"):
        df, _ = load_data(code)
        if df.empty:
            print(code, "无数据，跳过")
            continue
        result = strategy.run(df, 1_000_000.0, strategy.default_params(), {"code": code})
        assert len(result.equity) == len(df)
        assert result.final_equity > 0
        ran += 1
        print(f"{code} {strategy.name}: 收益 {result.total_return*100:+.2f}% | "
              f"回撤 {result.max_drawdown*100:.2f}% | 交易 {len(result.trades)} 笔")
    print("均线波段策略冒烟测试通过")
    return ran


def test_ma_swing_smoke():
    """pytest 入口：包装原 main()。

    额外断言"至少验证了一个标的"——原脚本在数据全缺时会静默通过（假绿），
    包装后数据缺失将直接失败，避免回归里出现"看着跑了其实没验"。
    """
    assert main() >= 1, "601619/688256 均无数据，测试实际未验证任何内容"


if __name__ == "__main__":
    main()
