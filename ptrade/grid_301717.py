# -*- coding: utf-8 -*-
"""PTrade 单文件策略：301717 移动网格（H76 移植版）

逻辑来源：`core/grid_shared.py::GridSharedEngine`，逐 bar 状态机**逐行对齐**，
对账方式见 `test_h75_ptrade_grid_parity.py`（同一段行情喂两个实现，比成交序列）。

为什么必须单文件：PTrade 策略跑在券商机房沙箱里，不能 import 本地工程、
不能读本机文件、不能连外网。所以核心逻辑整段搬进来。

语法约束：PTrade 多数券商内置 Python 3.5（国金已升 3.11，其余多为 3.5）
  ⇒ 本文件禁用 f-string / dataclasses / 变量注解 / `from __future__ import annotations`。
  改这个文件前先跑到 env_probe.py 确认版本。

安全默认：
  ARMED = False ⇒ 只算信号 + 打日志，**不下任何委托**。
  置 True 前必须完成：① env_probe ② 本地回放对账 ③ 模拟盘首单对账。

与纸面**有意不同**的三处（必须知情，别按纸面数字预期它）：
  1. 账户口径 = 真实账户的切片（BUDGET），不再是 100 万影子账户
     ⇒ `max_grid_loss_pct` 5% 止损会真的触发（纸面因影子账户 1M 而长期失效，见 H75）。
  2. 委托异步 ⇒ `order()` 返回即记「假定成交」，`on_trade_response` 再对账；
     `SAFE_HALT` 一旦为真，当日不再新开仓（只允许卖）。
  3. 复权 ⇒ `signal_close = close * ADJ`，ADJ 由日线 pre / 不复权 比值推出。
"""

from __future__ import print_function

import json
import math

try:
    import pandas as pd
except Exception:  # pragma: no cover - PTrade 一定有 pandas
    pd = None


# ─────────────────────────────────────────────────────────────
# 参数（与 strategies/grid_trade.py 的 param_meta 默认值同源）
# ─────────────────────────────────────────────────────────────
SYMBOL = '301717.SZ'

GRID_PARAMS = {
    'base_position': 200,          # 首根 bar 建底仓股数（30 万 × 25% / 现价 ≈ 200）
    'grid_pct': 2.0,               # 网格间距 %
    'grid_levels': 5,              # 中枢下方最多买几格
    'trade_shares': 100,           # 每格股数
    'trend_ma': 60,                # 趋势过滤均线（日线）
    'center_freeze_pct': 8.0,      # 中枢相对启动价最大下移 %
    'hard_stop_pct': 12.0,         # 价格硬止损 %
    'max_grid_loss_pct': 5.0,      # 账户亏损止损 %
    'enable_afternoon_close': False,
    '_init_cash': 300000.0,        # 账户亏损止损的分母（= BUDGET）
    'commission_rate': 0.00008,
    'min_commission': 1.0,
    'stamp_tax_rate': 0.0005,
    'transfer_fee_rate': 0.00001,
    'slippage_bps': 5.0,
}

BUDGET = 300000.0        # 本策略在真实账户里的资金切片
ARMED = False            # ← 实盘开关，默认关
MAX_SYMBOL_PCT = 0.25    # 单标的上限（占 BUDGET）
MAX_ORDER_DEV = 0        # 账本与真实持仓允许偏差（股），超出则 SAFE_HALT
TAG = '[GRID301717]'


# ─────────────────────────────────────────────────────────────
# 状态机（对齐 GridSharedState）
# ─────────────────────────────────────────────────────────────
class GridState(object):
    __slots__ = ('base_price', 'anchor_price', 'grid_bought', 'stopped', 'prev_day',
                 'day_amount', 'day_volume', 'prev_day_close', 'daily_closes')

    def __init__(self):
        self.base_price = None
        self.anchor_price = None
        self.grid_bought = 0
        self.stopped = False
        self.prev_day = None
        self.day_amount = 0.0
        self.day_volume = 0.0
        self.prev_day_close = 0.0
        self.daily_closes = []

    def to_dict(self):
        return {
            'base_price': self.base_price,
            'anchor_price': self.anchor_price,
            'grid_bought': self.grid_bought,
            'stopped': self.stopped,
            'prev_day': self.prev_day.isoformat() if self.prev_day else None,
            'day_amount': self.day_amount,
            'day_volume': self.day_volume,
            'prev_day_close': self.prev_day_close,
            'daily_closes': list(self.daily_closes),
        }

    @classmethod
    def from_dict(cls, d):
        st = cls()
        st.base_price = d.get('base_price')
        st.anchor_price = d.get('anchor_price')
        st.grid_bought = int(d.get('grid_bought', 0) or 0)
        st.stopped = bool(d.get('stopped', False))
        st.prev_day = _parse_date(d.get('prev_day'))
        st.day_amount = float(d.get('day_amount', 0.0) or 0.0)
        st.day_volume = float(d.get('day_volume', 0.0) or 0.0)
        st.prev_day_close = float(d.get('prev_day_close', 0.0) or 0.0)
        st.daily_closes = list(d.get('daily_closes', []) or [])
        return st


