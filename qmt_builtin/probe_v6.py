# ============================================================
# QMT builtin probe v6  (2026-11-03)  COVERAGE MAP
#
# WHY v6: v5 downloaded data and everything now has volume>0, but
# two things must be pinned down before any porting decision:
#   1) how far back 1m data actually goes (v5 saw ~58k nonzero bars
#      for a 3-year request -> suspicious, may be a 1-year cap);
#   2) 301717 daily had only 55 nonzero bars out of 2142 rows ->
#      likely a recent IPO, which changes the whole sample story.
# Also re-checks the +1 day offset on the daily index.
#
# NO ORDERS. Output: C:\Users\27329\qmt_probe_v6.txt
# ============================================================

import os
import time

_OUT_PATHS = [
    os.environ.get('QMT_PROBE_OUT', '') or r'C:\Users\27329\qmt_probe_v6.txt',
]

DATADIR = ('D:\\' + '\u8fc5\u6295' + 'QMT' + '\u4ea4\u6613\u7ec8\u7aef'
           + '   ' + '\u56fd\u76db\u8bc1\u5238\u5b9e\u76d8\u7248' + '\\datadir')

_MEM = []
_OUT_PATH = [None]
_DONE = [False]


def _flush():
    for p in _OUT_PATHS:
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
        _w('%-30s %s' % (tag, fn()))
    except Exception as e:
        _w('%-30s FAIL %s: %s' % (tag, type(e).__name__, e))


def init(ContextInfo):
    _w('qmt probe v6 start %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    _sec('1 datadir sizes')
    for sub, folder, name in (('SZ', '60', '300308.DAT'), ('SZ', '60', '301717.DAT'),
                              ('SZ', '86400', '300308.DAT'), ('SZ', '86400', '301717.DAT')):
        p = os.path.join(DATADIR, sub, folder, name)
        try:
            _w('%-28s %d bytes' % (sub + '/' + folder + '/' + name,
                                   os.path.getsize(p)))
        except Exception:
            _w('%-28s MISSING' % (sub + '/' + folder + '/' + name))
    _w('')
    _w('init done, handlebar follows')
    _flush()


def handlebar(ContextInfo):
    if _DONE[0]:
        return
    _DONE[0] = True

    _sec('2 targeted 1m download test: 300308 2024-06')
    _w('download -> %s' % _dl('300308.SZ', '1m', '20240601', '20240630'))
    _w('coverage -> %s' % _cov(_df(ContextInfo, '300308.SZ', '1m',
                                   '20240601', '20240630')))
    _w('sample   -> %s' % _sample(_df(ContextInfo, '300308.SZ', '1m',
                                      '20240603', '20240603'), 3))

    _sec('3 1m coverage by year: 300308')
    for y in ('2024', '2025', '2026'):
        _w('%s -> %s' % (y, _cov(_df(ContextInfo, '300308.SZ', '1m',
                                     y + '0101', y + '1231'))))

    _sec('4 1m coverage by year: 301717')
    for y in ('2024', '2025', '2026'):
        _w('%s -> %s' % (y, _cov(_df(ContextInfo, '301717.SZ', '1m',
                                     y + '0101', y + '1231'))))

    _sec('5 1d coverage: 300308 from 2010')
    _w('all  -> %s' % _cov(_df(ContextInfo, '300308.SZ', '1d',
                               '20100101', '20261231')))
    for y in ('2010', '2015', '2020', '2023', '2024'):
        _w('%s -> %s' % (y, _cov(_df(ContextInfo, '300308.SZ', '1d',
                                     y + '0101', y + '1231'))))

    _sec('6 1d coverage: 301717 from 2018 (IPO probe)')
    _w('all  -> %s' % _cov(_df(ContextInfo, '301717.SZ', '1d',
                               '20180101', '20261231')))
    _w('first nonzero -> %s' % _first_live(_df(ContextInfo, '301717.SZ', '1d',
                                               '20180101', '20261231')))

    _sec('7 300308 1m tail 2026-11-03 (15:00 bar sanity)')
    _w(_sample(_df(ContextInfo, '300308.SZ', '1m', '20261103', '20261103'), 6, tail=True))

    _sec('8 daily index offset check (300308 tail)')
    _w(_sample(_df(ContextInfo, '300308.SZ', '1d', '20261028', '20261103'), 5, tail=True))

    _w('')
    _w('done. result file: %s' % (_OUT_PATH[0] or 'NONE'))
    _flush()


def _dl(code, period, start, end):
    fn = globals().get('download_history_data')
    if fn is None:
        return 'MISSING'
    try:
        fn(code, period, start, end)
        return 'ok'
    except Exception as e:
        return 'FAIL %s' % e


def _df(ContextInfo, code, period, start, end):
    res = ContextInfo.get_market_data_ex(
        ['open', 'high', 'low', 'close', 'volume'], [code], period=period,
        start_time=start, end_time=end, count=-1, dividend_type='none')
    return res.get(code) if isinstance(res, dict) else None


def _cov(df):
    if df is None or len(df) == 0:
        return 'EMPTY'
    nz = int((df['volume'] > 0).sum())
    dates = set([str(i)[:8] for i in list(df.index)])
    return ('rows=%d nonzero=%d days=%d first=%s last=%s'
            % (len(df), nz, len(dates), list(df.index)[0], list(df.index)[-1]))


def _first_live(df):
    if df is None or len(df) == 0:
        return 'EMPTY'
    for i in range(len(df)):
        if float(df['volume'].iloc[i]) > 0:
            return '%s (row %d of %d)' % (df.index[i], i, len(df))
    return 'no nonzero volume at all'


def _sample(df, n, tail=False):
    if df is None or len(df) == 0:
        return 'EMPTY'
    idx = range(len(df) - n, len(df)) if tail else range(min(n, len(df)))
    out = []
    for i in idx:
        r = df.iloc[i]
        out.append('%s o=%s c=%s v=%s' % (df.index[i], r['open'], r['close'],
                                          r['volume']))
    return ' | '.join(out)
