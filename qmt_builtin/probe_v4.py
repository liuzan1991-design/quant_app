# ============================================================
# QMT builtin probe v4  (2026-11-03)  READ-ONLY, no orders
#
# WHY v4: v3 section 6 printed identical closes for every date,
# which cannot be true for a real price series. Before drawing any
# conclusion about dividend_type we need the RAW return structure.
# Also: v3 showed 1d HAS history (443 rows) while count= gave 1 row,
# so count semantics need pinning down with raw index dumps.
#
# OUTPUT: C:\Users\27329\qmt_probe_v4.txt  (override with QMT_PROBE_OUT)
# ============================================================

import sys
import os
import time

CODE_A = '300308.SZ'   # has a known ex-div on 2024-06-06 (10-for-4 split)
CODE_B = '301717.SZ'   # live symbol, listing date unknown to us
CODE_C = '600519.SH'   # control: plain blue chip, no event in range

OUT_PATHS = [
    os.environ.get('QMT_PROBE_OUT', '') or r'C:\Users\27329\qmt_probe_v4.txt',
]

_MEM = []
_OUT_PATH = [None]
_DONE = [False]


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
    _MEM.append(('[PROBE] ' + msg) if msg else '')
    try:
        print(_MEM[-1])
    except Exception:
        pass
    _flush()


def _sec(n):
    _w('')
    _w('=== ' + n + ' ===')


def _try(tag, fn):
    try:
        _w('%-32s %s' % (tag, fn()))
    except Exception as e:
        _w('%-32s FAIL %s: %s' % (tag, type(e).__name__, e))


def init(ContextInfo):
    _w('qmt probe v4 start %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    _sec('1 context dividend settings')
    for a in ('dividend_type', 'market', 'period', 'do_back_test', 'data_info_level'):
        _try('ctx.' + a, lambda a=a: repr(getattr(ContextInfo, a)))
    _sec('2 trading dates api')
    for args in ((), ('SH',), ('SH', '20261101', '20261110'),
                 ('20261101', '20261110'), ('SH', 20261101, 20261110)):
        _try('ctx.get_trading_dates%r' % (args,),
             lambda args=args: _summ(getattr(ContextInfo, 'get_trading_dates')(*args)))
    _sec('3 dividend factors api')
    for args in ((CODE_A,), (CODE_A, '20240101', '20261231')):
        _try('ctx.get_divid_factors%r' % (args,),
             lambda args=args: _summ(ContextInfo.get_divid_factors(*args)))
    _w('')
    _w('init done, handlebar follows')
    _flush()


def _summ(o):
    t = type(o).__name__
    if hasattr(o, 'shape'):
        return 'type=%s shape=%s cols=%s index[:6]=%s' % (
            t, o.shape, list(o.columns)[:6], list(o.index)[:6])
    if hasattr(o, '__len__'):
        s = list(o)[:6] if not isinstance(o, dict) else list(o.items())[:6]
        return 'type=%s len=%d sample=%s' % (t, len(o), s)
    return 'type=%s val=%s' % (t, str(o)[:200])


def handlebar(ContextInfo):
    if _DONE[0]:
        return
    _DONE[0] = True

    _sec('4 raw frame: 300308 1d 2024-06-03..06-07  dividend=none')
    _raw(ContextInfo, CODE_A, '1d', '20240603', '20240607', 'none')
    _sec('5 raw frame: same range  dividend=back')
    _raw(ContextInfo, CODE_A, '1d', '20240603', '20240607', 'back')

    _sec('6 raw frame: 301717 1d 2026-10-28..11-03  none')
    _raw(ContextInfo, CODE_B, '1d', '20261028', '20261103', 'none')
    _sec('7 raw frame: 600519 1d 2024-06-03..06-07  none (control)')
    _raw(ContextInfo, CODE_C, '1d', '20240603', '20240607', 'none')

    _sec('8 301717 early-2023 existence (listing check)')
    _raw(ContextInfo, CODE_B, '1d', '20230101', '20230120', 'none')
    _raw(ContextInfo, CODE_B, '1m', '20230103', '20230105', 'none')

    _sec('9 raw frame: 300308 1m 2024-06-05 09:30..09:35')
    _raw(ContextInfo, CODE_A, '1m', '20240605', '20240605', 'none', head=8)

    _w('')
    _w('done. result file: %s' % (_OUT_PATH[0] or 'NONE'))
    _flush()


def _raw(ContextInfo, code, period, start, end, dt, head=12):
    _w('-- %s %s %s..%s dt=%s' % (code, period, start, end, dt))
    try:
        res = ContextInfo.get_market_data_ex(
            ['open', 'high', 'low', 'close', 'volume'], [code], period=period,
            start_time=start, end_time=end, count=-1, dividend_type=dt)
    except Exception as e:
        _w('   CALL FAIL %s: %s' % (type(e).__name__, e))
        return
    _w('   ret type=%s' % type(res).__name__)
    if not isinstance(res, dict):
        _w('   non-dict ret: %s' % str(res)[:200])
        return
    _w('   keys=%s' % list(res.keys()))
    df = res.get(code)
    if df is None:
        _w('   key %s missing, fallback first value' % code)
        df = list(res.values())[0] if res else None
    if df is None:
        _w('   EMPTY')
        return
    _w('   shape=%s cols=%s' % (tuple(df.shape), list(df.columns)[:8]))
    idx = list(df.index)
    _w('   index type=%s len=%d first=%s last=%s' % (
        type(df.index).__name__, len(idx), idx[0] if idx else None,
        idx[-1] if idx else None))
    n = min(head, len(df))
    for i in range(n):
        row = df.iloc[i]
        vals = []
        for c in ('open', 'high', 'low', 'close', 'volume'):
            if c in df.columns:
                vals.append('%s=%s' % (c, row[c]))
        _w('   [%s] %s' % (idx[i], ' '.join(vals)))
