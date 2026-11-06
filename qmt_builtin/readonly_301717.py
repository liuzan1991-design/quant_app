# ============================================================
# QMT readonly shadow runner  v0.2  SANDBOX-PURE  (2026-11-04)
#
# WHY v0.2: the broker's 2026-11-14 compliance change disables
# inside builtin python strategies:
#   - all file IO (open / pathlib / os file ops -> raise)
#   - all external network access
# so this file must not import os, must not call open(), must not
# touch the network. Output goes to print() ONLY, which QMT writes
# into  <install>\userdata\log\XtClient_FormulaOutput_YYYYMMDD.log
# and that file is read from the OUTSIDE by our own tooling.
#
# SAFETY (physical, not by convention)
#   init() replaces every order-sending global with a stub that
#   raises RuntimeError and prints a VIOLATION line.
#
# DEPLOY NOTE
#   ACCOUNT_ID_HARD is empty in git on purpose. The copy deployed
#   under <install>\python\RO_301717.py has the real id injected.
# ============================================================

import time

CODE = '300319.SZ'   # 300319 = Majie Technology (ChiNext, listed 2012-05-23)
                     # switching instrument = change this one line only;
                     # the deployed file name stays RO_301717.py
ACCOUNT_ID_HARD = ''

# order-sending globals that must never be callable in this file
ORDER_FNS = (
    'passorder', 'algo_passorder', 'smart_algo_passorder',
    'order', 'order_shares', 'order_value', 'order_lots', 'order_percent',
    'order_target_value', 'order_target_percent',
    'buy_open', 'sell_open', 'buy_value', 'sell_value',
    'buy_close_tdayfirst', 'buy_close_ydayfirst',
    'sell_close_tdayfirst', 'sell_close_ydayfirst',
    'margincash_open', 'margincash_close', 'marginsec_open', 'marginsec_close',
    'cancel', 'do_order',
)

_STATE = {'bars': 0, 'guard': [], 'acct': '', 'last_ts': '', 'dup': 0,
          'tail_date': ''}


# ---------------- output (print only) ----------------

def _w(line):
    try:
        print('[RO] ' + line)
    except Exception:
        pass


def _mask(s):
    s = str(s)
    if len(s) <= 4:
        return s
    return s[:2] + '*' * (len(s) - 4) + s[-2:]


def _violation(name):
    def _blocked(*a, **k):
        _w('%s VIOLATION order fn called: %s args=%r'
           % (time.strftime('%H:%M:%S'), name, a[:3]))
        raise RuntimeError('readonly runner: %s is blocked' % name)
    return _blocked


# ---------------- time / tz ----------------

def to_beijing(idx, fmt='%H:%M:%S'):
    """QMT renders bar index in the machine local timezone (GMT+3 here).
    Convert through epoch, then format as Beijing (UTC+8).
    The machine offset is never hardcoded."""
    digits = ''.join(ch for ch in str(idx) if ch.isdigit())
    if len(digits) < 14:
        return '?'
    try:
        t = time.strptime(digits[:14], '%Y%m%d%H%M%S')
        return time.strftime(fmt, time.gmtime(time.mktime(t) + 8 * 3600))
    except Exception:
        return '?'


# ---------------- data ----------------

def get_bars(C, start, end):
    """Always filter filler rows: QMT fills missing history with
    volume=0 rows at the latest known price."""
    try:
        res = C.get_market_data_ex(
            ['open', 'high', 'low', 'close', 'volume'], [CODE],
            period='1m', start_time=start, end_time=end,
            count=-1, dividend_type='none')
    except Exception:
        return None
    df = res.get(CODE) if isinstance(res, dict) else None
    if df is None or len(df) == 0:
        return None
    try:
        df = df[df['volume'] > 0]
    except Exception:
        return None
    if len(df) == 0:
        return None
    return df


# ---------------- account (read only) ----------------

