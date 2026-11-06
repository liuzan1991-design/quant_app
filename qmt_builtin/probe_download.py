# ============================================================
# QMT history downloader + verifier  (2026-11-06)  --- 300319
#
# WHY
#   QMT local DB for 300319 has only today's 185 bars (from the live
#   subscription); 1m 2021-2025 are pure filler (volume==0) and there
#   is no daily file at all. This script calls download_history_data
#   then re-scans coverage every ~20s so progress is visible in the log.
#
# RANGE (deliberately narrow)
#   1m : last ~1 year. 2021-2025 proved to be filler, so downloading
#        them is wasted time (~880k of the 930k bars user first picked).
#   1d : from 2023-01-01 (matches the known real boundary on 300308).
#
# NO ORDERS. NO FILE IO. OUTPUT VIA print() ONLY.
# NOTE: download_history_data compliance under the 2026-11-24 sandbox is
#       UNVERIFIED (spec section 9, item V2). QMT-native market data
#       channel, not external network.
# ============================================================

import time

CODE = '300319.SZ'
M1_START = '20251101'
M1_END = '20261231'
D1_START = '20230101'
D1_END = '20261231'

_ST = {'triggered': False, 'last_scan': 0.0, 'scans': 0}


def _w(s):
    try:
        print('[DL] ' + str(s))
    except Exception:
        pass


def _try_download():
    """Try known signatures in order; report which one was accepted."""
    attempts = [
        ('4-arg incremental', lambda: download_history_data(CODE, '1m', M1_START, M1_END)),
        ('4-arg explicit', lambda: download_history_data(CODE, '1m', M1_START, M1_END, True)),
        ('2-arg default', lambda: download_history_data(CODE, '1m')),
    ]
    for name, fn in attempts:
        try:
            fn()
            _w('download_history_data OK via [%s]' % name)
            return name
        except Exception as e:
            _w('download_history_data FAIL [%s]: %s: %s' % (name, type(e).__name__, e))
    return None


def _try_download_daily():
    for name, fn in (
        ('4-arg', lambda: download_history_data(CODE, '1d', D1_START, D1_END)),
        ('2-arg', lambda: download_history_data(CODE, '1d')),
    ):
        try:
            fn()
            _w('daily download OK via [%s]' % name)
            return name
        except Exception as e:
            _w('daily download FAIL [%s]: %s: %s' % (name, type(e).__name__, e))
    return None


def _scan(C, period, start, end):
    try:
        res = C.get_market_data_ex(
            ['open', 'high', 'low', 'close', 'volume'], [CODE],
            period=period, start_time=start, end_time=end,
            count=-1, dividend_type='none')
    except Exception as e:
        return 'FAIL %s' % type(e).__name__
    df = res.get(CODE) if isinstance(res, dict) else None
    if df is None or len(df) == 0:
        return 'EMPTY'
    try:
        nz = int((df['volume'] > 0).sum())
        first_nz = '-'
        for i in range(len(df)):
            try:
                if float(df['volume'].iloc[i]) > 0:
                    first_nz = str(df.index[i])
                    break
            except Exception:
                continue
        return ('rows=%d nz=%d first=%s firstNZ=%s last=%s'
                % (len(df), nz, str(df.index[0]), first_nz, str(df.index[-1])))
    except Exception as e:
        return 'PARSE FAIL %s' % type(e).__name__


def init(ContextInfo):
    _w('=== downloader for %s ===' % CODE)
    _w('range: 1m %s~%s | 1d %s~%s' % (M1_START, M1_END, D1_START, D1_END))
    _w('scans every 20s; watch nz grow. stop the strategy when done.')


def handlebar(ContextInfo):
    now = time.time()

    if not _ST['triggered']:
        _ST['triggered'] = True
        _w('--- trigger downloads ---')
        _try_download()
        _try_download_daily()
        _w('--- before ---')
        _w('1m %s' % _scan(ContextInfo, '1m', M1_START, M1_END))
        _w('1d %s' % _scan(ContextInfo, '1d', D1_START, D1_END))
        _ST['last_scan'] = now
        return

    if now - _ST['last_scan'] >= 20:
        _ST['last_scan'] = now
        _ST['scans'] += 1
        _w('scan#%d  1m %s' % (_ST['scans'], _scan(ContextInfo, '1m', M1_START, M1_END)))
        _w('scan#%d  1d %s' % (_ST['scans'], _scan(ContextInfo, '1d', D1_START, D1_END)))
        if _ST['scans'] >= 30:
            _w('=== 30 scans done, stop me ===')
