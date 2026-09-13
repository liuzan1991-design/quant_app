# -*- coding: utf-8 -*-
"""选股进池：为候选股票拉取分钟数据 + 复权因子，验数后记入观察池。

链路
----
    候选清单 → 拉数 → 验数 → 进池

- 分钟数据：复用 `core.data_fetch.incremental_update`（含 45 秒登录超时保护）
- 复权因子：复用 `core.adjustment.load_backward_factor`（自动拉取 + 缓存）
- 本脚本**只做「编排 + 验数 + 记账」**，不重写任何拉取逻辑。

安全边界（重要）
----------------
会占用星耀 AmazingData **单点登录**。必须避开观察期采集窗口
（本机 04:30–10:10 = 北京 09:30–15:10），否则会踢掉当天采集进程。
脚本启动时会做窗口检查，命中则拒绝执行（可用 --force 覆盖，不推荐）。

排除北交所（43/83/87/88/92 开头）：现有策略不支持北交所。

用法
----
    # 1) 先干跑，只看计划，不登录
    python scripts/pool_fetch.py --codes 002084 300950 --dry-run

    # 2) 真拉（确认在非采集窗口）
    python scripts/pool_fetch.py --codes 002084 300950 --start 2023-01-01

    # 3) 从选股结果 CSV 取前 N 只（CSV 需含 code 列）
    python scripts/pool_fetch.py --from-csv D:\\Codex输出\\选股.csv --top 5
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from core import data_fetch  # noqa: E402
from core.adjustment import load_backward_factor  # noqa: E402

BSE_PREFIXES = ("43", "83", "87", "88", "92")   # 北交所，策略不支持
COLLECT_WINDOW = (4, 30, 10, 15)                # 本机采集窗口 04:30–10:15，避开


def in_collect_window(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    h, m = now.hour, now.minute
    cur = h * 60 + m
    return COLLECT_WINDOW[0] * 60 + COLLECT_WINDOW[1] <= cur <= COLLECT_WINDOW[2] * 60 + COLLECT_WINDOW[3]


def normalize_codes(raw: list[str]) -> tuple[list[str], list[str]]:
    """返回 (可用代码, 被拒绝的说明)。"""
    ok, rejected = [], []
    seen = set()
    for c in raw:
        c = str(c).strip().upper().replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
        if not c.isdigit() or len(c) != 6:
            rejected.append(f"{c}: 非法代码格式")
            continue
        if c.startswith(BSE_PREFIXES):
            rejected.append(f"{c}: 北交所，策略不支持")
            continue
        if c in seen:
            continue
        seen.add(c)
        ok.append(c)
    return ok, rejected


def verify(df: pd.DataFrame, factor: pd.Series | None) -> dict:
    """验数：不是「拉到就算成功」，要能回答「数据够不够、全不全」。"""
    out = {"bars": 0, "first": None, "last": None, "days": 0,
           "thin_days": 0, "thin_ratio": 0.0, "factor_days": 0,
           "factor_aligned": False, "ok": False, "issues": []}
    if df is None or not len(df):
        out["issues"].append("无数据")
        return out

    d = df.copy()
    d["time"] = pd.to_datetime(d["time"])
    d = d.sort_values("time")
    per_day = d.groupby(d["time"].dt.normalize()).size()

    out["bars"] = len(d)
    out["first"] = d["time"].iloc[0].strftime("%Y-%m-%d %H:%M")
    out["last"] = d["time"].iloc[-1].strftime("%Y-%m-%d %H:%M")
    out["days"] = int(per_day.size)
    # 一个完整交易日应为 240 根；<200 视为"薄日"（半天/停牌/缺采）
    out["thin_days"] = int((per_day < 200).sum())
    out["thin_ratio"] = round(out["thin_days"] / max(out["days"], 1), 4)

    if factor is not None and len(factor):
        out["factor_days"] = int(factor.size)
        fdates = pd.DatetimeIndex(factor.index).normalize()
        dd = pd.DatetimeIndex(d["time"].dt.normalize().unique())
        out["factor_aligned"] = bool(len(dd.intersection(fdates)) > 0)
        if not out["factor_aligned"]:
            out["issues"].append("复权因子与分钟数据日期无法对齐")
    else:
        out["issues"].append("复权因子缺失")

    if out["bars"] < 240:
        out["issues"].append(f"数据量过少（{out['bars']} 根 < 1 个交易日）")
    if out["thin_ratio"] > 0.05:
        out["issues"].append(f"薄日占比 {out['thin_ratio']:.1%} 偏高（>5%）")

    out["ok"] = not out["issues"]
    return out


def upsert_pool(pool_file: Path, record: dict) -> None:
    """进池：同 code 幂等更新，按 code 排序落盘。"""
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict] = {}
    if pool_file.exists():
        for line in pool_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            existing[r.get("code")] = r
    existing[record["code"]] = record
    rows = [existing[k] for k in sorted(existing)]
    pool_file.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


def load_names() -> dict:
    p = APP_DIR / "data" / "stock_list.csv"
    if not p.exists():
        return {}
    try:
        df = pd.read_csv(p, dtype={"code": str})
        return dict(zip(df["code"], df.get("name", pd.Series(dtype=str))))
    except Exception:  # noqa: BLE001
        return {}


def read_codes_from_csv(path: Path, top: int | None) -> list[str]:
    df = pd.read_csv(path, dtype=str)
    col = next((c for c in df.columns if c.lower() in ("code", "代码", "symbol")), None)
    if col is None:
        raise SystemExit(f"[错误] CSV 里找不到代码列（需名为 code/代码/symbol）：{list(df.columns)}")
    codes = [str(x).strip() for x in df[col].dropna().tolist()]
    return codes[:top] if top else codes


def main() -> int:
    ap = argparse.ArgumentParser(description="选股进池：拉分钟数据 + 复权因子，验数后入池")
    ap.add_argument("--codes", nargs="*", default=[], help="候选代码（6 位数字）")
    ap.add_argument("--from-csv", type=str, help="从含 code 列的 CSV 读取候选")
    ap.add_argument("--top", type=int, help="配合 --from-csv，只取前 N 只")
    ap.add_argument("--start", default="2023-01-01", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"), help="结束日期")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不登录、不拉数")
    ap.add_argument("--force", action="store_true", help="忽略采集窗口检查（不推荐）")
    ap.add_argument("--data-dir", default=str(APP_DIR / "data"))
    ap.add_argument("--pool-file", default=str(APP_DIR / "data" / "pool.jsonl"))
    args = ap.parse_args()

    raw = list(args.codes)
    if args.from_csv:
        raw += read_codes_from_csv(Path(args.from_csv), args.top)
    if not raw:
        print("[错误] 未提供候选：用 --codes 或 --from-csv 指定。")
        return 2

    codes, rejected = normalize_codes(raw)
    names = load_names()
    data_dir = Path(args.data_dir)
    pool_file = Path(args.pool_file)

    print("=" * 68)
    print(f"选股进池  |  区间 {args.start} ~ {args.end}")
    print(f"候选 {len(raw)} 只 → 可用 {len(codes)} 只" + (f"，拒绝 {len(rejected)} 只" if rejected else ""))
    for r in rejected:
        print(f"  [拒绝] {r}")
    print(f"目标目录: {data_dir}")
    print(f"台账文件: {pool_file}")
    print("=" * 68)

    if in_collect_window():
        msg = ("[警告] 当前处于观察期采集窗口（本机 04:30–10:15）！\n"
               "       星耀单点登录会被本脚本抢占，导致当天采集中断。\n"
               "       建议改到采集窗口外执行（如本机 10:15 之后或周末）。")
        if not args.force:
            print(msg)
            print("       已中止。确认要执行请加 --force。")
            return 3
        print(msg + "\n       --force 已指定，继续执行（风险自负）。")

    if args.dry_run:
        print("\n[干跑模式] 不会登录、不会拉数。计划如下：\n")
        for c in codes:
            exist_1m = (data_dir / f"stock_{c}_1m.csv").exists()
            exist_f = (data_dir / f"factor_{c}.csv").exists()
            print(f"  {c} {names.get(c, ''):<8} 分钟数据={'已存在(将增量)' if exist_1m else '新建'}  "
                  f"复权因子={'已存在(复用)' if exist_f else '新建'}")
        print(f"\n共 {len(codes)} 只。去掉 --dry-run 即真拉。")
        return 0

    print()
    results = []
    for i, code in enumerate(codes, 1):
        tag = f"[{i}/{len(codes)}] {code} {names.get(code, '')}"
        try:
            added, total, note = data_fetch.incremental_update(
                code, args.start, args.end, data_dir=data_dir)
            print(f"{tag} — 分钟数据: {note}")
        except Exception as exc:  # noqa: BLE001
            print(f"{tag} — [失败] 分钟数据拉取异常: {str(exc)[:120]}")
            results.append({"code": code, "ok": False, "err": str(exc)[:200]})
            continue

        try:
            factor = load_backward_factor(code)
            print(f"{tag} — 复权因子: {factor.size} 个交易日")
        except Exception as exc:  # noqa: BLE001
            factor = None
            print(f"{tag} — [警告] 复权因子获取失败: {str(exc)[:100]}（验数将标记）")

        target = data_dir / f"stock_{code}_1m.csv"
        try:
            df = pd.read_csv(target, parse_dates=["time"])
            v = verify(df, factor)
        except Exception as exc:  # noqa: BLE001
            print(f"{tag} — [失败] 验数读取异常: {str(exc)[:120]}")
            results.append({"code": code, "ok": False, "err": str(exc)[:200]})
            continue

        flag = "✅ 通过" if v["ok"] else "⚠️ 有疑点"
        print(f"{tag} — 验数: {flag} | {v['bars']} 根 / {v['days']} 天 "
              f"| {v['first']} → {v['last']} | 薄日 {v['thin_days']} ({v['thin_ratio']:.1%}) "
              f"| 因子 {v['factor_days']} 天")
        for issue in v["issues"]:
            print(f"          · {issue}")

        rec = {
            "code": code, "ad_code": data_fetch.code_to_ad(code),
            "name": names.get(code, ""), "added_at": datetime.now().isoformat(timespec="seconds"),
            "bars": v["bars"], "days": v["days"], "first": v["first"], "last": v["last"],
            "thin_days": v["thin_days"], "thin_ratio": v["thin_ratio"],
            "factor_days": v["factor_days"], "factor_aligned": v["factor_aligned"],
            "source": "from_csv" if args.from_csv else "manual",
            "verified": v["ok"], "issues": v["issues"],
        }
        upsert_pool(pool_file, rec)
        results.append({"code": code, "ok": v["ok"], **v})

    print()
    print("=" * 68)
    ok_n = sum(1 for r in results if r.get("ok"))
    print(f"完成：{ok_n}/{len(results)} 只通过验数")
    if pool_file.exists():
        n = len([l for l in pool_file.read_text(encoding="utf-8").splitlines() if l.strip()])
        print(f"观察池现有 {n} 只：{pool_file}")
    print("=" * 68)
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
