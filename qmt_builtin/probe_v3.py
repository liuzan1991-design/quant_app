# ============================================================
# QMT builtin probe v3  (2026-11-03 evening, for LIVE run)
# READ-ONLY: no orders. Writes only its own result file.
#
# WHY v3 (what v2 left open):
#   * account fields: v2 ran in BACKTEST mode -> zero candidates.
#     LIVE mode is the only place account/position structs exist.
#   * daily bars: v2 got rows=1 for 1d count=10000 -> daily history
#     is NOT downloaded locally. Need range query to confirm.
#   * dividend: v2 never passed dividend_type -> default unknown.
#     Need none/front/back comparison on a known ex-div date.
#   * trading calendar: global is_trading_day was MISSING; need to
#     find whatever the host actually provides.
#   * api inventory: enumerate ContextInfo methods + module globals
#     so the port knows exactly what it can call.
#
# HOW TO RUN:
#   QMT -> python strategy panel -> open this file -> period 1m
#   -> LIVE (during session; 09:30-15:00 Beijing) -> run
#   Backtest mode still works for sections 2-6, only section 7
#   (account) needs LIVE.
# ============================================================

import sys
import os
import time

# Leave empty to auto-detect. Fill only if auto-detect finds nothing.
MANUAL_ACCOUNT = ''
try:
    import os as _os
    for _p in (r'C:\Users\27329\qmt_account_id.txt',
               r'C:\Users\27329\qmt_account.txt'):
        if _os.path.exists(_p):
            _f = open(_p)
            MANUAL_ACCOUNT = _f.read().strip()
            _f.close()
            if MANUAL_ACCOUNT:
                break
except Exception:
    pass

CODE = '301717.SZ'
# A symbol with a known ex-dividend / split history, used to detect
# whether dividend_type actually shifts prices.
EXDIV_CODE = '300308.SZ'
EXDIV_START = '20240603'
EXDIV_END = '20240607'

OUT_PATHS = [
    r'C:\Users\27329\qmt_probe_v3.txt',
    r'D:\qmt_probe_v3.txt',
    'qmt_probe_v3.txt',
]

_KW = ('market', 'data', 'trade', 'account', 'order', 'download',
       'date', 'calendar', 'divid', 'factor', 'position', 'universe',
       'bench', 'bar', 'price', 'volume')

_MEM = []
_OUT_PATH = [None]
_BAR_DONE = [False]


def _flush():
    for p in OUT_PATHS:
        try:
            f = open(p, 'w')
            f.write('\n'.join(_MEM) + '\n')
            f.close()
            _OUT_PATH[0] = p
            return True
        except Exception:
            continue
    return False


def _w(msg):
    msg = str(msg)
    line = ('[PROBE] ' + msg) if msg else ''
    _MEM.append(line)
    try:
        print(line)
    except Exception:
        pass
    _flush()


def _sec(name):
    _w('')
    _w('=== ' + name + ' ===')


def _try(tag, fn):
    try:
        _w('%-30s %s' % (tag, fn()))
    except Exception as e:
        _w('%-30s FAIL %s: %s' % (tag, type(e).__name__, e))


def _mask(s):
    s = str(s)
    if len(s) <= 4:
        return s
    return s[:2] + '*' * (len(s) - 4) + s[-2:]


