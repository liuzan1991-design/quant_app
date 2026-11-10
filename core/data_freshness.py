"""行情新鲜度判定（H78，2026-11-10 立号）。

设计口径（用户 2026-11-10 审阅后收紧，四点是硬要求）：

1. **不看总根数，看最后一根已完成 bar 是否落后应有水位。**
   总根数只用于**单独记录历史缺口**（`gap_bars` = 最后一根之前缺的根数；
   `missing_bars` = 应有 − 实有，含尾部滞后）；数据新鲜与否由
   `lag_bars = 应有水位 - 实际最后一根` 决定。三者混淆会产生 11-10 那种
   "总根数看着有、最后一根停在 09:54" 的假正常。
2. **午休 / 节假日 / 停牌分别处理，未知是否停牌不得算正常。**
   - 午休：水位冻结在 11:30（120 根），不产生滞后；
   - 节假日：整日不判 stale，`session="non_trading_day"`；
   - 停牌：只有**显式确认**才写 `suspension="confirmed"`；拿不到状态一律
     `unknown`，此时仍判 stale，只是文案改成"是否停牌未知，需人工确认"。
3. **阈值以 bar 为单位，不用秒，且必须从实际滞后分布取。**
   `STALE_LAG_BARS = 10`：2026-11-10 14:05–14:22 盘中实测 **30 个采样点**
   （AD 源，20s 一次）：`p50=2、p90=7、max=8`，分布
   `{1:10, 2:12, 3:2, 7:3, 8:3}` ⇒ 行情是**批量补推**，不是逐分钟平滑到达。
   取 10 = 实测最大 + 2 根余量，避免"每天下午都标红"的狼来了；
   11-10 断数事件滞后 226 根，任何 ≥10 的阈值都能在 10 分钟内发现
   （当日人工恢复是 13:30，提前 3 小时以上）。
   ⚠️ 该阈值须用 heartbeat 里的 `lag_bars` 长期分布复核，样本不足时不得再收紧。
4. **本模块只做判定，不写文件、不发告警、不重启。** IO 只有 `scan_day_bars`
   这一处（读 CSV 尾部），供 live 进程每轮调用。

bar 标签约定（与本项目数据一致，勿改）：
- 上午 09:30..11:29 共 120 根，**无 11:30 标签**；
- 下午 13:00..14:59 共 120 根，**无 15:00 标签**；
- 标签 09:30 表示 09:30:00–09:30:59 这根，**09:31 才算完成**。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

# ---- 交易时段（北京时间，分钟数）----
OPEN_MIN = 9 * 60 + 30          # 09:30
LUNCH_START_MIN = 11 * 60 + 30  # 11:30
LUNCH_END_MIN = 13 * 60         # 13:00
CLOSE_MIN = 15 * 60             # 15:00

MORNING_BARS = 120
TOTAL_BARS = 240                # 与 core.data_quality.EXPECTED_1M_BARS 同源

STALE_LAG_BARS = 10             # 落后 ≥10 根（10 分钟）判 stale，标定见模块 docstring

SESSION_PRE_OPEN = "pre_open"
SESSION_IN_SESSION = "in_session"
SESSION_LUNCH = "lunch"
SESSION_POST_CLOSE = "post_close"
SESSION_NON_TRADING = "non_trading_day"


def _minute_of_day(ts: datetime) -> int:
    return ts.hour * 60 + ts.minute


def is_trading_day(d: datetime) -> bool:
    """复用交易日历唯一入口；日历不可用时**按交易日处理**（宁可多说不能漏说）。"""
    try:
        from core.trading_calendar import is_trading_day as _isd
        return bool(_isd(d.date()))
    except Exception:  # noqa: BLE001 —— 日历坏了不该把 stale 判成正常
        return d.weekday() < 5


def session_of(now: datetime) -> str:
    """当前处于哪个时段。非交易日优先，避免节假日走"收盘后"逻辑被当成正常。"""
    if not is_trading_day(now):
        return SESSION_NON_TRADING
    m = _minute_of_day(now)
    if m < OPEN_MIN:
        return SESSION_PRE_OPEN
    if m < LUNCH_START_MIN:
        return SESSION_IN_SESSION
    if m < LUNCH_END_MIN:
        return SESSION_LUNCH
    if m < CLOSE_MIN:
        return SESSION_IN_SESSION
    return SESSION_POST_CLOSE


def expected_bars(now: datetime) -> int:
    """`now` 时刻**应已完成**的 1m bar 根数（0..240）。

    09:31 → 1（09:30 那根已完成）；11:30 → 120；午休恒 120；
    13:01 → 121；15:00 → 240。
    """
    sess = session_of(now)
    if sess in (SESSION_PRE_OPEN, SESSION_NON_TRADING):
        return 0
    if sess == SESSION_LUNCH:
        return MORNING_BARS
    if sess == SESSION_POST_CLOSE:
        return TOTAL_BARS
    m = _minute_of_day(now)
    if m < LUNCH_START_MIN:
        return max(0, min(MORNING_BARS, m - OPEN_MIN))
    return max(MORNING_BARS, min(TOTAL_BARS, MORNING_BARS + (m - LUNCH_END_MIN)))


def bar_index(ts: datetime) -> int | None:
    """bar 标签 → 当日序号 0..239；不在交易时段的标签返回 None。"""
    m = _minute_of_day(ts)
    if OPEN_MIN <= m < LUNCH_START_MIN:
        return m - OPEN_MIN
    if LUNCH_END_MIN <= m < CLOSE_MIN:
        return MORNING_BARS + (m - LUNCH_END_MIN)
    return None


def assess(now: datetime, last_bar_at: datetime | None, actual_bars: int | None,
           *, suspension: str = "unknown", trading_day: bool | None = None) -> dict:
    """新鲜度判定（纯函数）。

    Args:
        now: 北京时间当前时刻（naive）
        last_bar_at: 当日最后一根 bar 的**标签**时间；None = 没有任何当日 bar
        actual_bars: 当日 CSV 实际根数（用于历史缺口）；None = 未知（不是 0）
        suspension: "none" | "confirmed" | "unknown"（默认 unknown：不擅自判正常）
        trading_day: 覆盖交易日判定（测试用）；None = 查日历
    """
    if trading_day is False:
        sess = SESSION_NON_TRADING
    else:
        sess = session_of(now)
    exp = expected_bars(now) if sess != SESSION_NON_TRADING else 0

    last_idx = None
    if last_bar_at is not None and last_bar_at.date() == now.date():
        last_idx = bar_index(last_bar_at)

    if sess in (SESSION_PRE_OPEN, SESSION_NON_TRADING):
        lag = 0
    elif last_idx is None:
        # 该有数据却一根都没有：按"整段落后"处理，不当作 0 根的正常
        lag = exp
    else:
        lag = max(0, exp - 1 - last_idx)

    gap = None if actual_bars is None else max(0, exp - int(actual_bars))
    # 历史缺口只算"最后一根之前的缺"，不能把尾部滞后重复记成缺口
    covered = (last_idx + 1) if last_idx is not None else 0
    hist_gap = None if actual_bars is None else max(0, covered - int(actual_bars))
    stale = sess in (SESSION_IN_SESSION, SESSION_LUNCH, SESSION_POST_CLOSE) and lag >= STALE_LAG_BARS

    if stale:
        if suspension == "confirmed":
            reason = f"停牌（当日无成交），行情滞后 {lag} 根"
        elif suspension == "unknown":
            reason = f"行情滞后 {lag} 根（是否停牌未知，按断数处理，需人工确认）"
        else:
            reason = f"行情滞后 {lag} 根"
    elif sess == SESSION_LUNCH:
        reason = "午休，水位冻结"
    elif sess == SESSION_NON_TRADING:
        reason = "非交易日"
    elif sess == SESSION_PRE_OPEN:
        reason = "未开盘"
    else:
        reason = "正常"

    return {
        "as_of": now.isoformat(timespec="seconds"),
        "session": sess,
        "expected_bars": exp,
        "actual_bars": actual_bars,
        "last_bar_at": (last_bar_at.isoformat(timespec="seconds")
                        if last_bar_at is not None else None),
        "last_bar_index": last_idx,
        "lag_bars": lag,
        "gap_bars": hist_gap,          # 历史缺口（最后一根之前缺的根数）
        "missing_bars": gap,           # 总缺口 = 应有 - 实有（含尾部滞后）
        "suspension": suspension,
        "stale": bool(stale),
        # 水位落后秒数（按 bar 折算，午休与收盘后不计；不是墙钟差）
        "data_stale_seconds": int(lag * 60),
        "reason": reason,
    }


def scan_day_bars(csv_path: Path, day: str) -> tuple[datetime | None, int]:
    """读当日 CSV，返回 (最后一根 bar 标签, 当日根数)。

    单次顺序扫描（~13MB / 数十毫秒），比 pandas 轻，且**不会**把缺失读成 0：
    文件不存在 ⇒ (None, 0)；有表头无数据 ⇒ (None, 0)。
    """
    p = Path(csv_path)
    last: datetime | None = None
    n = 0
    if not p.exists():
        return None, 0
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            f.readline()  # 表头
            for line in f:
                if not line.startswith(day):
                    continue
                head = line.split(",", 1)[0].strip()
                try:
                    ts = datetime.fromisoformat(head)
                except ValueError:
                    continue
                n += 1
                if last is None or ts > last:
                    last = ts
    except OSError:
        return None, 0
    return last, n


def worst_of(items: list[dict]) -> dict | None:
    """多标的汇总：取**最落后**的那一个（任一标的陈旧即整体陈旧）。"""
    items = [x for x in items if x]
    if not items:
        return None
    return max(items, key=lambda x: (x.get("lag_bars") or 0, x.get("data_stale_seconds") or 0))
