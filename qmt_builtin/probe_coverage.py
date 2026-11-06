# ============================================================
# QMT coverage probe  (2026-11-06)  ---  for 300319 only
#
# PURPOSE
#   QMT local DB has almost nothing for 300319 (1m ~175 bars from
#   today's subscription, no daily file at all). This probe asks the
#   API year by year and reports where REAL data actually starts.
#   Filler rows are detected by volume == 0 (QMT pads missing history
#   with the latest known price and zero volume).
#
# RUN
#   Safe in BACKTEST mode from the strategy editor: it does not touch
#   the live trading session. Period 1m, any date range, will run once.
#
# NO ORDERS. NO FILE IO. OUTPUT VIA print() ONLY.
# ============================================================

CODE = '300319.SZ'
_DAILY_YEARS = ('2015', '2020', '2023', '2024', '2025', '2026')
_MIN_YEARS = ('2021', '2022', '2023', '2024', '2025', '2026')
_DONE = [False]


def _w(s):
    try:
        print('[CV] ' + str(s))
    except Exception:
        pass


def _scan(C, period, start, end):
    try:
        res = C.get_market_data_ex(
            ['open', 'high', 'low', 'close', 'volume'], [CODE],
            period=period, start_time=start, end_time=end,
            count=-1, dividend_type='none')
    except Exception as e:
        return 'FAIL %s: %s' % (type(e).__name__, e)
    df = res.get(CODE) if isinstance(res, dict) else None
    if df is None or len(df) == 0:
        return 'EMPTY'
    try:
        nz = int((df['volume'] > 0).sum())
        idx = list(df.index)
        first_nz = None
        for i in range(len(df)):
            try:
                if float(df['volume'].iloc[i]) > 0:
                    first_nz = str(df.index[i])
                    break
            except Exception:
                continue
        return ('rows=%d nz=%d first=%s firstNZ=%s last=%s'
                % (len(df), nz, idx[0], first_nz or '-', idx[-1]))
    except Exception as e:
        return 'PARSE FAIL %s: %s' % (type(e).__name__, e)


def init(ContextInfo):
    _w('coverage probe start 300319')
    _w('backtest=%s period=%s' % (getattr(ContextInfo, 'do_back_test', '?'),
                                  getattr(ContextInfo, 'period', '?')))
    _w('handler planned: handlebar does the scan (once)')
    _w('csv,period,year,result')


def handlebar(ContextInfo):
    if _DONE[0]:
        return
    _DONE[0] = True

    _w('==== 1d by year ====')
    _w('1d,ALL,%s' % _scan(ContextInfo, '1d', '20100101', '20261231'))
    for y in _DAILY_YEARS:
        _w('1d,%s,%s' % (y, _scan(ContextInfo, '1d', y + '0101', y + '1231')))

    _w('==== 1m by year ====')
    for y in _MIN_YEARS:
        _w('1m,%s,%s' % (y, _scan(ContextInfo, '1m', y + '0101', y + '1231')))
    _w('1m,LAST300BARS,%s' % _scan(ContextInfo, '1m', '', ''))

    _w('==== done ====')
