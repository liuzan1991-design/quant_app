# -*- coding: utf-8 -*-
"""QMT 分钟线导入（H61 配套，2026-10-27）——把 xtdata 的 1m 数据转成 AD 口径的 CSV。

## 为什么需要它

AmazingData 单点登录，与 live 采集/盘后管道共用一条会话 ⇒ 补历史时容易与它们互踢。
QMT（xtdata）是**另一个数据源**，可离线取数，适合在 AD 忙的时候补 301717 这类新标的。

## 口径换算（2026-10-27 用 301717 逐根实测得出，不是猜）

| 项 | QMT | AD / 本项目 CSV |
|---|---|---|
| 时间标签 | **bar 结束**，且按**本机时区**渲染（本机 GMT+3） | **bar 开始**，北京时 |
| 每日根数 | **241**（含集合竞价单独一根） | **240**（竞价并入 09:30） |
| 成交量 | **手** | **股** |

换算规则：
1. 标签按本机时区解析 → 转北京时 → **减 1 分钟**（结束→开始）；
2. 每日把「09:29(=竞价)」与「09:30」两根合并进 09:30 一根（open 取首根、close 取末根、high/low 取极值、量额相加）；
3. 成交量 ×100（手→股）。AD 保留小数股，QMT 只有整手 ⇒ 单根最多相差 100 股（≈0.01%），已实测。

实测校验（301717 2026-08-11）：QMT 04:30+04:31 合并 ⇒ AD 09:30 的
`open=450 / high=460 / low=420 / close=440 / amount=1,267,554,218` **完全一致**，量差 2 股。

用法：
    python scripts/tool_qmt_minute_import.py 301717 --start 20260810 --end 20261027 \
        --out data/stock_301717_1m.csv [--no-download]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

BJ = ZoneInfo("Asia/Shanghai")
LOCAL_TZ = datetime.now().astimezone().tzinfo


def _symbol(code: str) -> str:
    code = code.strip().split(".")[0]
    if code.startswith("68"):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _labels_to_beijing(labels) -> pd.Series:
    """QMT 形如 20260811043000 的标签（本机时区）→ 北京时 naive（**保持原索引**）。"""
    out = []
    for s in labels:
        s = str(int(s)) if not isinstance(s, str) else s
        s = s.ljust(14, "0")
        naive_local = datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=LOCAL_TZ)
        out.append(naive_local.astimezone(BJ).replace(tzinfo=None))
    # 索引必须与输入一致，否则赋值对齐会全部变 NaT（2026-10-27 踩过）
    return pd.Series(pd.to_datetime(out), index=list(labels))


def convert(raw: pd.DataFrame) -> pd.DataFrame:
    """QMT 原始 df → AD 口径（240 根/日、bar 开始标签、量为股）。"""
    df = raw.copy().reset_index(drop=True)
    df["time"] = (_labels_to_beijing(raw.index) - pd.Timedelta(minutes=1)).to_numpy()
    df = df.sort_values("time").reset_index(drop=True)
    df["_hhmm"] = df["time"].dt.strftime("%H:%M")
    df["_day"] = df["time"].dt.strftime("%Y-%m-%d")

    rows = []
    for _day, g in df.groupby("_day", sort=True):
        auc = g[g["_hhmm"] == "09:29"]
        body = g[g["_hhmm"] != "09:29"].copy()
        if len(auc) and len(body):
            # 集合竞价那根并入当日 09:30：open 取竞价、high/low 取极值、量额相加、
            # close 保持 09:30 那根（与 AD 口径实测一致）
            i0 = body.index[0]
            a = auc.iloc[0]
            body.loc[i0, "open"] = a["open"]
            body.loc[i0, "high"] = max(a["high"], body.loc[i0, "high"])
            body.loc[i0, "low"] = min(a["low"], body.loc[i0, "low"])
            body.loc[i0, "volume"] = a["volume"] + body.loc[i0, "volume"]
            body.loc[i0, "amount"] = a["amount"] + body.loc[i0, "amount"]
        rows.append(body)

    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["time", "open", "high", "low", "close", "volume", "amount"])
    out["volume"] = (out["volume"] * 100).round().astype("int64")   # 手 → 股
    out = out[["time", "open", "high", "low", "close", "volume", "amount"]]
    out = out.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    # 剔除午休区间（11:31~12:59 不应有 bar）
    hhmm = out["time"].dt.strftime("%H:%M")
    return out[~((hhmm > "11:30") & (hhmm < "13:00"))].reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="QMT 1m → AD 口径 CSV")
    ap.add_argument("code")
    ap.add_argument("--start", default="20260801")
    ap.add_argument("--end", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-download", action="store_true")
    args = ap.parse_args()

    from xtquant import xtdata  # 延迟导入：无 QMT 环境下不影响其它用法

    sym = _symbol(args.code)
    end = args.end or datetime.now().strftime("%Y%m%d")
    if not args.no_download:
        xtdata.download_history_data(sym, period="1m", start_time=args.start, end_time=end)
    raw = xtdata.get_market_data_ex([], [sym], period="1m",
                                    start_time=args.start, end_time=end).get(sym)
    if raw is None or not len(raw):
        print("❌ QMT 无数据")
        return 2

    out = convert(raw)
    if out.empty:
        print("❌ 转换后为空")
        return 2

    counts = out.groupby(out["time"].dt.strftime("%Y-%m-%d")).size()
    print(f"标的 {sym} | 原始 {len(raw)} 根 → 转换后 {len(out)} 根 | "
          f"交易日 {len(counts)} | 每日根数分布 {counts.value_counts().to_dict()}")
    print(f"范围 {out['time'].iloc[0]} ~ {out['time'].iloc[-1]}")

    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(target, index=False, encoding="utf-8")
    print(f"已写入 {target}（{target.stat().st_size/1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
