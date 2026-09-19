# -*- coding: utf-8 -*-
"""登录冲突清单扫描器（H19 配套工具，可重跑）。

解决什么问题
------------
`test_fetch.py` 文件头写明了红线：`ad.login()` 占用 AmazingData **单点登录**，
与观察期 `live_run.py` 采集互斥，会踢掉当天采集。
项目据此维护了一份"排除名单" —— 但**那份名单是按"文件名"列的**，
而 2026-09-19 实测发现真实触发路径是**"策略内部依赖"**：

    test_ui_quick.py（文件名毫无提示）
      → 切策略到 sentiment_t
      → strategies/sentiment_t.py:50 load_index_min
      → core/sentiment_data.py:55 fetch_kline
      → core/data_fetch.py:85 _login()   ← 真实 ad.login

⇒ 所以本工具**按运行时的实际调用**判定，不按文件名猜。

怎么判定
--------
把 `core.data_fetch._login` 与 `AmazingData.login` 换成**计数器**（不触网、
不真登录），逐个跑候选测试文件，统计各自的命中次数。
命中 > 0 ⇒ **该文件属于"登录冲突清单"**。

为什么必须用子进程
------------------
同一进程里连续跑多个测试文件会互相污染（模块级状态、`_AD_CACHE`、
streamlit session）。每个候选**单独起一个子进程**，结果才干净。

用法
----
    D:\\Anaconda3\\python.exe scripts\\tool_login_conflict_scan.py

输出：控制台表格 + `test_outputs/login_conflict_scan.json`（供留档/对比）。

⚠️ 本工具**只扫描、不修改任何文件**，且**全程不联网**。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
OUT_DIR = APP / "test_outputs"

# 候选 = 全部会被 pytest 收集的文件（含 tests/ 子目录）+ 被 smoke runner
# 以 subprocess 拉起的脚本式测试。用 `--collect-only` 的权威清单来生成更严谨，
# 但这里显式列出以保证"脚本式测试"也被覆盖。
COLLECTED = [
    "test_app.py", "test_batch_ui.py", "test_grid_trade_no_phantom_fills.py",
    "test_h9_p0_gaps.py", "test_ma_swing.py", "test_ma_swing_finalize_idempotent.py",
    "test_ma_swing_lifecycle.py", "test_ma_swing_live.py",
    "test_paper_execution_semantics.py", "test_paper_replay.py",
    "test_paper_safety.py", "test_risk_rejection_observability.py",
    "test_server.py", "test_ui_quick.py", "tests/test_scripts_smoke.py",
]

# 被 tests/test_scripts_smoke.py 以 subprocess 拉起的脚本式测试
SMOKE_SCRIPTS = [
    "test_grid_dual_gate.py", "test_intraday_t_dual_gate.py",
    "test_ma_swing_dual_gate.py", "test_sentiment_t_dual_gate.py",
    "test_ma_swing_consistency_gate.py",
    "test_grid_shared_consistency.py", "test_intraday_t_shared_consistency.py",
    "test_sentiment_t_shared_consistency.py",
]

# 明确"手工运行、不进 pytest"的联网脚本（预期命中，用于自证工具有效）
MANUAL_ONLY = ["test_fetch.py"]

_DRIVER = r'''
import json, sys
from pathlib import Path
APP = Path(r"{app}")
sys.path.insert(0, str(APP))
import core.data_fetch as df

HITS = []

def spy(name):
    def f(*a, **k):
        HITS.append(name)
        raise RuntimeError("SCAN: blocked real login")
    return f

df._login = spy("core.data_fetch._login")
try:
    import AmazingData as ad
    ad.login = spy("AmazingData.login")
except Exception:
    pass

mode = {mode!r}
if mode == "pytest":
    import pytest
    rc = pytest.main(["-q", "--no-header", "-p", "no:cacheprovider",
                      "-x", "--co" if False else "-q", r"{target}"])
else:
    import runpy
    try:
        runpy.run_path(str(APP / r"{target}"), run_name="__main__")
        rc = 0
    except SystemExit as e:
        rc = int(e.code or 0)
    except BaseException:
        rc = 1

Path(r"{out}").write_text(json.dumps({{"hits": HITS, "rc": rc}}), encoding="utf-8")
'''


def scan(target: str, mode: str) -> dict:
    out = OUT_DIR / f"_scan_{target.replace('/', '_')}.json"
    if out.exists():
        out.unlink()
    code = _DRIVER.format(app=str(APP), target=target, mode=mode, out=str(out))
    env = {
        "QUANT_OFFLINE_GUARD": "1",
        "PYTHONPATH": str(APP / "test_support"),
    }
    import os
    full_env = dict(os.environ)
    full_env.update(env)
    try:
        subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(APP), timeout=1800, env=full_env)
    except subprocess.TimeoutExpired:
        return {"hits": [], "rc": None, "note": "timeout"}
    if not out.exists():
        return {"hits": [], "rc": None, "note": "no output (crashed?)"}
    data = json.loads(out.read_text(encoding="utf-8"))
    out.unlink()
    return data


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 78)
    print("登录冲突扫描（H19）：命中 > 0 的文件 = 属于「登录冲突清单」")
    print("=" * 78)
    print("判定方式：把 _login / AmazingData.login 换成计数器，实跑、统计命中。")
    print("全程不联网；只读，不改任何文件。")
    print()

    results = {}
    groups = [
        ("会被 pytest 收集的测试文件", COLLECTED, "pytest"),
        ("被 smoke runner 以 subprocess 拉起的脚本", SMOKE_SCRIPTS, "script"),
        ("项目自述「只手工跑」的联网脚本（预期命中，用于自证工具有效）",
         MANUAL_ONLY, "script"),
    ]

    for title, targets, mode in groups:
        print("-" * 78)
        print(f"【{title}】")
        print("-" * 78)
        for t in targets:
            if not (APP / t).exists():
                print(f"  {t:<52} (不存在，跳过)")
                continue
            r = scan(t, mode)
            n = len(r.get("hits", []))
            flag = "⚠️ 会登录" if n else "✅ 不登录"
            note = f"  [{r['note']}]" if r.get("note") else ""
            print(f"  {t:<52} 命中 {n:>3} 次  {flag}{note}")
            results[t] = {"hits": n, "detail": r.get("hits", []), "rc": r.get("rc")}
        print()

    conflicted = sorted(k for k, v in results.items() if v["hits"] > 0)
    print("=" * 78)
    print(f"结论：{len(conflicted)} 个文件属于「登录冲突清单」")
    print("=" * 78)
    for k in conflicted:
        print(f"  - {k}  (命中 {results[k]['hits']} 次)")
    print()
    print("⇒ 这些文件若被真实执行（不装离线守卫），会占用 AmazingData 单点登录，")
    print("  与观察期 live_run.py 采集互斥。当前由 tests_offline_guard.py 拦下。")
    print()
    print("⚠️ 本清单必须按「运行时依赖」维护，不得只按文件名 —— H19 的教训正是")
    print("   test_ui_quick.py 文件名毫无提示，却经由 sentiment_t 间接登录。")

    report = OUT_DIR / "login_conflict_scan.json"
    report.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                      encoding="utf-8")
    print(f"\n留档：{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
