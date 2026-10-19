#!/usr/bin/env python
"""AD 分钟线「缺口探针」—— 定位缺失分钟，并用 QMT 交叉校验其性质（只读）。

## 为什么需要它（2026-10-19 立）

10-12（D19）AD 只采到 236 根、缺 09:39-09:42。当时的定性是「AD 源端缺、非本机丢失」，
由此被默认为「缺口不可避免 / 市场无成交」。2026-10-19 用 QMT 逐根核对发现：
**这 4 分钟在 QMT 侧有完整真实成交**（量 1458/1149/792/1143 手）。
⇒ 那是个 §6.9 第八形态：否定性断言「源端没有」**只扫了 AD、没扫 QMT**。

所以收盘七件套 ⑥ 增加一条自动触发规则：
**AD 当日根数 < 240 时，必须跑本脚本判定缺口性质**，不得停在「源端缺」这一步。

## 判定口径

| QMT 对应根 | 判定 | 含义 |
|---|---|---|
| 存在且 volume > 0 | **AD 侧缺失** | 市场有成交、AD 没给 ⇒ 可用 QMT 补（是否回补另议） |
| 存在且 volume = 0 | 市场无成交 | AD 与 QMT 一致为空 ⇒ 正常，非缺失 |
| 不存在 | 两源均缺 | 需查是否停牌/半日市 |

## 三条硬口径（与 tool_qmt_ad_reconcile.py 完全一致）

1. **AD 标签 = bar 开始，QMT 标签 = bar 结束** ⇒ `AD[t] ↔ QMT[t+1min]`。
2. **QMT 索引按本机时区渲染** ⇒ 一律用 `time` 字段（epoch）换算北京时间，**禁止硬编码 ±5h**。
3. **量**：QMT 单位 = 手，AD = 股 ⇒ `AD量 ≈ QMT量 × 100`。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 无缺口；或缺口全部是「市场无成交」 |
| 2 | QMT 前置自检未过（未做校验，需先登录极简版） |
| 3 | 存在「AD 侧缺失」—— 值得关注 |
| 4 | 存在「两源均缺」 |
| 5 | 本地 CSV 当日无数据 |

用法：
    D:/Anaconda3/python.exe scripts/tool_gap_probe.py
    D:/Anaconda3/python.exe scripts/tool_gap_probe.py --date 2026-10-12
    D:/Anaconda3/python.exe scripts/tool_gap_probe.py --no-qmt      # QMT 没开时只看缺口
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_qmt_ad_reconcile import (  # noqa: E402  复用同一口径，避免二次实现漂移
    fmt_ts,
    load_ad_side,
    load_qmt_side,
    preflight_qmt,
    to_qmt_code,
)

# AD 交易分钟轴：09:30-11:29 + 13:00-14:59 = 240 根（无 11:30 / 15:00 标签）
EXPECTED_BARS = 240


def build_session_axis() -> list[str]:
    """返回 ['09:30', '09:31', ..., '11:29', '13:00', ..., '14:59']。"""
    out = []
    for start, end in ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)):
        out += [f"{t // 60:02d}:{t % 60:02d}" for t in range(start, end)]
    return out


def last_date_in_csv(local_csv: Path) -> str:
    df = pd.read_csv(local_csv)
    df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
    tcol = "time" if "time" in df.columns else df.columns[0]
    ts = pd.to_datetime(df[tcol], errors="coerce").dropna()
    if ts.empty:
        raise ValueError(f"{local_csv} 无可用时间戳")
    return ts.max().strftime("%Y-%m-%d")


def ohcl_equal(a: dict, q: pd.Series, tol: float = 1e-6) -> tuple[bool, dict]:
    """比较 AD 一根与 QMT 一根的 OHLC + 量（AD量应 ≈ QMT量×100）。"""
    detail, ok = {}, True
    for f in ("open", "high", "low", "close"):
        av, qv = float(a[f]), float(q[f])
        same = abs(av - qv) <= tol
        detail[f] = (av, qv, same)
        ok &= same
    av, qv = float(a["volume"]), float(q["volume"]) * 100.0
    # 量的容差：AD 可能对零股取整，允许 100 股内误差
    same_v = abs(av - qv) <= 100.0
    detail["volume"] = (av, qv, same_v)
    ok &= same_v
    return ok, detail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="AD 分钟线缺口探针（定位缺口 + QMT 交叉校验，只读）")
    ap.add_argument("--code", default="300308", help="6 位代码，默认 300308")
    ap.add_argument("--date", default=None,
                    help="交易日 YYYY-MM-DD；默认取本地 CSV 的末根日")
    ap.add_argument("--local-csv", default="data/live/stock_{code}_1m.csv",
                    help="本地 AD CSV；默认 data/live/…（🚨 不读静态池 data/…）")
    ap.add_argument("--no-qmt", action="store_true",
                    help="只列缺口、不查 QMT（QMT 未开时用）")
    args = ap.parse_args(argv)

    local_csv = Path(args.local_csv.format(code=args.code))
    if not local_csv.exists():
        print(f"❌ 本地 CSV 不存在：{local_csv}")
        return 5

    date = args.date or last_date_in_csv(local_csv)
    print("=" * 74)
    print(f"AD 分钟线缺口探针   标的={args.code}   日期={date}")
    print("=" * 74)
    print(f"本地 CSV : {local_csv}")

    try:
        ad = load_ad_side(args.code, local_csv, date)
    except Exception as exc:
        print(f"❌ 读取失败：{type(exc).__name__}: {exc}")
        return 5

    have = {t.strftime("%H:%M") for t in ad["adm_time"]}
    axis = build_session_axis()
    gaps = [t for t in axis if t not in have]
    extra = sorted(have - set(axis))

    print(f"AD 根数  : {len(ad)} / 应有 {EXPECTED_BARS}")
    print(f"首根/末根: {fmt_ts(ad['adm_time'].iloc[0])} / {fmt_ts(ad['adm_time'].iloc[-1])}")
    if extra:
        print(f"⚠️ 轴外时间戳（{len(extra)} 个）：{extra}")

    if not gaps:
        print("\n✅ 240/240 无缺口 —— 无需交叉校验。")
        return 0

    print(f"\n🔴 缺口 {len(gaps)} 根：{gaps[0]} ~ {gaps[-1]}（{len(gaps)}/{EXPECTED_BARS}"
          f" = {len(gaps) / EXPECTED_BARS:.2%}）")
    ranges, cur = [], [gaps[0]]
    for prev, nxt in zip(gaps, gaps[1:]):
        if (datetime.strptime(nxt, "%H:%M") - datetime.strptime(prev, "%H:%M")) == timedelta(minutes=1):
            cur.append(nxt)
        else:
            ranges.append(cur)
            cur = [nxt]
    ranges.append(cur)
    print("   连续缺口段：" + "；".join(f"{r[0]}~{r[-1]}({len(r)}根)" for r in ranges))

    if args.no_qmt:
        print("\n（--no-qmt：仅列缺口，未做交叉校验）")
        return 3

    qmt_code = to_qmt_code(args.code)
    print("\n── QMT 前置自检 ──")
    if not preflight_qmt(qmt_code):
        print("⇒ ❌ 自检未过，无法判定缺口性质（先开并登录 QMT 极简版后重跑）")
        return 2

    qmt, diag = load_qmt_side(qmt_code, date)
    print(f"\n── QMT 侧 ──\n  根数 = {len(qmt)}   时间轴单位 = {diag.get('time_unit')}")

    qidx = qmt.set_index("qmt_dt")

    print("\n── 逐根判定（AD[t] ↔ QMT[t+1min]）──")
    print(f"{'AD分钟':<8}{'QMT分钟':<9}{'open':>9}{'high':>9}{'low':>9}{'close':>9}{'量(手)':>9}   判定")
    print("-" * 74)
    verdict = {"AD侧缺失": [], "市场无成交": [], "两源均缺": []}
    for g in gaps:
        ad_t = datetime.strptime(f"{date} {g}", "%Y-%m-%d %H:%M")
        q_t = ad_t + timedelta(minutes=1)
        if q_t not in qidx.index:
            verdict["两源均缺"].append(g)
            print(f"{g:<8}{q_t.strftime('%H:%M'):<9}{'—':>9}{'—':>9}{'—':>9}{'—':>9}{'—':>9}   ⚠️ 两源均缺")
            continue
        q = qidx.loc[q_t]
        vol = float(q["volume"])
        kind = "AD侧缺失" if vol > 0 else "市场无成交"
        verdict[kind].append(g)
        mark = "🔴" if vol > 0 else "✅"
        print(f"{g:<8}{q_t.strftime('%H:%M'):<9}{float(q['open']):>9.2f}{float(q['high']):>9.2f}"
              f"{float(q['low']):>9.2f}{float(q['close']):>9.2f}{vol:>9.0f}   {mark} {kind}")

    # 边界互证：缺口外侧紧邻的 2 根（前一根 / 后一根），证明平移关系在缺口两侧都成立
    print("\n── 边界互证（缺口外侧各 1 根，验证 AD[t]=QMT[t+1] 平移关系）──")
    bounds = []
    i0, i1 = axis.index(gaps[0]), axis.index(gaps[-1])
    if i0 > 0 and axis[i0 - 1] in have:
        bounds.append(("前", axis[i0 - 1]))
    if i1 + 1 < len(axis) and axis[i1 + 1] in have:
        bounds.append(("后", axis[i1 + 1]))
    if not bounds:
        print("  （缺口贴边，外侧无可用根）")
    for tag, t in bounds:
        ad_t = datetime.strptime(f"{date} {t}", "%Y-%m-%d %H:%M")
        q_t = ad_t + timedelta(minutes=1)
        row = ad[ad["adm_time"] == ad_t]
        if q_t not in qidx.index or row.empty:
            print(f"  {tag} {t}：QMT 侧无对应根，无法互证")
            continue
        ok, detail = ohcl_equal(row.iloc[0].to_dict(), qidx.loc[q_t])
        print(f"  {tag} {t} ↔ QMT {q_t.strftime('%H:%M')}：{'✅ 逐位吻合' if ok else '⚠️ 存在差异'}")
        if not ok:
            for f, (av, qv, same) in detail.items():
                if not same:
                    print(f"      {f}: AD={av}  QMT={qv}  (QMT×100 for volume)")

    print("\n" + "=" * 74)
    print("结论：")
    if verdict["AD侧缺失"]:
        print(f"  🔴 AD 侧缺失 {len(verdict['AD侧缺失'])} 根 —— QMT 有真实成交：{verdict['AD侧缺失']}")
        print("     ⇒ 缺口属 AD 数据缺失，非市场无成交。是否回补另议（混源会破坏单一源一致性）。")
    if verdict["市场无成交"]:
        print(f"  ✅ 市场无成交 {len(verdict['市场无成交'])} 根：{verdict['市场无成交']}（非缺失）")
    if verdict["两源均缺"]:
        print(f"  ⚠️ 两源均缺 {len(verdict['两源均缺'])} 根：{verdict['两源均缺']}（查停牌/半日市）")
    print("=" * 74)

    if verdict["两源均缺"]:
        return 4
    if verdict["AD侧缺失"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