def acct_snapshot():
    aid = _STATE['acct']
    if not aid:
        return {}
    try:
        rows = get_trade_detail_data(aid, 'stock', 'ACCOUNT')
    except Exception:
        return {}
    if not rows:
        return {}
    a = rows[0]
    out = {}
    for k in ('m_dAvailable', 'm_dBalance', 'm_dStockValue',
              'm_dPositionProfit', 'm_strStatus'):
        try:
            out[k] = getattr(a, k)
        except Exception:
            out[k] = None
    return out


def pos_of(code_only):
    """Cost price uses m_dOpenPrice on purpose: m_dAvgOpenPrice is
    DBL_MAX (uninitialised sentinel)."""
    aid = _STATE['acct']
    if not aid:
        return {}
    try:
        rows = get_trade_detail_data(aid, 'stock', 'POSITION')
    except Exception:
        return {}
    for p in (rows or []):
        try:
            if str(getattr(p, 'm_strInstrumentID', '')) != code_only:
                continue
            return {
                'vol': int(getattr(p, 'm_nVolume', 0) or 0),
                'sellable': int(getattr(p, 'm_nCanUseVolume', 0) or 0),
                'yvol': int(getattr(p, 'm_nYesterdayVolume', 0) or 0),
                'cost': float(getattr(p, 'm_dOpenPrice', 0) or 0),
                'last': float(getattr(p, 'm_dLastPrice', 0) or 0),
            }
        except Exception:
            continue
    return {}


def all_positions():
    aid = _STATE['acct']
    if not aid:
        return []
    try:
        rows = get_trade_detail_data(aid, 'stock', 'POSITION') or []
    except Exception:
        return []
    out = []
    for p in rows:
        try:
            out.append((str(getattr(p, 'm_strExchangeID', '')),
                        str(getattr(p, 'm_strInstrumentID', '')),
                        int(getattr(p, 'm_nVolume', 0) or 0),
                        int(getattr(p, 'm_nCanUseVolume', 0) or 0)))
        except Exception:
            continue
    return out


# ---------------- shadow logic (placeholder) ----------------

def shadow_signal(df):
    """Placeholder only: proves the data path end to end.
    Real grid / ma_swing / intraday_t logic is NOT ported yet."""
    if df is None or len(df) < 20:
        return 'warmup'
    try:
        last = float(df['close'].iloc[-1])
        ma20 = float(df['close'].iloc[-20:].mean())
        dev = (last - ma20) / ma20 if ma20 else 0.0
        if dev > 0.01:
            return 'above %+.2f%%' % (dev * 100)
        if dev < -0.01:
            return 'below %+.2f%%' % (dev * 100)
        return 'flat %+.2f%%' % (dev * 100)
    except Exception:
        return 'calc_err'


# ---------------- qmt entry ----------------

def init(ContextInfo):
    for name in ORDER_FNS:
        if name in globals():
            globals()[name] = _violation(name)
            _STATE['guard'].append(name)

    _STATE['acct'] = ACCOUNT_ID_HARD
    try:
        if ACCOUNT_ID_HARD:
            ContextInfo.set_account(ACCOUNT_ID_HARD)
    except Exception:
        pass

    _w('# readonly v0.2 SANDBOX-PURE start %s'
       % time.strftime('%Y-%m-%d %H:%M:%S'))
    _w('# sink=print-only log=userdata/log/XtClient_FormulaOutput_YYYYMMDD.log')
    _w('# order fns blocked: %d [%s]'
       % (len(_STATE['guard']), ','.join(_STATE['guard'][:6])))
    _w('# account %s'
       % (_mask(ACCOUNT_ID_HARD) if ACCOUNT_ID_HARD else 'MISSING'))

    a = acct_snapshot()
    if a:
        _w('# acct avail=%.2f bal=%.2f mv=%.2f pnl=%.2f status=%s'
           % (a.get('m_dAvailable') or 0, a.get('m_dBalance') or 0,
              a.get('m_dStockValue') or 0, a.get('m_dPositionProfit') or 0,
              a.get('m_strStatus')))
    else:
        _w('# acct EMPTY (account id / login)')

    for ex, cd, v, sv in all_positions():
        _w('# pos %s%s vol=%d sellable=%d' % (ex, cd, v, sv))

    _w('# hb=RO  parse these lines externally, last line age = liveness')
    _w('bar,bar_ts,bj_date,bj_time,open,high,low,close,vol,day_cum,'
       'pos_vol,pos_sellable,avail,signal,ticks,code')


