# -*- coding: utf-8 -*-
r"""QMT（xttrader）交易适配器 —— **默认锁死，不 arm 不下单**。

为什么它跟 free-stockdb 无关
---------------------------
free-stockdb **没有**下单/委托/资金/持仓接口（2026-10-25 核实仓库全量文件清单）。
"交易接进去"只能接券商，本机唯一可用通道 = QMT `xttrader`。
所以本模块与 `stockdb_kline` 是两条独立链路，只是最终都在 `live_match_loop` 汇合。

为什么默认锁死
-------------
国盛 QMT **没有模拟盘**（A16 已定案）⇒ **第一次 `order_stock` 就是真金白银**。
因此：
  - 所有**只读**方法（资产/持仓/委托/成交）任何时候都能用；
  - 所有**写**方法（下单/撤单）必须 `QMT_TRADING_ARMED=1` 才放行，否则抛
    `QmtTradingDisarmed`；
  - arm 之后仍要求 `A16 对账` 已清（费用口径第一次要靠实盘反推），这是**流程门禁**，
    代码无法替你判断，所以只做一次显式 arm，不做"自动判断"。

设计约束
--------
- **依赖注入**：`trader` / `account` 由调用方传入，本模块不自己建会话
  ⇒ 单测可以用假对象，完全不需要装 QMT。
- **纯映射与 IO 分离**：`to_qmt_code` / `side_to_const_name` / `bind_ok` 是纯函数，
  已单测；真正下单只有 `_order_stock()` 一处。
- 会话建立方式复刻已验证的 `scripts/tool_qmt_trader_probe.py`
  （`XtQuantTrader(userdata_mini, session_id)` → `.start()` → `.connect()`，0 = 成功）。

官方签名（`D:\Codex输出\国盛QMT官方文档_20260920\02_XtTrader_交易模块.md:1199`）
--------------------------------------------------------------------------------
    order_stock(account, stock_code, order_type, order_volume, price_type,
                price, strategy_name, order_remark) -> order_id
    - stock_code 形如 '600000.SH'；order_volume 单位 = 股
    - 返回 >0 成功；**-1 表示委托失败**
"""
from __future__ import annotations

import os
from dataclasses import dataclass

ARM_ENV = "QMT_TRADING_ARMED"


class QmtBrokerError(RuntimeError):
    """交易链路错误（未 arm / 下单失败 / 会话未就绪）。"""


class QmtTradingDisarmed(QmtBrokerError):
    """未 arm 就想下单 —— 这是设计行为，不是 bug。"""


def armed() -> bool:
    return os.environ.get(ARM_ENV, "").strip() == "1"


def require_armed() -> None:
    if not armed():
        raise QmtTradingDisarmed(
            f"交易未 arm（{ARM_ENV} != 1），拒绝下单。"
            "国盛 QMT 无模拟盘，首次下单即实盘；须先过 P0 清账 + A16 对账。"
        )


def to_qmt_code(code: str) -> str:
    """'300308' → '300308.SZ'；'600900' → '600900.SH'；已带后缀原样大写。

    交易所判定按代码首位（A 股规则）：6/9 开头 = 沪，其余 = 深。
    """
    c = code.strip().upper()
    if "." in c:
        return c
    if not (c.isdigit() and len(c) == 6):
        raise QmtBrokerError(f"非法 A 股代码：{code!r}（需 6 位数字或带后缀）")
    return f"{c}.SH" if c[0] in "69" else f"{c}.SZ"


def side_to_const_name(side: str) -> str:
    """'BUY'/'SELL' → xtconstant 的常量名（纯字符串，便于无 xtquant 单测）。"""
    s = str(side).strip().upper()
    mapping = {"BUY": "STOCK_BUY", "SELL": "STOCK_SELL",
               "买入": "STOCK_BUY", "卖出": "STOCK_SELL"}
    if s not in mapping:
        raise QmtBrokerError(f"未知买卖方向：{side!r}")
    return mapping[s]


def _const(name: str):
    from xtquant import xtconstant          # 延迟 import：不装 QMT 也能 import 本模块
    return getattr(xtconstant, name)


@dataclass
class OrderResult:
    ok: bool
    order_id: int | None
    raw: object
    reason: str = ""


class QmtBroker:
    """薄适配层：把项目的下单参数映射到 xttrader，**不做撮合**（撮合在券商侧）。

    `trader` / `account` 由外部注入：
        trader  = XtQuantTrader(userdata_mini, session_id)  # 已 start + connect
        account = StockAccount(account_id, "STOCK")
    """

    def __init__(self, trader, account, strategy_name: str = "quant_app"):
        self.trader = trader
        self.account = account
        self.strategy_name = strategy_name

    # ── 只读：任何时候可用 ──────────────────────────────────────────────
    def query_asset(self):
        return self.trader.query_stock_asset(self.account)

    def query_positions(self):
        return self.trader.query_stock_positions(self.account)

    def query_orders(self):
        return self.trader.query_stock_orders(self.account)

    def query_trades(self):
        return self.trader.query_stock_trades(self.account)

    # ── 写：必须 arm ────────────────────────────────────────────────────
    def submit(self, code: str, side: str, quantity: int, price: float,
               remark: str = "") -> OrderResult:
        """限价单。数量单位 = 股（与 xttrader 一致，**不要**再乘 100）。"""
        require_armed()
        qty = int(quantity)
        if qty <= 0:
            raise QmtBrokerError(f"数量必须为正：{quantity!r}")
        if qty % 100 != 0:
            # A 股买入须 100 股整数倍；卖出零股需一次性全卖 —— 这里不自作聪明，直接拒
            raise QmtBrokerError(f"A 股委托数量须为 100 股整数倍：{qty}")
        if not (price and float(price) > 0):
            raise QmtBrokerError(f"限价单价格必须为正：{price!r}")

        const = _const(side_to_const_name(side))
        oid = self.trader.order_stock(
            self.account, to_qmt_code(code), const, qty,
            _const("FIX_PRICE"), float(price),
            self.strategy_name, remark or "")
        if oid is None or int(oid) == -1:
            return OrderResult(ok=False, order_id=None, raw=oid, reason="order_stock 返回 -1")
        return OrderResult(ok=True, order_id=int(oid), raw=oid)

    def cancel(self, order_id: int) -> bool:
        require_armed()
        rc = self.trader.cancel_order_stock(self.account, int(order_id))
        return int(rc) == 0 if rc is not None else False


def build_session(userdata_mini: str, session_id: int,
                  account_id: str | None = None, connect_timeout: float = 15.0):
    """建立会话并返回 (trader, account)。**只连不登、只读**。

    会话建立方式与 `scripts/tool_qmt_trader_probe.py` 一致（已验证）：
    session_id 必须与在用会话错开，撞号会顶掉对方。
    """
    import time

    from xtquant.xttrader import XtQuantTrader
    from xtquant.xttype import StockAccount

    trader = XtQuantTrader(str(userdata_mini), int(session_id))
    trader.start()
    rc = trader.connect()
    if rc != 0:
        raise QmtBrokerError(f"connect() 返回 {rc}（0=成功）：确认 miniQMT 交易登录就绪")

    deadline = time.time() + connect_timeout
    accounts = []
    while time.time() < deadline and not accounts:
        accounts = trader.query_account_infos() or []
        if not accounts:
            time.sleep(1.0)
    if not accounts:
        raise QmtBrokerError("未取到资金账户列表（回调未就绪或未登录）")

    aid = account_id or (getattr(accounts[0], "account_id", None) or str(accounts[0]))
    return trader, StockAccount(str(aid), "STOCK")
