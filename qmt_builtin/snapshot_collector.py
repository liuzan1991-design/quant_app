# ============================================================
# QMT dual-round read-only snapshot collector   (2026-11-07)
#
# SCOPE
#   Stage-0 evidence collector for the state-rebuild contract.
#   Reads ACCOUNT / POSITION / ORDER / DEAL **completely, twice**,
#   prints every row and every query's real start/end time, then
#   prints an internal round-comparison verdict.
#
# HARD RULES
#   - 24 order / cancel globals replaced with raising stubs in init()
#   - pure ASCII, import time only
#   - zero file IO, zero network, output via print() only
#   - rows are NOT filtered: no target-symbol pre-selection, no
#     "no remark means manual" assumption, no rewritten timestamps
#   - this file is a SEPARATE strategy; it never touches
#     RO_301717.py and never stops or alters the running instance
#
# DEPLOY
#   ACCOUNT_ID_HARD is empty in git. The deployed copy under
#   <QMT>\python\SNAP_300319.py has the real account id injected.
#
# EXTERNAL PARSE
#   Every line starts with "[SNAP] ". Line kinds:
#     BEGIN / Q (per query timing) / D (one data row) / END / VERDICT
#   Field pairs are "name=value", separated by single spaces,
#   values have "," replaced by "~" so a comma split stays safe.
# ============================================================

import time

CODE = '300319.SZ'
ACCOUNT_ID_HARD = ''

TABLES = ('ACCOUNT', 'POSITION', 'ORDER', 'DEAL')

# Collection window is expressed in BEIJING time and derived from the
# epoch (UTC+8), never from the machine timezone. The machine here runs
# GMT+3, so local 03:50 == Beijing 08:50.
WIN_BJ_START = '08:50'
WIN_BJ_END = '09:05'
RUN_ANYTIME = 0

# Collect a second independent snapshot this many seconds after the first.
SECOND_AFTER = 0

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

_ST = {'done': 0, 'bars': 0, 'guard': [], 'first_end': 0.0}


def _w(s):
    try:
        print('[SNAP] ' + str(s))
    except Exception:
        pass


def _stamp(t):
    return '%s.%03d' % (time.strftime('%H:%M:%S', time.localtime(t)),
                        int((t % 1) * 1000))


def _fmt(v):
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return v.replace(',', '~').replace('\r', ' ').replace('\n', ' ')
    return str(v).replace(',', '~')


def _violation(name):
    def _blocked(*a, **k):
        _w('VIOLATION order fn called: %s args=%r' % (name, a[:3]))
        raise RuntimeError('snapshot collector: %s is blocked' % name)
    return _blocked


def _mask(s):
    s = str(s)
    return s if len(s) <= 4 else s[:2] + '*' * (len(s) - 4) + s[-2:]


def _fields(obj):
    """All m_* non-callable attributes, name-sorted for stable output.
    dir() on these objects also yields enum constants, hence the prefix
    filter; m_dAvgOpenPrice may carry the DBL_MAX sentinel, so floats are
    emitted with repr() to keep full precision."""
    out = []
    try:
        names = dir(obj)
    except Exception:
        return out
    for name in names:
        if not name.startswith('m_'):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        out.append((name, value))
    out.sort()
    return out


def _read_table(aid, table):
    t0 = time.time()
    status = 'OK'
    rows = []
    try:
        got = get_trade_detail_data(aid, 'stock', table)
        if got is None:
            status = 'NONE'
            rows = []
        else:
            rows = list(got)
    except Exception as e:
        status = 'FAIL:' + type(e).__name__
    t1 = time.time()
    return rows, status, t0, t1


def _collect_round(aid, rnd):
    """One complete pass over all four tables. Returns canonical rows for
    comparison plus the round span."""
    canon = []
    round_start = time.time()
    for table in TABLES:
        rows, status, t0, t1 = _read_table(aid, table)
        _w('Q r=%d t=%s n=%d st=%s t0=%s t1=%s ms=%d'
           % (rnd, table, len(rows), status, _stamp(t0), _stamp(t1),
              int((t1 - t0) * 1000)))
        if status != 'OK':
            _w('Q r=%d t=%s ABORT=1' % (rnd, table))
        for index in range(len(rows)):
            pairs = _fields(rows[index])
            payload = ' '.join('%s=%s' % (k, _fmt(v)) for k, v in pairs)
            _w('D r=%d t=%s k=%d f=%d %s' % (rnd, table, index, len(pairs), payload))
            canon.append((table, index, tuple((k, _fmt(v)) for k, v in pairs)))
    round_end = time.time()
    canon.sort()
    return canon, round_start, round_end