def tail_prev_day(C):
    """Backfill the last two bars of the previous trading day.

    WHY: after the close no new tick arrives, so handlebar stops firing and
    the final bar can never be promoted to "completed" (2026-11-05 lost
    AD 14:58 and 14:59). The only usable moment is the next session, when
    those bars are already final. Runs once per trading day, memory flag
    only, nothing written to disk.
    """
    for k in (1, 2, 3, 4, 5):
        d = time.strftime('%Y%m%d', time.localtime(time.time() - 86400 * k))
        df = get_bars(C, d, d)
        if df is None or len(df) < 2:
            continue
        for i in (-2, -1):
            try:
                row = df.iloc[i]
                idx = df.index[i]
                _w('TAIL,%s,%s,%s,%.4f,%.4f,%.4f,%.4f,%.0f'
                   % (str(idx), to_beijing(idx, '%Y-%m-%d'), to_beijing(idx),
                      float(row['open']), float(row['high']), float(row['low']),
                      float(row['close']), float(row['volume'])))
            except Exception:
                pass
        _w('# tail backfill done for %s (%d bars)' % (d, len(df)))
        return
    _w('# tail backfill: no prev-day data found within 5 days')


def handlebar(ContextInfo):
    _STATE['bars'] += 1

    today = time.strftime('%Y%m%d')

    # once per trading day: backfill previous session's tail bars
    if _STATE['tail_date'] != today:
        _STATE['tail_date'] = today
        try:
            tail_prev_day(ContextInfo)
        except Exception as e:
            _w('# tail backfill FAIL %s: %s' % (type(e).__name__, e))

    df = get_bars(ContextInfo, today, today)
    if df is None:
        df = get_bars(ContextInfo, '', '')

    if df is None or len(df) < 2:
        return

    # QMT calls handlebar on EVERY TICK, not once per bar.
    # df.iloc[-1] is the bar still IN PROGRESS, so the only completed
    # bar we can trust is df.iloc[-2]. Recording iloc[-1] produced
    # closes off by up to 2.6 CNY and volumes off by ~1000x on 11-04.
    try:
        idx = df.index[-2]
        row = df.iloc[-2]
    except Exception:
        return

    key = str(idx)
    if key == _STATE['last_ts']:
        _STATE['dup'] += 1
        return
    ticks = _STATE['dup'] + 1
    _STATE['dup'] = 0
    _STATE['last_ts'] = key

    try:
        o = float(row['open'])
        h = float(row['high'])
        lo = float(row['low'])
        px = float(row['close'])
        bvol = float(row['volume'])
        cum = float(df['volume'].iloc[:-1].sum())
    except Exception:
        return

    code_only = CODE.split('.')[0]
    p = pos_of(code_only)
    a = acct_snapshot()
    sig = shadow_signal(df.iloc[:-1])

    _w('%d,%s,%s,%s,%.4f,%.4f,%.4f,%.4f,%.0f,%.0f,%d,%d,%.2f,%s,%d,%s'
       % (_STATE['bars'], str(idx), to_beijing(idx, '%Y-%m-%d'),
          to_beijing(idx), o, h, lo, px, bvol, cum,
          p.get('vol', 0), p.get('sellable', 0),
          a.get('m_dAvailable') or 0.0, sig, ticks, CODE))
