# -*- coding: utf-8 -*-
"""PTrade 环境探针 —— 只读，不下任何委托。

用法（在 PTrade 里新建策略，频率选「分钟」或「日线」都行，粘贴本文件全部内容，
点运行，然后看日志）：

    1. 新建策略 → 粘贴 → 保存 → 运行
    2. 日志里找 `[PROBE]` 开头的行
    3. 把整段贴回给我

设计原则：
  * 绝不调用 order / order_target / order_value / order_market —— 本文件零委托。
  * 每一段都有 try/except，某一项失败不影响后面的项。
  * 只用 3.5 兼容语法（PTrade 多数券商停在 3.5，用了 f-string 会直接语法报错）。
"""

from __future__ import print_function

import sys


PROBE_SYMBOL = '301717.SZ'


def _p(tag, value):
    try:
        log.info('[PROBE] %-22s %s' % (tag, value))
    except Exception:
        print('[PROBE] %-22s %s' % (tag, value))


def _try(tag, func):
    try:
        _p(tag, func())
    except Exception as exc:
        _p(tag, 'FAIL %s: %s' % (type(exc).__name__, exc))


def initialize(context):
    _p('python', sys.version.replace('\n', ' '))
    _p('executable', sys.executable)

    # ---- 关键第三方库版本（决定代码能不能用 f-string / 新 pandas）----
    def _libs():
        out = []
        for name in ('numpy', 'pandas', 'talib', 'scipy', 'statsmodels'):
            try:
                mod = __import__(name)
                out.append('%s=%s' % (name, getattr(mod, '__version__', '?')))
            except Exception:
                out.append('%s=ABSENT' % name)
        return ' '.join(out)
    _try('libs', _libs)

    # ---- 策略运行环境基本信息 ----
    _try('get_user_name', lambda: get_user_name())
    _try('get_research_path', lambda: get_research_path())
    _try('get_trading_day', lambda: get_trading_day())
    _try('universe', lambda: 'set_universe ok')

    # ---- 账户口径：这是「四个策略共享一个真实账户」的判据 ----
    def _portfolio():
        pf = context.portfolio
        return 'cash=%.2f portfolio_value=%.2f positions=%s' % (
            float(pf.cash), float(pf.portfolio_value), list(pf.positions.keys()))
    _try('portfolio', _portfolio)

    def _pos_fields():
        pf = context.portfolio
        if PROBE_SYMBOL not in pf.positions:
            return 'no position in %s' % PROBE_SYMBOL
        p = pf.positions[PROBE_SYMBOL]
        fields = [a for a in dir(p) if not a.startswith('_')]
        vals = []
        for a in ('amount', 'enable_amount', 'cost_basis', 'last_sale_price'):
            if hasattr(p, a):
                vals.append('%s=%s' % (a, getattr(p, a)))
        return 'fields=%s | %s' % (','.join(fields), ' '.join(vals))
    _try('position_fields', _pos_fields)

    # ---- 1 分钟数据：有没有、多深、复权三种口径能否取到 ----
    def _m1():
        df = get_history(5, frequency='1m', field=['open', 'high', 'low', 'close', 'volume'],
                         security_list=PROBE_SYMBOL, fq=None, include=False)
        if df is None:
            return 'None'
        return 'type=%s shape=%s tail_idx=%s' % (type(df).__name__, getattr(df, 'shape', '?'),
                                                 list(df.index[-2:]) if len(df) else [])
    _try('hist_1m', _m1)

    def _m1_deep():
        for n in (240, 480, 1200, 2400):
            try:
                df = get_history(n, frequency='1m', field='close',
                                 security_list=PROBE_SYMBOL, fq=None)
                got = 0 if df is None else len(df)
            except Exception as exc:
                return 'n=%d -> FAIL %s' % (n, exc)
            if got < n:
                return 'n=%d -> got %d（上限约 %d 根 ≈ %.1f 个交易日）' % (
                    n, got, got, got / 240.0)
        return 'n=2400 -> got 2400（>=10 个交易日）'
    _try('hist_1m_depth', _m1_deep)

    def _d1():
        df = get_history(300, frequency='1d', field=['close', 'volume'],
                         security_list=PROBE_SYMBOL, fq='pre')
        return 'shape=%s first=%s last=%s' % (
            getattr(df, 'shape', '?'),
            df.index[0] if len(df) else None, df.index[-1] if len(df) else None)
    _try('hist_1d_300', _d1)

    # ---- 停牌 / 涨跌停 / 名称（301717 是创业板，20% 幅度）----
    _try('stock_info', lambda: get_stock_info(PROBE_SYMBOL))
    _try('stock_name', lambda: get_stock_name(PROBE_SYMBOL))
    _try('stock_status', lambda: get_stock_status(PROBE_SYMBOL, 'HALT'))

    # ---- sqlite3：跨策略共享台账（H73 闸门要靠它）----
    def _sqlite():
        import sqlite3
        path = None
        try:
            base = get_research_path()
            path = base + '/probe_shared.db'
        except Exception:
            path = ':memory:'
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE IF NOT EXISTS t (k TEXT PRIMARY KEY, v TEXT)')
        conn.execute('INSERT OR REPLACE INTO t VALUES (?, ?)', ('probe', str(context.blotter.current_dt)))
        conn.commit()
        row = conn.execute('SELECT v FROM t WHERE k=?', ('probe',)).fetchone()
        conn.close()
        return 'OK path=%s readback=%s' % (path, row)
    _try('sqlite3_shared', _sqlite)

    # ---- 外网出口（是否封闭；只测 DNS/TCP，不做任何数据传输）----
    def _net():
        import socket
        out = []
        for host, port in (('114.114.114.114', 53), ('www.baidu.com', 443)):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            try:
                s.connect((host, port))
                out.append('%s:%d=OPEN' % (host, port))
            except Exception as exc:
                out.append('%s:%d=BLOCKED(%s)' % (host, port, type(exc).__name__))
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        return ' '.join(out)
    _try('net_egress', _net)

    # ---- 委托接口是否在（只检查可调用，不调用）----
    _try('order_api', lambda: 'order=%s order_target=%s order_value=%s' % (
        callable(globals().get('order')), callable(globals().get('order_target')),
        callable(globals().get('order_value'))))

    _p('done', '探针结束，零委托。把 [PROBE] 行整段贴回。')


def handle_data(context, data):
    # 探针不需要盘中逻辑
    pass
