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
#   V2 records: sequence, kind, hex-encoded ASCII Python literal.
#   END seals the preceding record count/checksum. This detects damage,
#   NOT source authenticity. Legacy records are never upgraded in place.
# ============================================================

import time

CODE = '300319.SZ'
ACCOUNT_ID_HARD = ''

TABLES = ('ACCOUNT', 'POSITION', 'ORDER', 'DEAL')
REQUIRED = {
    'ACCOUNT': ('m_strAccountID', 'm_dAvailable', 'm_dBalance'),
    'POSITION': ('m_strAccountID', 'm_strExchangeID', 'm_strInstrumentID',
                 'm_nVolume', 'm_nCanUseVolume', 'm_dLastPrice'),
    'ORDER': ('m_strAccountID', 'm_strExchangeID', 'm_strInstrumentID',
              'm_strOrderSysID', 'm_strInsertDate', 'm_strInsertTime',
              'm_nOffsetFlag', 'm_nOrderStatus', 'm_nVolumeTotalOriginal',
              'm_nVolumeTraded', 'm_dLimitPrice', 'm_strOrderRef', 'm_strRemark'),
    'DEAL': ('m_strAccountID', 'm_strExchangeID', 'm_strInstrumentID',
             'm_strOrderSysID', 'm_strTradeID', 'm_strTradeDate',
             'm_strTradeTime', 'm_nOffsetFlag', 'm_nVolume', 'm_dPrice'),
}

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
_WIRE = {'n': 0, 'checksum': 0}


def _w(s):
    try:
        print('[SNAP] ' + str(s))
    except Exception:
        pass


def _emit(kind, data):
    # ascii() preserves whitespace/unicode/type without delimiter ambiguity.
    payload = ascii(data).encode('ascii').hex()
    line = 'V2 n=%d kind=%s data=%s' % (_WIRE['n'], kind, payload)
    _w(line)
    for char in line:
        _WIRE['checksum'] = (_WIRE['checksum'] * 33 + ord(char)) & 0xffffffff
    _WIRE['n'] += 1


def _violation(name):
    def _blocked(*a, **k):
        _w('VIOLATION order fn called: %s args=%r' % (name, a[:3]))
        raise RuntimeError('snapshot collector: %s is blocked' % name)
    return _blocked


def _fields(obj, table):
    """All m_* non-callable attributes, name-sorted for stable output.
    dir() on these objects also yields enum constants, hence the prefix
    filter; m_dAvgOpenPrice may carry the DBL_MAX sentinel, so floats are
    emitted with repr() to keep full precision."""
    out, errors = [], []
    try:
        names = dir(obj)
    except Exception:
        return out, ['dir_failed']
    for name in names:
        if not name.startswith('m_'):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            errors.append('unreadable_' + name)
            continue
        if callable(value):
            continue
        # Native bindings can return int/float subclasses (including enums).
        # Copy their primitive value; do not serialize binding-specific repr.
        if isinstance(value, bool):
            value = bool(value)
        elif isinstance(value, int):
            value = int(value)
        elif isinstance(value, float):
            value = float(value)
        elif isinstance(value, str):
            value = str(value)
        elif value is not None:
            errors.append('unsupported_' + name)
            continue
        if isinstance(value, float) and not (-1e309 < value < 1e309):
            errors.append('nonfinite_' + name)
            continue
        out.append((name, value))
    out.sort()
    found = dict(out)
    errors.extend('missing_' + name for name in REQUIRED[table]
                  if name not in found or found[name] is None)
    return out, errors


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
    canon, failures = [], []
    round_start = time.time()
    for table in TABLES:
        rows, status, t0, api_t1 = _read_table(aid, table)
        extracted, errors = [], []
        for obj in rows:
            pairs, field_errors = _fields(obj, table)
            extracted.append(pairs)
            errors.extend(field_errors)
        # Include reading object fields in the evidence window, not only
        # returning native object references from the API.
        t1 = time.time()
        if table == 'ACCOUNT' and len(rows) != 1:
            errors.append('account_row_count')
        if errors and status == 'OK':
            status = 'FIELDS'
        if status != 'OK':
            failures.append('%d_%s_%s' % (rnd, table, status))
        _emit('Q', dict(r=rnd, t=table, n=len(rows), st=status,
                        t0=t0, t1=t1, ms=int((t1 - t0) * 1000),
                        api_t1=api_t1, api_ms=int((api_t1 - t0) * 1000), errors=errors))
        for index in range(len(rows)):
            pairs = extracted[index]
            _emit('D', dict(r=rnd, t=table, k=index, f=len(pairs), fields=pairs))
            canon.append((table, index, tuple(pairs)))
    round_end = time.time()
    canon.sort()
    return canon, round_start, round_end, failures


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
    _WIRE.update(n=0, checksum=0)
    _emit('BEGIN', dict(version=2, code=CODE, account=aid, epoch=time.time(),
                        tables=TABLES, rounds=2))
    if not aid:
        _emit('END', dict(records=_WIRE['n'], checksum=_WIRE['checksum'],
                          stable=False, equal=False, errors=['no_account']))
        return

    a_start = time.time()
    canon_a, r1s, r1e, errors_a = _collect_round(aid, 1)
    canon_b, r2s, r2e, errors_b = _collect_round(aid, 2)
    a_end = time.time()

    span_ms = int((a_end - a_start) * 1000)
    r1_ms = int((r1e - r1s) * 1000)
    r2_ms = int((r2e - r2s) * 1000)
    gap_ms = int((r2s - r1e) * 1000)

    diffs, first = _compare(canon_a, canon_b)
    errors = errors_a + errors_b
    _emit('END', dict(records=_WIRE['n'], checksum=_WIRE['checksum'],
                      rows=len(canon_a), span_ms=span_ms, r1_ms=r1_ms,
                      r2_ms=r2_ms, gap_ms=gap_ms, equal=diffs == 0,
                      stable=diffs == 0 and not errors, diffs=diffs,
                      first=first, errors=errors, finished_epoch=a_end))
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
                _collect()
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
