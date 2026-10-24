#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QMT 交易接口**只读探针**（P1-b 交易侧第一步）：查资金 / 持仓 / 委托 / 成交。

红线（代码内自检，不是口头承诺）
------------------------------
- 本文件**不调用**任何 `order_*` / `cancel_*` / `bank_transfer_*` / `*_async` 下单类接口；
  启动时 `_assert_readonly()` 会扫描本文源码，出现禁止词直接 `SystemExit(2)`。
- `--execute` 之类参数**故意不提供**：本工具只能看，不能动。
- 只 connect + query。session_id 用时间戳派生，避免与在用会话撞号（撞号会顶掉对方）。

用法
----
  D:\\Anaconda3\\python.exe scripts/tool_qmt_trader_probe.py

产物
----
  test_outputs/qmt_trader_probe/probe_<时间戳>.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
OUT = APP / "test_outputs" / "qmt_trader_probe"

def _assert_readonly() -> None:
    """AST 只查**真实调用**（注释/文档/常量表里的字眼不算违规）。"""
    import ast
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    bad = [n.func.attr for n in ast.walk(tree)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr in _FORBIDDEN]
    if bad:
        print(f"⛔ 只读自检失败：源码调用了禁用的下单/撤单接口 {sorted(set(bad))}", file=sys.stderr)
        raise SystemExit(2)


# 只读：任何下单/撤单/转账类方法名出现在本文件即拒绝运行
_FORBIDDEN = ("order_stock", "cancel_order", "bank_transfer", "fund_transfer",
              "secu_transfer", "smart_algo_order", "order_stock_async")


def _qmt_userdata() -> Path:
    """miniQMT 的 userdata_mini 目录（xtdata 报的数据路径去掉 /datadir）。"""
    cands = []
    try:
        from xtquant import xtdata
        from xtquant.xtdata import get_client
        info = getattr(get_client(), "get_data_dir", lambda: "")()
        if info:
            p = Path(str(info))
            cands.append(p.parent if p.name.lower() == "datadir" else p)
    except Exception:  # noqa: BLE001
        pass
    cands += [Path(r"D:\迅投QMT交易终端   国盛证券实盘版\userdata_mini")]
    for c in cands:
        if c.is_dir():
            return c
    raise SystemExit(f"⛔ 找不到 userdata_mini，候选：{[str(c) for c in cands]}")


def _jsonable(obj) -> dict:
    if obj is None:
        return {}
    out = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            v = getattr(obj, name)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(v, (int, float, str, bool)) or v is None:
            out[name] = v
    return out


def main() -> int:
    _assert_readonly()
    ap = argparse.ArgumentParser(description="QMT 交易接口只读探针")
    ap.add_argument("--timeout", type=float, default=15.0, help="等待账户列表的超时秒数")
    args = ap.parse_args()

    from xtquant.xttrader import XtQuantTrader  # 延迟 import
    from xtquant.xttype import StockAccount

    ud = _qmt_userdata()
    session_id = int(time.time()) % 1_000_000 + 200_000
    print(f"userdata_mini: {ud}\nsession_id: {session_id}（时间戳派生，避免撞号）")

    trader = XtQuantTrader(str(ud), session_id)
    trader.start()
    rc = trader.connect()
    print(f"connect() -> {rc}（0=成功）")
    if rc != 0:
        print("⛔ 连接失败：确认 miniQMT 的交易登录已就绪（资金账户）", file=sys.stderr)
        return 2

    accounts = None
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            accounts = trader.query_account_infos()
        except Exception as exc:  # noqa: BLE001
            print(f"  query_account_infos 异常：{type(exc).__name__}: {exc}")
        if accounts:
            break
        time.sleep(1.0)

    result = {"probe_at": datetime.now().isoformat(timespec="seconds"),
              "userdata_mini": str(ud), "session_id": session_id, "connect_rc": rc,
              "accounts": [], "errors": []}
    if not accounts:
        print("⚠️ 15s 内未取到账户列表（可能回调尚未就绪）")
        result["errors"].append("empty_accounts")
    for acc in accounts or []:
        aid = getattr(acc, "account_id", None) or str(acc)
        atype = getattr(acc, "account_type", None)
        print(f"\n账户: {aid}  类型: {atype}")
        item = {"account_id": aid, "account_type": atype}
        sa = StockAccount(str(aid), "STOCK")
        for label, fn in (("asset", trader.query_stock_asset),
                          ("positions", trader.query_stock_positions),
                          ("orders", trader.query_stock_orders),
                          ("trades", trader.query_stock_trades)):
            try:
                raw = fn(sa)
                if isinstance(raw, list):
                    item[label] = [_jsonable(x) for x in raw]
                else:
                    item[label] = _jsonable(raw)
                n = len(item[label]) if isinstance(item[label], list) else 1
                print(f"  {label:10s} {n} 条")
            except Exception as exc:  # noqa: BLE001
                item[label] = {"error": f"{type(exc).__name__}: {exc}"}
                result["errors"].append(f"{label}:{type(exc).__name__}")
                print(f"  {label:10s} 异常 {type(exc).__name__}")
        result["accounts"].append(item)

    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"probe_{datetime.now():%Y%m%d_%H%M%S}.json"
    p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物: {p}")
    print("⚠️ 只读探针：全程未调用任何下单/撤单接口。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
