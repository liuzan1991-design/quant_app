# -*- coding: utf-8 -*-
"""P1-b：行情源可切换（QMT 分支）+ 四条规范化口径 + 失败回退 AD。

规范化口径依据：`D:\\Codex输出\\P1-b切源验收清单_20261024.md`（12 交易日实测）。
"""
from __future__ import annotations

import pandas as pd
import pytest

from core import data_fetch, qmt_kline


def _raw_day(dates: list[str], *, auction_vol=8871, first_vol=14656,
             tail_vol=1793, extra_tail_vol=21) -> pd.DataFrame:
    """构造 QMT 原始 1m（标签=bar 结束、量=手），足够覆盖四条口径。"""
    rows = []
    for d in dates:
        day = pd.Timestamp(d)
        # 竞价根：标签 09:30（+8h 时区→用 epoch 构造）
        def ms(ts):
            return int(pd.Timestamp(ts).tz_localize("Asia/Shanghai").tz_convert("UTC").value // 10**6)
        auction = day + pd.Timedelta(hours=9, minutes=30)
        first = day + pd.Timedelta(hours=9, minutes=31)
        rows += [
            {"time": ms(auction), "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
             "volume": auction_vol, "amount": 1_000_000.0},
            {"time": ms(first), "open": 100.5, "high": 101.0, "low": 100.2, "close": 100.8,
             "volume": first_vol, "amount": 2_000_000.0},
        ]
        # 注意：QMT 标签 = bar 终点 ⇒ 想要平移后落在 14:56/14:57，原始须为 14:57/14:58
        for i, (hm, px) in enumerate([("10:01", 101.5), ("14:57", 102.0), ("14:58", 102.5),
                                      ("15:00", 103.0)]):
            ts = day + pd.Timedelta(hours=int(hm[:2]), minutes=int(hm[3:]))
            v = tail_vol if hm == "14:57" else (extra_tail_vol if hm == "14:58" else 100)
            rows.append({"time": ms(ts), "open": px, "high": px + 0.5, "low": px - 0.5,
                         "close": px, "volume": v, "amount": px * 100 * v})
    return pd.DataFrame(rows)


def test_p1b_normalize_four_conventions() -> None:
    out = qmt_kline.normalize_qmt_1m(_raw_day(["2026-10-23"]))
    hm = out["time"].dt.strftime("%H:%M").tolist()
    assert hm[0] == "09:30" and hm[-1] == "14:59", "标签必须 −1 分钟且首末对齐 AD"
    assert "09:29" not in hm, "竞价根必须并入 09:30"

    r930 = out[out["time"].dt.strftime("%H:%M") == "09:30"].iloc[0]
    assert r930["open"] == pytest.approx(100.0), "09:30 open 取竞价根 open"
    assert r930["close"] == pytest.approx(100.8), "09:30 close 取首根连续分钟 close"
    assert r930["volume"] == pytest.approx((8871 + 14656) * 100), "量必须 ×100 并求和"
    assert r930["high"] == pytest.approx(101.0) and r930["low"] == pytest.approx(100.0), \
        "high/low 取两根极值（竞价根 low=100.0 更低）"

    r1456 = out[out["time"].dt.strftime("%H:%M") == "14:56"].iloc[0]
    r1457 = out[out["time"].dt.strftime("%H:%M") == "14:57"].iloc[0]
    assert r1456["close"] == pytest.approx(102.5), "H54：14:57 段必须并入 14:56 的 close"
    assert r1456["volume"] == pytest.approx((1793 + 21) * 100)
    assert r1457["volume"] == 0.0, "并入后必须补一根零量 14:57"


# ── 开关：默认仍走 AD ──
def test_p1b_default_source_is_ad(monkeypatch) -> None:
    monkeypatch.delenv(data_fetch._KLINE_SOURCE_ENV, raising=False)
    sentinel = pd.DataFrame({"time": [pd.Timestamp("2026-01-01")], "open": [1.0], "high": [1.0],
                             "low": [1.0], "close": [1.0], "volume": [0], "amount": [0.0]})
    monkeypatch.setattr(data_fetch, "_fetch_kline_inner", lambda *a, **k: sentinel)
    monkeypatch.setattr(qmt_kline, "fetch_qmt_kline",
                        lambda *a, **k: pytest.fail("默认源不应触碰 QMT"))
    got = data_fetch.fetch_kline("300308", "2026-01-01", "2026-01-01")
    assert got is sentinel


def test_p1b_qmt_source_used(monkeypatch) -> None:
    monkeypatch.setenv(data_fetch._KLINE_SOURCE_ENV, "qmt")
    sentinel = pd.DataFrame({"time": [pd.Timestamp("2026-01-01")], "open": [2.0], "high": [2.0],
                             "low": [2.0], "close": [2.0], "volume": [100], "amount": [200.0]})
    monkeypatch.setattr(qmt_kline, "fetch_qmt_kline", lambda *a, **k: sentinel)
    got = data_fetch.fetch_kline("300308", "2026-01-01", "2026-01-01")
    assert got is sentinel


# ── 回退：QMT 失败必须回落 AD，而不是空表 ──
def test_p1b_qmt_failure_falls_back_to_ad(monkeypatch, capsys) -> None:
    monkeypatch.setenv(data_fetch._KLINE_SOURCE_ENV, "qmt")
    monkeypatch.setattr(data_fetch, "_KLINE_WARNED", set())
    sentinel = pd.DataFrame({"time": [pd.Timestamp("2026-01-01")], "open": [3.0], "high": [3.0],
                             "low": [3.0], "close": [3.0], "volume": [0], "amount": [0.0]})
    monkeypatch.setattr(data_fetch, "_fetch_kline_inner", lambda *a, **k: sentinel)

    def _boom(*a, **k):
        raise ConnectionError("miniQMT 未就绪")

    monkeypatch.setattr(qmt_kline, "fetch_qmt_kline", _boom)
    got = data_fetch.fetch_kline("300308", "2026-01-01", "2026-01-01")
    assert got is sentinel, "必须回退 AD"
    assert "回退 AD" in capsys.readouterr().out


# ── 缺根必须告警（每日应 240 根）──
def test_p1b_gap_warns(monkeypatch, capsys) -> None:
    monkeypatch.setenv(data_fetch._KLINE_SOURCE_ENV, "qmt")
    monkeypatch.setattr(data_fetch, "_KLINE_WARNED", set())
    short = pd.DataFrame({"time": [pd.Timestamp("2026-01-01 09:30")], "open": [1.0], "high": [1.0],
                          "low": [1.0], "close": [1.0], "volume": [0], "amount": [0.0]})
    short.attrs["gaps"] = {"2026-01-01": 1}
    monkeypatch.setattr(qmt_kline, "fetch_qmt_kline", lambda *a, **k: short)
    data_fetch.fetch_kline("300308", "2026-01-01", "2026-01-01")
    assert "缺根" in capsys.readouterr().out


# ── 非 min1 周期：QMT 分支未实现，必须回落 AD ──
def test_p1b_non_min1_falls_back(monkeypatch) -> None:
    monkeypatch.setenv(data_fetch._KLINE_SOURCE_ENV, "qmt")
    monkeypatch.setattr(data_fetch, "_KLINE_WARNED", set())
    sentinel = pd.DataFrame({"time": [pd.Timestamp("2026-01-01")], "open": [4.0], "high": [4.0],
                             "low": [4.0], "close": [4.0], "volume": [0], "amount": [0.0]})
    monkeypatch.setattr(data_fetch, "_fetch_kline_inner", lambda *a, **k: sentinel)
    got = data_fetch.fetch_kline("300308", "2026-01-01", "2026-01-01", period="day")
    assert got is sentinel


# ── H72：`date` 对象不能被静默吃掉（2026-10-28 实测踩坑）────────────────────
def test_p1b_accepts_date_object() -> None:
    """`beijing_today()` 返回 `datetime.date`，而 `date.replace('-', '')` 会抛
    "TypeError: 'str' object cannot be interpreted as an integer"（2026-10-28 实测踩到）。
    这里钉住 `_yyyymmdd`：date / datetime / Timestamp / str 四种入参都得转成 YYYYMMDD。
    """
    import datetime as _dt
    f = qmt_kline._yyyymmdd
    assert f(_dt.date(2026, 1, 1)) == "20260101"
    assert f(_dt.datetime(2026, 1, 2, 15, 30)) == "20260102"
    assert f(pd.Timestamp("2026-03-04 09:30")) == "20260304"
    assert f("2026-03-05") == "20260305", "字符串行为必须与历史完全一致"