def _compare(a, b):
    if a == b:
        return 0, ''
    if len(a) != len(b):
        return -1, 'row_count a=%d b=%d' % (len(a), len(b))
    diffs = 0
    first = ''
    for ra, rb in zip(a, b):
        if ra == rb:
            continue
        diffs += 1
        if not first:
            if ra[0] != rb[0] or ra[1] != rb[1]:
                first = '%s%d_vs_%s%d' % (ra[0], ra[1], rb[0], rb[1])
            else:
                fa = dict(ra[2])
                fb = dict(rb[2])
                keys = sorted(set(fa) | set(fb))
                for k in keys:
                    if fa.get(k) != fb.get(k):
                        first = '%s%d.%s %s|%s' % (ra[0], ra[1], k,
                                                   fa.get(k), fb.get(k))
                        break
    return diffs, first


def _collect():
    aid = ACCOUNT_ID_HARD
    _w('BEGIN code=%s acct=%s local=%s asof_epoch=%.3f anytime=%d'
       % (CODE, _mask(aid) if aid else 'MISSING',
          time.strftime('%Y-%m-%d %H:%M:%S'), time.time(), RUN_ANYTIME))
    if not aid:
        _w('END aborted=1 reason=no_account')
        return

    a_start = time.time()
    canon_a, r1s, r1e = _collect_round(aid, 1)
    canon_b, r2s, r2e = _collect_round(aid, 2)
    a_end = time.time()

    span_ms = int((a_end - a_start) * 1000)
    r1_ms = int((r1e - r1s) * 1000)
    r2_ms = int((r2e - r2s) * 1000)
    gap_ms = int((r2s - r1e) * 1000)

    diffs, first = _compare(canon_a, canon_b)
    _w('END rows=%d span_ms=%d r1_ms=%d r2_ms=%d gap_ms=%d equal=%d diffs=%d first=%s'
       % (len(canon_a), span_ms, r1_ms, r2_ms, gap_ms,
          1 if diffs == 0 else 0, diffs, first or '-'))
    _w('VERDICT stable=%s  (timeframe must also satisfy the external contract)'
       % ('yes' if diffs == 0 else 'no'))
    _ST['first_end'] = a_end


def _bj_hhmm():
    return time.strftime('%H:%M', time.gmtime(time.time() + 8 * 3600))


def _in_window():
    return WIN_BJ_START <= _bj_hhmm() <= WIN_BJ_END


def init(ContextInfo):
    for name in ORDER_FNS:
        if name in globals():
            globals()[name] = _violation(name)
            _ST['guard'].append(name)
    try:
        if ACCOUNT_ID_HARD:
            ContextInfo.set_account(ACCOUNT_ID_HARD)
    except Exception:
        pass
    _w('INIT %s order_fns_blocked=%d tables=%s'
       % (time.strftime('%Y-%m-%d %H:%M:%S'), len(_ST['guard']),
          ','.join(TABLES)))
    _w('INIT window_bj=%s-%s anytime=%d second_after=%d'
       % (WIN_BJ_START, WIN_BJ_END, RUN_ANYTIME, SECOND_AFTER))
    _w('INIT note: rows are unfiltered; no auto-classification of owner')


def handlebar(ContextInfo):
    _ST['bars'] += 1
    if _ST['done'] >= 1:
        if SECOND_AFTER and _ST['done'] == 1 and _ST['first_end']:
            if time.time() - _ST['first_end'] >= SECOND_AFTER:
                _ST['done'] = 2
                _w('BEGIN code=%s round_set=3 local=%s asof_epoch=%.3f'
                   % (CODE, time.strftime('%Y-%m-%d %H:%M:%S'), time.time()))
                aid = ACCOUNT_ID_HARD
                canon_c, c1s, c1e = _collect_round(aid, 3)
                canon_d, c2s, c2e = _collect_round(aid, 4)
                diffs, first = _compare(canon_c, canon_d)
                _w('END rows=%d span_ms=%d equal=%d diffs=%d first=%s'
                   % (len(canon_c), int((c2e - c1s) * 1000),
                      1 if diffs == 0 else 0, diffs, first or '-'))
        return

    if not RUN_ANYTIME and not _in_window():
        if _ST['bars'] == 1:
            _w('WAIT bar=1 bj=%s not_in_window_bj=%s-%s'
               % (_bj_hhmm(), WIN_BJ_START, WIN_BJ_END))
        return

    _ST['done'] = 1
    _w('TRIGGER bar=%d bj=%s local=%s'
       % (_ST['bars'], _bj_hhmm(), time.strftime('%H:%M:%S')))
    try:
        _collect()
    except Exception as e:
        _w('END aborted=1 reason=%s:%s' % (type(e).__name__, e))
