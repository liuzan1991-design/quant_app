import numpy as np
import pandas as pd

from core import sentiment_data
from core.paper_broker import PaperBroker
from core.paper_models import OrderSide
from core.sentiment_t_shared_live import SentimentTSharedLiveEngine
from core.intraday_t_shared import IntradayTSharedEngine


SYMBOL = "300308.SZ"


def scenario(monkeypatch):
    days = pd.bdate_range("2026-09-01", periods=23)
    target = days[-2] + pd.Timedelta(hours=14, minutes=30)
    rows = []
    for i, day in enumerate(days):
        for minute, close in [(f"09:{30+j}", 110) for j in range(12)] + [
                ("14:30", 97), ("14:59", 100+i)]:
            rows.append({"time": pd.Timestamp(f"{day.date()} {minute}"), "open": close,
                         "close": close, "high": close, "low": close,
                         "volume": 10000, "amount": close*10000})
    df = pd.DataFrame(rows)
    index = df[["time", "open", "close", "volume", "amount"]].copy()
    index["open"] = index["close"] = 100.0
    index["amount"] = index["volume"] * 100.0
    industry = pd.DataFrame({"time": days, "close": 100.0+np.arange(len(days))})
    monkeypatch.setattr(sentiment_data, "load_index_min", lambda *args: index)
    monkeypatch.setattr(sentiment_data, "get_stock_industry_index", lambda _: "industry")

    def prices(frame, code):
        d = frame.copy()
        for name in ("close", "high", "low"):
            d[f"signal_{name}"] = d[name]
        return d, [], "synthetic"

    monkeypatch.setattr("core.sentiment_t_shared_live.prepare_signal_prices", prices)
    return df, industry, target


def run_at(df, industry, target, monkeypatch):
    monkeypatch.setattr(sentiment_data, "load_industry_daily", lambda *args: industry)
    engine = SentimentTSharedLiveEngine(SYMBOL, {"base_position": 0}, df=df, broker_driven=True)
    monkeypatch.setattr(engine.engine, "_evaluate_buy", lambda *_: (True, 10, "试算", {}))
    broker = PaperBroker(300000, target.date().isoformat())
    row = df.loc[df.time == target].iloc[0]
    signals = tuple(engine.on_bar(row, broker))
    return engine, signals


def test_same_and_future_daily_close_changes_cannot_change_a_past_buy(monkeypatch):
    df, sector, ts = scenario(monkeypatch)
    first, signals = run_at(df, sector, ts, monkeypatch)
    changed = df.copy()
    changed.loc[changed.time > ts, ["open", "close", "high", "low"]] = 50
    changed_sector = sector.copy()
    changed_sector.loc[changed_sector.time.dt.date >= ts.date(), "close"] = [70, 60]
    other, again = run_at(changed, changed_sector, ts, monkeypatch)
    assert len(signals) == 1 and signals[0].side == OrderSide.BUY
    assert again == signals
    assert first.engine._day_mode == other.engine._day_mode == "hold"
    assert first.engine._bar_index_status == other.engine._bar_index_status == "strong"


def test_unfinished_stock_session_and_stale_sector_do_not_drive_next_day(monkeypatch):
    df, sector, ts = scenario(monkeypatch)
    prior = df[df.time.dt.date < ts.date()].time.dt.date.iloc[-1]
    df = df[~((df.time.dt.date == prior) & (df.time.dt.hour == 14) & (df.time.dt.minute == 59))]
    stale_sector = sector[sector.time.dt.date < prior]
    engine, signals = run_at(df, stale_sector, ts, monkeypatch)
    assert engine.engine._day_mode == "range"
    assert engine.engine._bar_index_status == "flat"
    assert signals == ()


def test_new_completed_close_becomes_available_only_next_session(monkeypatch):
    df, sector, ts = scenario(monkeypatch)
    close_bar = df[df.time < ts.normalize()].iloc[-1]
    monkeypatch.setattr(sentiment_data, "load_industry_daily", lambda *args: sector)
    engine = SentimentTSharedLiveEngine(SYMBOL, {"base_position": 0},
                                        df=df[df.time < close_bar.time], broker_driven=True)
    broker = PaperBroker(300000, close_bar.time.date().isoformat())
    assert close_bar.time.date() not in engine._stock_closes
    engine.on_bar(close_bar, broker)
    assert close_bar.time.date() in engine._stock_closes
    engine._set_causal_context(ts)
    assert engine.engine._day_mode == "hold"


def test_sector_series_matches_single_close_calculation():
    daily = pd.DataFrame({"time": pd.date_range("2026-08-01", periods=28),
                          "close": np.r_[np.linspace(100, 120, 21), [121, 119, 118, 120, 110, 115, 111]]})
    by_date = sentiment_data.sector_status_by_close(daily)
    for i, ts in enumerate(daily.time):
        assert by_date[ts.date()] == sentiment_data.compute_sector_status(daily.iloc[:i+1])


def test_ablation_switches_remove_only_targeted_scores_and_context():
    p = {"sell_threshold": 7, "buy_threshold": 6, "buy_back_gap_pct": .005}
    indicator = {"closes": np.full(30, 100.), "volumes": np.ones(30),
                 "macd_tail": np.zeros(6), "vol_level": "normal", "rsi": 50,
                 "sig_high": True, "sig_low": True}
    full = IntradayTSharedEngine(p)
    without_nine = IntradayTSharedEngine({**p, "enable_nine_turn": False})
    without_rebuy = IntradayTSharedEngine({**p, "enable_sell_buyback": False})
    for engine in (full, without_nine, without_rebuy):
        engine.state.sell_lowest_price = 102
    assert full._evaluate_sell(indicator, None)[1] - without_nine._evaluate_sell(indicator, None)[1] == 10
    assert full._evaluate_buy(indicator, 100)[1] - without_nine._evaluate_buy(indicator, 100)[1] == 10
    assert full._evaluate_buy(indicator, 100)[1] - without_rebuy._evaluate_buy(indicator, 100)[1] == 3
    neutral = IntradayTSharedEngine({**p, "enable_sentiment_filter": False})
    neutral.set_context("crash", -3.0, "hold")
    assert (neutral._bar_index_status, neutral._bar_index_change_pct, neutral._day_mode) == ("flat", 0.0, "range")
