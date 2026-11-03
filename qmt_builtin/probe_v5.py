# ============================================================
# QMT builtin probe v5  (2026-11-03)  DOWNLOAD + VERIFY
#
# WHY v5: v4 proved every history query returns FILLED bars
# (identical OHLC, volume=0) because datadir has no data:
#   SZ/86400/300308.DAT = 72 bytes  (daily, empty)
#   SZ/86400/301717.DAT = 72 bytes  (daily, empty)
#   SZ/60/300308.DAT    = 2.2 MB    (minute, only up to 10-12)
# So we call download_history_data, wait, then re-read and verify
# that volume > 0 and prices actually move.
#
# SCOPE (confirmed with user, ~30-50 MB):
#   300308 daily 2024-01..2026-12 / minute 2024-01..2026-12
#   301717 daily 2018-01..2026-12 / minute 2026-01..2026-12
#
# NO ORDERS. Output: C:\Users\27329\qmt_probe_v5.txt
# ============================================================

import os
import time

DATADIR = ('D:\\' + '\u8fc5\u6295' + 'QMT' + '\u4ea4\u6613\u7ec8\u7aef'
           + '   ' + '\u56fd\u76db\u8bc1\u5238\u5b9e\u76d8\u7248' + '\\datadir')

OUT_PATHS = [
    os.environ.get('QMT_PROBE_OUT', '') or r'C:\Users\27329\qmt_probe_v5.txt',
]

JOBS = [
    ('300308.SZ', '1d', '20240101', '20261231', 'SZ', '86400', '300308.DAT'),
    ('300308.SZ', '1m', '20240101', '20261231', 'SZ', '60', '300308.DAT'),
    ('301717.SZ', '1d', '20180101', '20261231', 'SZ', '86400', '301717.DAT'),
    ('301717.SZ', '1m', '20260101', '20261231', 'SZ', '60', '301717.DAT'),
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
    _MEM.append(('[PROBE] ' + str(msg)) if str(msg) else '')
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
        _w('%-34s %s' % (tag, fn()))
    except Exception as e:
        _w('%-34s FAIL %s: %s' % (tag, type(e).__name__, e))


def _sz(sub, folder, name):
    p = os.path.join(DATADIR, sub, folder, name)
    try:
        return '%s bytes' % os.path.getsize(p)
    except Exception:
        return 'MISSING'


def init(ContextInfo):
    _w('qmt probe v5 start %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    _sec('1 before: datadir file sizes')
    for code, per, s, e, sub, folder, name in JOBS:
        _w('%-22s %-8s %s' % (sub + '/' + folder + '/' + name, per,
                              _sz(sub, folder, name)))
    _try('ctx.dividend_type', lambda: repr(ContextInfo.dividend_type))
    _w('')
    _w('init done, downloads run in handlebar')
    _flush()


def handlebar(ContextInfo):
    if _DONE[0]:
        return
    _DONE[0] = True

    for code, per, start, end, sub, folder, name in JOBS:
        _sec('%s %s %s..%s' % (code, per, start, end))
        _w('before: %s' % _sz(sub, folder, name))
        ok = _download(code, per, start, end)
        _w('call returned: %s' % ok)
        _w('after : %s' % _sz(sub, folder, name))
        _w('wait  : %s' % _wait(ContextInfo, code, per, start, end))
        _w(_verify(ContextInfo, code, per, start, end))

    _sec('5 dividend check after download (300308 2024-06)')
    for dt in ('none', 'back'):
        _w(dt + ': ' + _peek(ContextInfo, '300308.SZ', '1d', '20240603',
                             '20240607', dt))

    _sec('6 final file sizes')
    for code, per, s, e, sub, folder, name in JOBS:
        _w('%-30s %-4s %s' % (sub + '/' + folder + '/' + name, per,
                              _sz(sub, folder, name)))

    _w('')
    _w('done. result file: %s' % (_OUT_PATH[0] or 'NONE'))
    _flush()


def _download(code, period, start, end):
    fn = globals().get('download_history_data')
    if fn is None:
        return 'MISSING download_history_data'
    try:
        fn(code, period, start, end)
        return 'ok'
    except Exception as e:
        try:
            fn(code, period, start, end, False)
            return 'ok (5 args)'
        except Exception as e2:
            return 'FAIL %s: %s | %s: %s' % (type(e).__name__, e,
                                             type(e2).__name__, e2)


def _frames(ContextInfo, code, period, start, end, dt):
    res = ContextInfo.get_market_data_ex(
        ['open', 'high', 'low', 'close', 'volume'], [code], period=period,
        start_time=start, end_time=end, count=-1, dividend_type=dt)
    df = res.get(code) if isinstance(res, dict) else None
    return df


def _wait(ContextInfo, code, period, start, end, max_s=45, step=3):
    """download_history_data can be async; poll until volume>0 shows up."""
    t0 = time.time()
    last = None
    while time.time() - t0 < max_s:
        df = _frames(ContextInfo, code, period, start, end, 'none')
        if df is not None and len(df):
            nz = int((df['volume'] > 0).sum())
            last = 'rows=%d nonzero_vol=%d' % (len(df), nz)
            if nz > 0:
                return 'ready after %ds (%s)' % (int(time.time() - t0), last)
        time.sleep(step)
    return 'timeout %ds, last=%s' % (max_s, last)


def _verify(ContextInfo, code, period, start, end):
    df = _frames(ContextInfo, code, period, start, end, 'none')
    if df is None or len(df) == 0:
        return 'verify: EMPTY'
    vol = df['volume']
    nonzero = int((vol > 0).sum())
    # how many distinct closes in the first 200 rows: filled data gives 1
    tail = df['close'].tail(min(200, len(df)))
    distinct = len(set([round(float(x), 4) for x in tail]))
    return ('verify: rows=%d nonzero_vol=%d distinct_close_last200=%d '
            'sample_last=%s' % (len(df), nonzero, distinct,
                                _row(df, -1)))


def _peek(ContextInfo, code, period, start, end, dt):
    df = _frames(ContextInfo, code, period, start, end, dt)
    if df is None or len(df) == 0:
        return 'EMPTY'
    out = ['rows=%d' % len(df)]
    for i in range(min(3, len(df))):
        out.append(_row(df, i))
    out.append('last=' + _row(df, -1))
    return ' | '.join(out)


def _row(df, i):
    try:
        r = df.iloc[i]
        return '%s o=%s c=%s v=%s' % (df.index[i], r['open'], r['close'],
                                      r['volume'])
    except Exception as e:
        return 'row(%d) FAIL %s' % (i, e)