def _parse_date(s):
    if not s:
        return None
    if hasattr(s, 'year') and hasattr(s, 'month') and hasattr(s, 'day'):
        return s
    try:
        y, m, d = str(s).split('-')
        import datetime
        return datetime.date(int(y), int(m), int(d))
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# 网格核心（对齐 GridSharedEngine.on_bar）
# ─────────────────────────────────────────────────────────────
class GridCore(object):
    def __init__(self, params):
        p = dict(params)
        self.warmup_end = p.pop('warmup_end', None)
        if self.warmup_end is not None and pd is not None:
            self.warmup_end = pd.Timestamp(self.warmup_end)
        self.params = p
        self.state = GridState()
        self._base_position = int(p.get('base_position', 1000))
        self._trade_shares = int(p.get('trade_shares', 100))
        self._grid_step = float(p.get('grid_pct', 2.0)) / 100.0
        self._grid_levels = int(p.get('grid_levels', 5))
        self._max_position = int(p.get('max_position', 6000))
        self._enable_close = bool(p.get('enable_afternoon_close', False))
        self._freeze_pct = float(p.get('center_freeze_pct', 8.0)) / 100.0
        self._hard_stop_pct = float(p.get('hard_stop_pct', 12.0)) / 100.0
        self._max_loss_pct = float(p.get('max_grid_loss_pct', 5.0)) / 100.0
        self._init_cash = float(p.get('_init_cash', 300000.0))
        self._trend_ma = int(p.get('trend_ma', 60))

    def on_bar(self, bar, account):
        ts = bar['time']
        if pd is not None:
            ts = pd.Timestamp(ts)
        day = ts.date()
        cur = float(bar['close'])
        signal_close = float(bar.get('signal_close', cur))
        hhmm = ts.hour * 100 + ts.minute
        trades = []

        if day != self.state.prev_day:
            if self.state.prev_day is not None:
                self.state.daily_closes.append(self.state.prev_day_close)
            if hasattr(account, 'new_day'):
                account.new_day()
            self.state.day_amount = 0.0
            self.state.day_volume = 0.0
            self.state.prev_day = day
            # H46-①：stopped 只锁当日；止损线每交易日重判
            self.state.stopped = False

        self.state.day_amount += float(bar.get('amount', 0) or 0)
        self.state.day_volume += float(bar.get('volume', 0) or 0)
        self.state.prev_day_close = signal_close

        if self.warmup_end is not None and pd is not None and pd.Timestamp(bar['time']) < self.warmup_end:
            return trades

        # 建底仓
        if self.state.base_price is None:
            if self.state.anchor_price is None:
                self.state.anchor_price = cur
            if self._base_position > 0:
                ok = account.buy(cur, self._base_position)
                if ok:
                    self.state.base_price = account.last_fill_price
                    trades.append(self._trade(ts, 'BUY', self._base_position, '建底仓', account))
            else:
                self.state.base_price = cur
            return trades

        raw_center = (self.state.day_amount / self.state.day_volume) if self.state.day_volume else float('nan')
        if math.isnan(raw_center):
            raw_center = cur
        center_floor = self.state.anchor_price * (1 - self._freeze_pct)
        self.state.base_price = max(raw_center, center_floor)

        account_loss = account.equity(cur) / self._init_cash - 1
        hard_stop = cur <= self.state.base_price * (1 - self._hard_stop_pct)
        loss_stop = account_loss <= -self._max_loss_pct
        if hard_stop or loss_stop:
            self.state.stopped = True
            sellable_t = max(0, min(account.closeable, account.total - self._base_position))
            if sellable_t > 0:
                sold = account.sell(cur, sellable_t)
                if sold:
                    trades.append(self._trade(ts, 'SELL', sold, '网格硬止损', account))
                    self.state.grid_bought = max(
                        0, self.state.grid_bought - sold // max(self._trade_shares, 1))

        # 尾盘平 T 仓（可选）
        if self._enable_close and hhmm >= 1455:
            t_shares = max(0, account.total - self._base_position)
            if t_shares > 0:
                sold = account.sell(cur, t_shares)
                if sold:
                    trades.append(self._trade(ts, 'SELL', sold, '尾盘平T仓', account))
                    self.state.grid_bought = 0
            return trades

        # 卖出：涨回上一格
        if self.state.grid_bought > 0:
            sell_line = self.state.base_price * (1 - self._grid_step * (self.state.grid_bought - 1))
            if cur >= sell_line:
                sellable = max(0, account.total - self._base_position)
                if sellable >= self._trade_shares:
                    sold = account.sell(cur, self._trade_shares)
                    if sold:
                        trades.append(self._trade(
                            ts, 'SELL', self._trade_shares,
                            '网格卖出(回到%d档)' % (self.state.grid_bought - 1), account))
                        self.state.grid_bought -= 1
                else:
                    trades.append(self._rejected(
                        ts, 'SELL', self._trade_shares,
                        '网格卖出但T仓不足(T+1/持仓%s)' % account.closeable))

        # 买入：跌破下一格（趋势防御 + 停买锁）
        defense = self._trend_defense_active()
        if (self.state.grid_bought < self._grid_levels
                and not self.state.stopped and not defense):
            buy_line = self.state.base_price * (1 - self._grid_step * (self.state.grid_bought + 1))
            if cur <= buy_line:
                if account.total + self._trade_shares > self._max_position:
                    trades.append(self._rejected(
                        ts, 'BUY', self._trade_shares, '网格买入超最大持仓'))
                elif account.cash < cur * self._trade_shares:
                    trades.append(self._rejected(
                        ts, 'BUY', self._trade_shares, '网格买入现金不足'))
                else:
                    ok = account.buy(cur, self._trade_shares)
                    if ok:
                        trades.append(self._trade(
                            ts, 'BUY', self._trade_shares,
                            '网格买入(-%d档)' % (self.state.grid_bought + 1), account))
                        self.state.grid_bought += 1
                    else:
                        trades.append(self._rejected(
                            ts, 'BUY', self._trade_shares, '网格买入未成交'))
        return trades

    def _trend_defense_active(self):
        closes = self.state.daily_closes
        n = len(closes)
        if n < self._trend_ma:
            return False
        s = pd.Series(closes, dtype=float)
        ma = s.rolling(self._trend_ma, min_periods=self._trend_ma).mean()
        slope = ma.diff(5)
        last_close = s.iloc[-1]
        last_ma = ma.iloc[-1]
        last_slope = slope.iloc[-1]
        if pd.isna(last_ma) or pd.isna(last_slope):
            return False
        return bool(last_close < last_ma and last_slope < 0)

    @staticmethod
    def _trade(ts, direction, shares, reason, account):
        return {'time': ts, 'direction': direction, 'shares': shares, 'reason': reason,
                'price': account.last_fill_price, 'fee': account.last_fee}

    @staticmethod
    def _rejected(ts, direction, shares, reason):
        return {'time': ts, 'direction': direction, 'shares': shares,
                'reason': reason, 'rejected': True}


# ─────────────────────────────────────────────────────────────
# 账户适配层：本地子账本 + 真实账户对账
# ─────────────────────────────────────────────────────────────
class PtAccount(object):
    """引擎要的是同步账户（买完立刻知道成交价/持仓），PTrade 是异步委托。

    折中：order() 返回非空即记「假定成交」，价格用当前 bar 收盘价（与纸面同口径）；
    on_trade_response 回来时用真实成交价修订账本，偏差超阈值 ⇒ SAFE_HALT。
    """

    def __init__(self, sid, budget, params):
        self.sid = sid
        self.cash = float(budget)
        self.total = 0
        self.closeable = 0
        self.init_cash = float(budget)
        self.last_fill_price = None
        self.last_fee = 0.0
        self.commission_rate = float(params.get('commission_rate', 0.00008))
        self.min_commission = float(params.get('min_commission', 1.0))
        self.stamp_tax_rate = float(params.get('stamp_tax_rate', 0.0005))
        self.transfer_fee_rate = float(params.get('transfer_fee_rate', 0.00001))
        self.slippage_bps = float(params.get('slippage_bps', 5.0))
        self.orders = []       # 本地委托流水
        self.safe_halt = False
        self.halt_reason = ''

    # ── 引擎接口 ──
    def new_day(self):
        self.closeable = self.total

    def buy(self, price, shares):
        if self.safe_halt:
            return False
        fill = price * (1.0 + self.slippage_bps / 10000.0)
        cost = fill * shares
        fee = max(self.min_commission, cost * self.commission_rate) + cost * self.transfer_fee_rate
        if self.cash < cost + fee:
            return False
        oid = _place(self.sid, int(shares), price, 'BUY')
        if oid is False:
            return False
        self.cash -= cost + fee
        self.total += int(shares)
        self.last_fee = fee
        self.last_fill_price = fill
        self.orders.append({'side': 'BUY', 'shares': int(shares), 'price': fill,
                            'oid': oid, 't': str(_now(context=None))})
        return True

    def sell(self, price, shares):
        shares = min(int(shares), self.closeable)
        if shares <= 0:
            return 0
        fill = price * (1.0 - self.slippage_bps / 10000.0)
        proceeds = fill * shares
        fee = (max(self.min_commission, proceeds * self.commission_rate)
               + proceeds * self.stamp_tax_rate + proceeds * self.transfer_fee_rate)
        oid = _place(self.sid, -shares, price, 'SELL')
        if oid is False:
            return 0
        self.cash += proceeds - fee
        self.total -= shares
        self.closeable -= shares
        self.last_fee = fee
        self.last_fill_price = fill
        self.orders.append({'side': 'SELL', 'shares': shares, 'price': fill,
                            'oid': oid, 't': str(_now(context=None))})
        return shares

    def equity(self, price):
        return self.cash + self.total * price

    def halt(self, reason):
        self.safe_halt = True
        self.halt_reason = reason
        log.info('%s SAFE_HALT: %s' % (TAG, reason))


def _now(context=None):
    try:
        return context.blotter.current_dt
    except Exception:
        return ''


def _place(sid, amount, ref_price, tag):
    """返回：委托号 / 'DRY'（未武装）/ False（被拒）。"""
    if amount == 0:
        return False
    if not ARMED:
        log.info('%s [DRY] %s %s %d股 参考价 %.2f' % (TAG, tag, sid, abs(amount), ref_price))
        return 'DRY'
    # 限价保护：买 上浮 0.3%、卖 下浮 0.3%，避免市价单在极端行情里打穿。
    if amount > 0:
        lp = round(ref_price * 1.003, 2)
    else:
        lp = round(ref_price * 0.997, 2)
    try:
        oid = order(sid, amount, limit_price=lp)
    except TypeError:
        oid = order(sid, amount)   # 老版本无 limit_price 参数
    except Exception as exc:
        log.info('%s 委托异常 %s: %s' % (TAG, type(exc).__name__, exc))
        return False
    if oid is None:
        log.info('%s 委托被拒 %s %d股 @%.2f' % (TAG, tag, abs(amount), lp))
        return False
    log.info('%s 委托 %s %s %d股 @%.2f oid=%s' % (TAG, tag, sid, abs(amount), lp, oid))
    return oid


# ─────────────────────────────────────────────────────────────
# PTrade 生命周期
# ─────────────────────────────────────────────────────────────
def initialize(context):
    set_universe(SYMBOL)
    try:
        set_commission(commission_ratio=GRID_PARAMS['commission_rate'],
                       min_commission=GRID_PARAMS['min_commission'], type='STOCK')
    except Exception as exc:
        log.info('%s set_commission 失败（用券商默认）: %s' % (TAG, exc))

    g.sid = SYMBOL
    g.params = dict(GRID_PARAMS)
    g.params['max_position'] = max(int(g.params['base_position'] * 2),
                                   int(g.params['base_position']) + 100,
                                   int(BUDGET * MAX_SYMBOL_PCT / 270.0) // 100 * 100)
    g.core = GridCore(g.params)
    g.acct = PtAccount(SYMBOL, BUDGET, g.params)
    g.adj = 1.0
    g.last_min = None
    g.trades = []
    g.day = None

    # 支持「重启续跑」：把状态写进券商研究目录，隔日重启可恢复
    g.state_path = ''
    try:
        g.state_path = get_research_path() + '/grid_301717_state.json'
        _restore(g)
    except Exception as exc:
        log.info('%s 状态恢复跳过: %s' % (TAG, exc))

    log.info('%s init | ARMED=%s BUDGET=%.0f base=%d trade=%d max=%d trend_ma=%d' % (
        TAG, ARMED, BUDGET, g.params['base_position'], g.params['trade_shares'],
        g.params['max_position'], g.params['trend_ma']))


def before_trading_start(context, data):
    g.day = context.blotter.current_dt.date()
    # 复权系数：日线 pre / 不复权
    try:
        pre = get_history(1, frequency='1d', field='close', security_list=SYMBOL, fq='pre')
        raw = get_history(1, frequency='1d', field='close', security_list=SYMBOL, fq=None)
        pv = float(pre['close'].iloc[-1])
        rv = float(raw['close'].iloc[-1])
        if rv > 0:
            g.adj = pv / rv
    except Exception as exc:
        log.info('%s 复权系数取不到，按 1.0: %s' % (TAG, exc))
        g.adj = 1.0
    log.info('%s 新交易日 %s | adj=%.6f | state=%s' % (TAG, g.day, g.adj,
                                                       json.dumps(g.core.state.to_dict())))


def handle_data(context, data):
    unit = data.get(SYMBOL)
    if unit is None:
        return
    now = context.blotter.current_dt
    if g.last_min == now:
        return                      # 同一分钟重复触发保护
    g.last_min = now

    close = float(unit.close)
    bar = {
        'time': now,
        'close': close,
        'signal_close': close * g.adj,
        'amount': float(getattr(unit, 'money', 0.0) or 0.0),
        'volume': float(getattr(unit, 'volume', 0.0) or 0.0),
    }
    out = g.core.on_bar(bar, g.acct)
    for t in out:
        g.trades.append(t)
        if t.get('rejected'):
            log.info('%s 跳过 %s %d股 | %s' % (TAG, t['direction'], t['shares'], t['reason']))
        else:
            log.info('%s 成交 %s %d股 @%.2f 费%.2f | %s' % (
                TAG, t['direction'], t['shares'], t['price'], t['fee'], t['reason']))
    _reconcile(context)


def on_trade_response(context, trade_list):
    """真实成交回调：只做对账，不改状态机（状态机已按假定成交推进）。"""
    try:
        pos = context.portfolio.positions.get(SYMBOL)
        real = int(pos.amount) if pos is not None else 0
        dev = abs(real - g.acct.total)
        log.info('%s [T+0对账] 真实持仓 %d / 账本 %d / 偏差 %d' % (TAG, real, g.acct.total, dev))
        if dev > MAX_ORDER_DEV:
            g.acct.halt('持仓偏差 %d 股 > %d' % (dev, MAX_ORDER_DEV))
    except Exception as exc:
        log.info('%s on_trade_response 异常: %s' % (TAG, exc))


def after_trading_end(context, data):
    state = {
        'date': str(g.day),
        'state': g.core.state.to_dict(),
        'acct': {'cash': g.acct.cash, 'total': g.acct.total,
                 'equity': g.acct.equity(float(g.core.state.prev_day_close or 0.0))},
        'trades': [dict(t, time=str(t.get('time'))) for t in g.trades],
    }
    if g.state_path:
        try:
            fh = open(g.state_path, 'w')
            fh.write(json.dumps(state, ensure_ascii=False, default=str))
            fh.close()
        except Exception as exc:
            log.info('%s 状态落盘失败: %s' % (TAG, exc))
    log.info('%s 收盘 | %s' % (TAG, json.dumps(state['acct'], ensure_ascii=False)))


def _restore(g):
    fh = open(g.state_path, 'r')
    try:
        d = json.load(fh)
    finally:
        fh.close()
    g.core.state = GridState.from_dict(d.get('state') or {})
    acct = d.get('acct') or {}
    g.acct.cash = float(acct.get('cash', BUDGET))
    g.acct.total = int(acct.get('total', 0))
    g.acct.closeable = g.acct.total
    log.info('%s 状态已恢复: grid_bought=%d base=%.2f total=%d' % (
        TAG, g.core.state.grid_bought, g.core.state.base_price or 0.0, g.acct.total))


def _reconcile(context):
    try:
        pos = context.portfolio.positions.get(SYMBOL)
        real = int(pos.amount) if pos is not None else 0
        if abs(real - g.acct.total) > MAX_ORDER_DEV:
            g.acct.halt('持仓偏差 真实%d vs 账本%d' % (real, g.acct.total))
    except Exception:
        pass
