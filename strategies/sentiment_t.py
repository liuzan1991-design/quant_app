# -*- coding: utf-8 -*-
"""大盘情绪做T：指数强弱（strong/weak/crash）+ 板块情绪过滤的增强版日内做T。"""
import sys
from pathlib import Path

import pandas as pd

from strategies.intraday_t import IntradayTStrategy
from strategies.base import BacktestResult, ParamMeta
from core import sentiment_data

ENGINE_DIR = Path(__file__).resolve().parents[2]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))
import backtest_generic as bg  # noqa: E402
from core.adjustment import prepare_signal_prices


class SentimentTStrategy(IntradayTStrategy):
    id = "sentiment_t"
    name = "大盘情绪做T"
    description = "分析指数强弱与板块情绪后再做T：大盘暴跌禁买、弱势只卖不买、板块弱更积极卖出"

    param_meta = list(IntradayTStrategy.param_meta) + [
        ParamMeta("index_code", "大盘指数", "399006.SZ", "select",
                  options=list(sentiment_data.INDEX_POOL.keys()),
                  group="risk",
                  help="用于判断大盘强弱的指数（默认创业板指，代表市场情绪）"),
        ParamMeta("use_sector", "启用板块过滤", True, "checkbox", group="risk",
                  help="个股所属行业指数（申万一级）5日/20日均线判断板块强弱，板块弱时更积极卖出"),
        ParamMeta("trend_mode", "趋势自适应(持股/做T)", True, "checkbox", group="risk",
                  help="个股日线多头排列(收>MA20且MA5>MA10>MA20)时切持股模式不做T，防卖飞；空头排列时禁买"),
        ParamMeta("trend_ma", "趋势均线周期", 20, "number", 5, 60, 1, group="risk",
                  help="趋势判断用多少日均线，越大越不敏感"),
    ]

    def run(self, df: pd.DataFrame, init_cash: float, params: dict,
            context: dict = None) -> BacktestResult:
        p = dict(params)
        code = (context or {}).get("code", "")
        prepared, corporate_dates, adjustment = prepare_signal_prices(df, code)
        prepared.attrs["adjustment"] = adjustment
        prepared.attrs["corporate_action_dates"] = corporate_dates
        start = pd.Timestamp(prepared["time"].iloc[0]).strftime("%Y-%m-%d")
        end = pd.Timestamp(prepared["time"].iloc[-1]).strftime("%Y-%m-%d")

        # 大盘状态（指数1分钟 → 每bar status/chg）
        index_code = p.get("index_code", "399006.SZ")
        try:
            idx = sentiment_data.load_index_min(index_code, start, end)
            status_map, chg_map = sentiment_data.compute_market_status(idx)
        except Exception as e:  # noqa: BLE001
            status_map, chg_map = {}, {}
            print(f"[情绪做T] 大盘数据获取失败，降级为无过滤：{e}")

        # 板块状态（个股→申万一级行业→行业指数日线）
        if p.get("use_sector", True) and code:
            try:
                ind_code = sentiment_data.get_stock_industry_index(code)
                if ind_code:
                    ind_daily = sentiment_data.load_industry_daily(ind_code, start, end)
                    sector = sentiment_data.compute_sector_status(ind_daily)
                    if sector == "weak" and status_map:
                        status_map = {ts: ("weak" if st != "crash" else st)
                                      for ts, st in status_map.items()}
                    elif sector == "strong" and status_map:
                        status_map = {ts: ("strong" if st in ("flat", "strong") else st)
                                      for ts, st in status_map.items()}
            except Exception as e:  # noqa: BLE001
                print(f"[情绪做T] 板块数据获取失败：{e}")

        # 个股趋势模式：hold/defense/range
        mode_map = None
        if p.get("trend_mode", True):
            try:
                trend_map = sentiment_data.compute_trend_mode(
                    prepared, ma_trend=int(p.get("trend_ma", 20)))
                mode_map = trend_map
                if status_map:
                    for ts in list(status_map.keys()):
                        day = pd.Timestamp(ts).date()
                        if trend_map.get(day) == "defense" and status_map[ts] != "crash":
                            status_map[ts] = "weak"
            except Exception as e:  # noqa: BLE001
                print(f"[情绪做T] 趋势模式计算失败：{e}")

        p["stop_loss_pct"] = {
            "low": p.pop("stop_loss_low"),
            "normal": p.pop("stop_loss_normal"),
            "high": p.pop("stop_loss_high"),
        }
        acc, trades, eq = bg.run_backtest(
            prepared, init_cash, p,
            index_status_map=status_map if status_map else None,
            index_change_pct_map=chg_map if chg_map else None,
            mode_map=mode_map)

        return self._build_result(acc, trades, eq, prepared, init_cash, p)