def init(ContextInfo):
    _w('qmt probe v3 start  %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    _sec('1 environment')
    _try('python', lambda: sys.version.replace('\n', ' '))
    _try('cwd', lambda: os.getcwd())
    _try('do_back_test', lambda: ContextInfo.do_back_test)
    _try('period', lambda: ContextInfo.period)
    _try('barpos', lambda: ContextInfo.barpos)
    _try('stockcode', lambda: ContextInfo.stockcode)
    _try('benchmark', lambda: ContextInfo.benchmark)

    _sec('2 ContextInfo api inventory')
    names = []
    for a in dir(ContextInfo):
        if a.startswith('_'):
            continue
        low = a.lower()
        if any(k in low for k in _KW):
            names.append(a)
    _w('hits=%d' % len(names))
    for a in sorted(names):
        _w('  ctx.%s' % a)

    _sec('3 module global inventory')
    g = sorted([n for n in globals().keys() if not n.startswith('_')])
    builtin_names = set(dir(__builtins__)) if not isinstance(__builtins__, dict) else set(__builtins__)
    mine = [n for n in g if n not in builtin_names]
    _w('non-builtin globals=%d' % len(mine))
    _w('  ' + ', '.join(mine))
    for n in ('get_trade_detail_data', 'passorder', 'order_shares', 'order_value',
              'get_market_data', 'get_market_data_ex', 'download_history_data',
              'is_trading_day', 'get_trade_dates', 'timetag_to_datetime',
              'get_divid_factors'):
        _w('%-28s %s' % (n, 'present' if n in globals() else 'MISSING'))

    _sec('4 trading calendar probes (presence only)')
    for cand in ('get_trade_dates', 'is_trading_day', 'get_trading_dates'):
        _w('%-28s %s' % ('ctx.' + cand,
                         'present' if hasattr(ContextInfo, cand) else 'MISSING'))
    _try('ctx.get_trade_dates()', lambda: _short(ContextInfo.get_trade_dates()))
    _try('today ym', lambda: time.strftime('%Y%m%d'))

    _w('')
    _w('init done. handlebar section follows.')
    _flush()


def _short(obj):
    s = str(obj)
    return 'type=%s len=%s val=%s' % (type(obj).__name__, len(obj) if hasattr(obj, '__len__') else 'n/a', s[:120])


def handlebar(ContextInfo):
    if _BAR_DONE[0]:
        return
    _BAR_DONE[0] = True
    _sec('5 market data depth (code=%s)' % CODE)
    for tag, kw in (
        ('1m count=240', {'period': '1m', 'count': 240}),
        ('1m 20250101-', {'period': '1m', 'start_time': '20250101', 'end_time': '', 'count': -1}),
        ('1m 20230101-', {'period': '1m', 'start_time': '20230101', 'end_time': '', 'count': -1}),
        ('1d 20250101-', {'period': '1d', 'start_time': '20250101', 'end_time': '20261103', 'count': -1}),
        ('1d 20230101-20231231', {'period': '1d', 'start_time': '20230101', 'end_time': '20231231', 'count': -1}),
        ('1d count=10000', {'period': '1d', 'count': 10000}),
    ):
        _try(tag, lambda kw=kw: _md(ContextInfo, kw))

    _sec('6 dividend_type comparison (%s %s..%s)' % (EXDIV_CODE, EXDIV_START, EXDIV_END))
    for dt in ('none', 'front', 'back', 'front_ratio', 'back_ratio'):
        _try('1d ' + dt,
             lambda dt=dt: _md_detail(ContextInfo, EXDIV_CODE, '1d',
                                      EXDIV_START, EXDIV_END, dt))
    for dt in ('none', 'front'):
        _try('1m ' + dt,
             lambda dt=dt: _md_detail(ContextInfo, EXDIV_CODE, '1m',
                                      EXDIV_START, EXDIV_END, dt))

    _sec('7 account / position (LIVE only)')
    _acct_section(ContextInfo)

    _w('')
    _w('done. result file: %s' % (_OUT_PATH[0] or 'NONE'))
    _flush()


def _md(ContextInfo, kw):
    args = dict(kw)
    res = ContextInfo.get_market_data_ex(['close'], [CODE], **args)
    df = res.get(CODE) if isinstance(res, dict) else res
    if df is None or len(df) == 0:
        return 'EMPTY'
    return 'rows=%d first=%s last=%s lastclose=%s' % (
        len(df), str(df.index[0]), str(df.index[-1]), float(df['close'].iloc[-1]))


def _md_detail(ContextInfo, code, period, start, end, dividend_type):
    res = ContextInfo.get_market_data_ex(
        ['close'], [code], period=period, start_time=start, end_time=end,
        count=-1, dividend_type=dividend_type)
    df = res.get(code) if isinstance(res, dict) else res
    if df is None or len(df) == 0:
        return 'EMPTY'
    out = ['rows=%d' % len(df)]
    idx = list(df.index)
    show = idx[:2] + idx[-2:] if len(idx) > 4 else idx
    for i in show:
        val = df['close'].loc[i]
        try:
            val = float(val)
        except Exception:
            val = float(val.iloc[0])
        out.append('%s=%.4f' % (str(i), val))
    return ' '.join(out)


def _acct_section(ContextInfo):
    cands = []
    for attr in dir(ContextInfo):
        low = attr.lower()
        if 'acc' not in low and 'account' not in low:
            continue
        try:
            v = getattr(ContextInfo, attr)
        except Exception:
            continue
        if isinstance(v, str) and v.strip():
            cands.append((attr, v.strip()))
    if cands:
        for a, v in cands:
            _w('ctx attr %-14s = %s (len=%d)' % (a, _mask(v), len(v)))
    else:
        _w('no account-like attribute on ContextInfo (expected in BACKTEST)')

    ids = list(cands)
    if MANUAL_ACCOUNT:
        ids.insert(0, ('MANUAL', MANUAL_ACCOUNT))
    if not ids:
        _w('SKIP: no account id candidate. Re-run in LIVE mode to get this section.')
        return

    setacc = getattr(ContextInfo, 'set_account', None)
    _w('ContextInfo.set_account available: %s' % ('YES' if callable(setacc) else 'NO'))

    for tag, aid in ids:
        _w('--- try id %s (len=%d) ---' % (_mask(aid), len(aid)))
        if callable(setacc):
            try:
                setacc(aid)
                _w('set_account -> ok')
            except Exception as e:
                _w('set_account -> FAIL %s: %s' % (type(e).__name__, e))
        seen = {}
        for atype in ('stock', 'STOCK', 'credit', 'future'):
            for dtype in ('ACCOUNT', 'POSITION', 'ORDER', 'DEAL'):
                if atype != 'stock' and dtype != 'ACCOUNT':
                    continue
                try:
                    rows = get_trade_detail_data(aid, atype, dtype)
                    n = 0 if rows is None else len(rows)
                except Exception as e:
                    _w('  %-7s %-9s FAIL %s: %s'
                       % (atype, dtype, type(e).__name__, e))
                    continue
                _w('  %-7s %-9s rows=%d' % (atype, dtype, n))
                if n and dtype not in seen:
                    seen[dtype] = True
                    _dump_fields(dtype, rows[0])
        _w('  local now=%s' % time.strftime('%Y-%m-%d %H:%M:%S'))


def _dump_fields(dtype, obj):
    for attr in dir(obj):
        if attr.startswith('_'):
            continue
        try:
            v = getattr(obj, attr)
        except Exception:
            continue
        if isinstance(v, (int, float, str)):
            low = attr.lower()
            shown = _mask(v) if ('accountid' in low or 'accountkey' in low) else v
            _w('    %s.%s = %s' % (dtype, attr, shown))
