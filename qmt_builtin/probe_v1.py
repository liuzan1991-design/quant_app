# ============================================================
# QMT builtin probe v1  (2026-10-30)
# READ-ONLY. No orders. One temp file write to test the boundary.
# HOW TO USE:
#   1. Fill ACCOUNT_ID with your full fund account number.
#   2. Paste whole file into QMT model editor -> Save -> Run (LIVE mode).
#   3. Copy every line starting with [PROBE] from the log window
#      and paste it back to the chat.
# ============================================================

import sys
import os

ACCOUNT_ID = ''      # <-- REPLACE with your full fund account
CODE = '301717.SZ'


def init(ContextInfo):
    ContextInfo.set_universe([CODE])
    print('[PROBE] === section 1: environment ===')
    print('[PROBE] python:', sys.version.replace('\n', ' '))
    try:
        print('[PROBE] stockcode:', ContextInfo.stockcode)
        print('[PROBE] period:', ContextInfo.period)
        print('[PROBE] capital:', ContextInfo.capital)
        print('[PROBE] do_back_test:', ContextInfo.do_back_test)
        print('[PROBE] barpos:', ContextInfo.barpos)
    except Exception as e:
        print('[PROBE] ctx attrs FAIL:', repr(e))
    _probe_file_write()
    _probe_passorder()
    _probe_account()


def _probe_file_write():
    print('[PROBE] === section 2: file write boundary ===')
    print('[PROBE] (today writes are allowed; after Nov-14 this tells us what breaks)')
    try:
        p = os.path.join(os.path.expanduser('~'), 'qmt_probe_write_test.txt')
        f = open(p, 'w')
        f.write('ok')
        f.close()
        print('[PROBE] file write OK ->', p)
        os.remove(p)
        print('[PROBE] file delete OK')
    except Exception as e:
        print('[PROBE] file write FAIL:', repr(e))


def _probe_passorder():
    print('[PROBE] === section 3: order function presence (NOT called) ===')
    try:
        print('[PROBE] passorder exists:', callable(passorder))
    except NameError:
        print('[PROBE] passorder NOT FOUND in this scope')


def _probe_account():
    print('[PROBE] === section 4: account read-only ===')
    for dt in ('ACCOUNT', 'POSITION'):
        try:
            rows = get_trade_detail_data(ACCOUNT_ID, 'stock', dt)
            print('[PROBE]', dt, 'rows:', len(rows))
            if rows:
                r = rows[0]
                for attr in dir(r):
                    if attr.startswith('_'):
                        continue
                    try:
                        v = getattr(r, attr)
                    except Exception:
                        continue
                    if isinstance(v, (int, float, str)):
                        print('[PROBE]  %s.%s = %s' % (dt, attr, v))
        except Exception as e:
            print('[PROBE]', dt, 'FAIL:', repr(e))


def handlebar(ContextInfo):
    if not ContextInfo.is_last_bar():
        return
    print('[PROBE] === section 5: market data depth ===')
    for period, kw in (('1m', {'count': 10000}),
                       ('1m_deep', {'start_time': '20250101', 'end_time': '', 'count': -1}),
                       ('1d', {'count': 10000})):
        try:
            args = dict(kw)
            args['period'] = period.replace('_deep', '')
            res = ContextInfo.get_market_data_ex(['close'], [CODE], **args)
            df = res.get(CODE) if isinstance(res, dict) else res
            if df is None or len(df) == 0:
                print('[PROBE]', period, 'EMPTY')
                continue
            print('[PROBE]', period, 'rows:', len(df),
                  'first:', str(df.index[0]),
                  'last:', str(df.index[-1]),
                  'last close:', float(df['close'].iloc[-1]))
        except Exception as e:
            print('[PROBE]', period, 'FAIL:', repr(e))
    print('[PROBE] === done ===')
