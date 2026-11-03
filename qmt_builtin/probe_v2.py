# ============================================================
# QMT builtin probe v2  (2026-11-03)
# READ-ONLY: no orders, no writes outside its own result file.
# Output goes to BOTH the log window AND a text file, so the
# result can be read without copying from the GUI.
#
# WHY v2 (v1 failed to run):
#   * account id was hardcoded and truncated -> account section empty
#   * is_last_bar() gate can silently skip handlebar in LIVE mode
#   * no on-disk result, so nothing to collect
#
# HOW TO RUN: see chat message. Short version:
#   QMT -> python strategy panel -> open this file -> set period 1m
#   -> choice BACKTEST (works after close) or LIVE (only during session)
#   -> run -> then tell me; I read the txt file directly.
# ============================================================

import sys
import os

# Leave empty to auto-detect. Fill only if auto-detect finds nothing.
MANUAL_ACCOUNT = ''

CODE = '301717.SZ'

# ASCII-only paths on purpose: QMT's Python 3.6 on a CN locale can
# mangle non-ASCII file paths.
OUT_PATHS = [
    r'C:\Users\27329\qmt_probe_v2.txt',
    r'D:\qmt_probe_v2.txt',
    'qmt_probe_v2.txt',
]

# Project file that must be readable if builtin strategy is to reuse
# our own minute data. Path contains CJK, so it is written with \u
# escapes: source stays pure ASCII (python 3.6 on a CN locale re-opens
# the file as GBK when it sees non-ASCII, which is what broke v1).
PROJECT_CSV = ('D:\\Documents\\ChatGPT\\daily work\\'
               '\u65e5\u5185\u505aT\u7b56\u7565'
               '\\quant_app\\data\\live\\stock_301717_1m.csv')
PROJECT_LIVE_DIR = ('D:\\Documents\\ChatGPT\\daily work\\'
                    '\u65e5\u5185\u505aT\u7b56\u7565'
                    '\\quant_app\\data\\live')

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
        _w('%-26s %s' % (tag, fn()))
    except Exception as e:
        _w('%-26s FAIL %s: %s' % (tag, type(e).__name__, e))


def init(ContextInfo):
    _w('qmt probe v2 start')
    _sec('1 environment')
    _try('python', lambda: sys.version.replace('\n', ' '))
    _try('executable', lambda: sys.executable)
    _try('cwd', lambda: os.getcwd())
    _try('fsencoding', lambda: sys.getfilesystemencoding())
    _try('result file', lambda: _OUT_PATH[0] or 'NONE (file write blocked)')
    _try('stockcode', lambda: ContextInfo.stockcode)
    _try('period', lambda: ContextInfo.period)
    _try('capital', lambda: ContextInfo.capital)
    _try('do_back_test', lambda: ContextInfo.do_back_test)
    _try('barpos', lambda: ContextInfo.barpos)

    _sec('2 account id discovery')
    _discover_account(ContextInfo)

    _sec('3 order api presence (not called)')
    _try('passorder callable', lambda: callable(passorder))
    for fn_name in ('order_shares', 'order_value', 'get_trade_detail_data',
                    'get_market_data_ex', 'get_market_data',
                    'timetag_to_datetime', 'is_trading_day'):
        _try('global ' + fn_name,
             lambda n=fn_name: ('present' if n in globals() else 'MISSING'))
    _try('ContextInfo.set_universe',
         lambda: 'present' if hasattr(ContextInfo, 'set_universe') else 'MISSING')

    _sec('4 file read/write boundary')
    _try('write own result', lambda: 'OK' if _OUT_PATH[0] else 'BLOCKED')
    _try('read project csv', lambda: _read_head(PROJECT_CSV))
    _try('list project data dir', lambda: _list_dir(PROJECT_LIVE_DIR))

    _w('')
    _w('init done. handlebar section follows on first bar.')
    _flush()


def _read_head(path):
    f = open(path, 'r')
    try:
        lines = [f.readline() for _ in range(2)]
    finally:
        f.close()
    return 'OK size=%d head0=%s' % (os.path.getsize(path), (lines[0] or '').strip()[:60])


def _list_dir(path):
    names = os.listdir(path)
    return 'OK n=%d sample=%s' % (len(names), ','.join(names[:5]))


def _discover_account(ContextInfo):
    # 1) every ContextInfo attribute whose name looks like an account id
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
            _w('ctx attr %-12s = %s (len=%d)' % (a, v, len(v)))
    else:
        _w('no account-like attribute found on ContextInfo')

    ids = []
    if MANUAL_ACCOUNT:
        ids.append(('MANUAL', MANUAL_ACCOUNT))
    ids += cands
    if not ids:
        _w('no candidate account id at all')
        return

    for tag, aid in ids:
        for dtype in ('ACCOUNT', 'POSITION'):
            try:
                rows = get_trade_detail_data(aid, 'stock', dtype)
                n = 0 if rows is None else len(rows)
                _w('%-10s %-8s id=%-14s rows=%d' % (tag, dtype, aid, n))
                if rows:
                    _dump_fields(dtype, rows[0])
            except Exception as e:
                _w('%-10s %-8s id=%-14s FAIL %s: %s'
                   % (tag, dtype, aid, type(e).__name__, e))


def _dump_fields(dtype, obj):
    for attr in dir(obj):
        if attr.startswith('_'):
            continue
        try:
            v = getattr(obj, attr)
        except Exception:
            continue
        if isinstance(v, (int, float, str)):
            _w('    %s.%s = %s' % (dtype, attr, v))


def handlebar(ContextInfo):
    # v1 gated this on is_last_bar(), which can stay False in LIVE mode
    # and silently prints nothing. Run exactly once, unconditionally.
    if _BAR_DONE[0]:
        return
    _BAR_DONE[0] = True
    try:
        _w('handlebar fired, barpos=%s' % ContextInfo.barpos)
    except Exception:
        _w('handlebar fired')
    _sec('5 market data depth')
    for tag, p, kw in (
        ('1m count=240', '1m', {'count': 240}),
        ('1m count=1200', '1m', {'count': 1200}),
        ('1m count=10000', '1m', {'count': 10000}),
        ('1m by date', '1m', {'start_time': '20250101', 'end_time': '', 'count': -1}),
        ('1d count=10000', '1d', {'count': 10000}),
    ):
        _try(tag, lambda p=p, kw=kw: _md(ContextInfo, p, kw))
    _w('')
    _w('done. result file: %s' % (_OUT_PATH[0] or 'NONE'))
    _flush()


def _md(ContextInfo, period, kw):
    args = dict(kw)
    args['period'] = period
    res = ContextInfo.get_market_data_ex(['close'], [CODE], **args)
    df = res.get(CODE) if isinstance(res, dict) else res
    if df is None or len(df) == 0:
        return 'EMPTY'
    return 'rows=%d first=%s last=%s lastclose=%s' % (
        len(df), str(df.index[0]), str(df.index[-1]), float(df['close'].iloc[-1]))
