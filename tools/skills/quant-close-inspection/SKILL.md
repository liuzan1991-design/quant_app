---
name: quant-close-inspection
description: quant_app 量化实盘「收盘七件套」巡检。当用户问「今天跑的怎么样」「跑得如何」「收盘巡检」「D几收市核验」，或需要核查观察期某一交易日的 live 运行状态（失败次数/水位/权益/E5/E1/QMT 对账/回归）时使用。覆盖 A股 300308 网格+均线双策略 live 链路，含已知口径陷阱（必须 --local-csv、时间戳列位置、pytest 收集数读法、本机-北京 5 小时时差），以及 AD 缺根时用 QMT 交叉校验判定「AD侧缺失/市场无成交」的缺口探针（⑥-b，AD 根数≠240 自动触发），和网格引擎 stopped/defense 状态判读（⑥-c，防「策略已停摆」被读成「正常运行无信号」）。
agent_created: true
---

# quant_app 收盘七件套巡检

核验任一交易日 live 运行状态，产出结构化结论。**默认只读，不改生产代码/数据**。

## 0. 环境硬约束（不摸清会白折腾）

```bash
# 🚨 严禁写死 PortableGit 版本号（2026-10-14 实测踩坑）：
#    `versions/<ver>` 是指向 MSYS 根 `/` 的**符号链接**，`versions/current` 只是**普通文本文件**（不是链接）。
#    工具链升级后旧版本目录消失 ⇒ 写死路径整段失效 ⇒ date/grep/wc/sha256sum 全部 `command not found`，
#    但**整条命令的退出码仍是 0**（管道末端命令成功）⇒ 巡检读到"空输出"却以为"零异常" = 典型假绿。
#    正解：挂 MSYS 根的 /usr/bin 与 /cmd（与当前 shell 同源，天然免疫改名与升级）。
export PATH="/usr/bin:/cmd:/c/Windows/System32:/c/Windows"
# 自检：命令不可用就大声报错，绝不静默通过
command -v date grep wc sha256sum git >/dev/null 2>&1 || echo "🚨 PATH 未生效：本次巡检结果不可信（先修 PATH 再继续）"
cd "D:/Documents/ChatGPT/daily work/日内做T策略/quant_app"
```

- **生产解释器必须 `D:/Anaconda3/python.exe`**（托管 Python 没有 pandas）。
- **时区**：本机 GMT+3，北京 GMT+8 ⇒ **本机 = 北京 − 5h**。本机 04:30 = 北京 09:30 开盘；本机 10:10 ≈ 北京 15:10 收盘。
  🚨 **别用 `TZ=Asia/Shanghai date` 换算**（2026-09-24 实测）：本机 Git Bash **无 tzdata** ⇒ 该命令**静默返回 UTC**（例：本机 07:58 时它报 `04:58`，而真实北京时间为 `12:58`），
  **偏差 8 小时且不报错** = 又一处假绿。**换算只用算术：`北京 = 本机 + 5h`**。要机器算就走 `D:/Anaconda3/python.exe -c "import datetime;print(datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))))"`。
- **调度窗口以本机时间为准**：任务 `QuantLiveRun` `StartBoundary=04:30+03:00`，周一~五。
- ⚠️ PowerShell 工具在本机可能返回「exit 0 + 空输出」= **假绿**。核数值一律走 `Anaconda python` 或 Git Bash。

## 1. 七件套执行顺序

### ① 存活 + 时间
```bash
date '+%Y-%m-%d %H:%M:%S %z'
cat live_outputs/heartbeat.json          # status=running / last_success_at / collect_errors / match_errors
```
`last_success_at` 应贴近当前本机时间（差几分钟内）⇒ 进程存活。

### ② 水位（分钟数据行数 + 末根）
```bash
wc -l < data/live/stock_300308_1m.csv
tail -1 data/live/stock_300308_1m.csv    # 末根应为 14:59（北京）
```
⚠️ 末根 14:59 = 240 根/日完整。**校准基准**：前一交易日收盘水位 + 当日 241（含 09:30 起点）≈ 当日收盘水位。

### ③ 失败明细（**必须按失败模式分开记，禁止合并笼统计数**）
```bash
L=live_outputs/logs/live_$(date +%Y%m%d).log
grep -c "采集失败" $L                      # 失败总块数（= 各模式之和，用于交叉核对）
grep -c '^TimeoutError:' $L                # ✅ 权威计数：行首即异常名
grep -c '^TypeError:' $L                   # ✅ 同上
grep -nE "采集失败|TimeoutError|TypeError|NoneType" $L
grep -c "采集完成" $L                      # 成功轮数
```
- 🚨 **计数陷阱（2026-09-24 实证）**：**别用 `grep -c '星耀登录超时'` 数失败次数** —— traceback 里
  **源码行**（`f"星耀登录超时…"`）与**异常行**都含该串，**恰好 2×**（当日真值 90，误读成 180）。
  **一律用 `^TimeoutError:` / `^TypeError:`**，并与 `采集失败` 行数交叉核对（90+7=97 ✅）。
- 判定**连击簇**：失败时间点两两间隔是否密集（典型 ~189s）。间隔数十分钟 ⇒ 单发，非簇。
- 关键健康信号：**失败前后紧邻是否仍为成功轮**（下一轮立即恢复 = 自愈）。
- 已知签名：`TypeError: 'NoneType' object is not subscriptable` 落点
  `live_run.py:135 → market_collector.py:120/98 → data_fetch.py:87 → AmazingData/query_api/base_data.py:50`（H34）。
- 🆕 **第 3 种失败形态（2026-09-28 首次实证）**：`Exception: 查询失败` @
  `data_fetch.py:90 → AmazingData/query_api/market_data.py:236 (query_kline)`
  ⇒ 与 H34 **落点不同、层次不同**（H34 是"取日历"失败，这一条是"查 K 线"失败）⇒ **不得合并进 H34 计数**。
- 🚨 **交叉核对的两个陷阱（2026-09-28 同时踩到）**：
  1. **裸 `Exception:` 会被漏计**：`^[A-Za-z]+(Error|Exception):` 这类正则**要求异常名前至少 1 个字符**，
     **匹配不到 `Exception: 查询失败`** ⇒ 造成"各模式之和 ≠ 失败总数"的**假差**（当日 18 vs 17，差 1）。
     ⇒ 校正口径：`grep -cE "^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception):|^Exception:"`，**仍须与 `采集失败` 行数相等**才算核通。
  2. **同日内可能并存多形态**：当日 = `15×TypeError` + `2×TimeoutError` + `1×Exception查询失败` = **18** ✅。
     ⇒ **必须逐块解析**（每个 `采集失败` 后跟哪一行异常），不能只跑几个 `grep -c` 就宣布"核对通过"。
- ⚠️ **分实例统计**：一份日志可能含多个实例（计划任务 + 看门狗多次拉起）。以 `== 观察期启动 ==` 切段分别计数，
  否则"失败数"会跨实例混在一起（当日：实例①–④ 全 TimeoutError、实例⑤ 全 TypeError）。
- 🆕🚨 **`TimeoutError` 必须再分两种成因**（2026-09-30 实证，**异常串完全一样、成因完全相反**）：

  | 成因 | 现场判据（一条命令即出结论） | 处理 |
  |---|---|---|
  | **链路中断** | `netstat -ano \| grep :8600` **无任何连接**；`ping`/`TCP` 测域名或 IP 均失败 | 等链路恢复（自愈） |
  | **服务端拒绝登录** | TCP 能连（`connect` 300–400 ms 稳定成功），但套接字**全是 `LAST_ACK`、无 `ESTABLISHED`** ⇒ 对端**接受连接后立即 FIN**、且不回最终 ACK | **本机无解**，只能等对端恢复；**不要反复重启进程**（重启也登不上） |

  ```bash
  netstat -ano | awk '/:8600/ {print $4}' | sort | uniq -c | sort -rn   # 状态分布
  D:/Anaconda3/python.exe -c "import socket,time;s=socket.socket();t=time.time();s.connect(('101.230.159.234',8600));print('TCP %dms'%((time.time()-t)*1000));s.close()"
  ```
  ⇒ **判据方向**：**"有 `ESTABLISHED` 但 `get_calendar()` 返 None" = H34（会话中毒）**；
  **"零 `ESTABLISHED`、`LAST_ACK` 成堆" = 服务端拒绝**；**"连 TCP 都不通" = 链路断**。
  三者**对外表现都是采集失败**，**必须先分辨再决定处置**（分辨成本 = 两条命令）。
- 🧰 **登录探针（判定"服务端全局拒绝" vs "实例特有 / 窗口太窄"）** —— 独立进程 + 90 秒窗口：
  ```python
  from core.data_fetch import load_credentials   # 取 host/port，不打印密码
  import AmazingData as ad, threading, time
  # ⚠️ 必须把 fd 1/2 重定向到【临时文件】，不要 devnull、更不要直通控制台（见下）
  # 然后：ad.login(username=..., password=..., host=..., port=...) 放子线程，join(90)
  # 成功则量耗时：>45 s ⇒ 生产窗口太窄；<45 s ⇒ 服务端曾拒绝、现已恢复
  # 结束务必 ad.logout(username=...)   ← 实测需要 username 参数，否则 TypeError
  ```
  | 探针结果 | 结论 | 处置 |
  |---|---|---|
  | 探针**成功且 <45 s** | 生产实例特有 / 已恢复 | 重启实例即可 |
  | 探针**成功但 >45 s** | **生产 45 秒窗口太窄** | 属代码问题，记录后排期 |
  | 探针**也失败/不返回** | ✅ **服务端（或账号）全局拒绝** | **本机无解**；**重启无效**，只能等服务端恢复 |
- 🚨🚨 **安全边界：不能为了看 SDK 报错就把输出抑制关掉**（2026-09-30 实证踩到边上）：
  - 生产 `core/ad_silence.py` 会抑制 SDK 输出，**后果是 SDK 自己那句 `login fail` 被吞掉**，
    日志里只剩本项目抛的 `TimeoutError: 星耀登录超时（>45秒）` ⇒ **误导排查方向（以为是网络）**。
  - **但**该文件 `:1-8` 明示 **SDK 登录会把会话 Token 与权限列表写 fd 1/2**
    ⇒ **直通控制台的探针 = 可能把 Token 打进日志 / 对话记录**。
  - ✅ **正确做法**：fd 1/2 重定向到**临时文件** ⇒ 事后 **只 `grep -i "fail\\|error"` 取非敏感行**、**绝不整段回显**，用完即删。

### ④ 权益与每日摘要

> 📌 **2026-10-20 起（H46-③）**：`daily_summary*.json` / `daily_history.jsonl` 对 `grid_trade` **新增
> `stopped` / `defense` / `grid_bought` / `base_price`** 四个状态字段 —— 读摘要时**必须一并报出**，
> 否则「策略已停止交易」与「在跑但无信号」无法区分（详见 ⑥-c）。
> ⚠️ 历史摘要（≤ 2026-10-19）**没有**这四个字段，回看旧日不适用本判据。

> 🚨🚨 **`health.json` 只有一个写入点：收盘分支（`live_run.py:184-209`），写完立刻 `break` 退出实例。**
> **2026-09-30 更正**：09-29 记的"第二个写入时机 = 实例退出时写"**把因果读反了** ——
> 不是"退出时写 health"，而是"**收盘分支写了 health，然后实例才退出**"。二者是同一个事件。
>
> 收盘分支有**两条入口，语义完全不同**：
> 1. **正常收盘**（数据到 14:59 + 连续 6 轮无新 bar）⇒ `last_collected` **有值** ✅
> 2. 🚨🚨 **提前收盘**（`latest` = **上一交易日**的 14:59、连续 6 轮不变）⇒ 写成 `date` = 当日 **但 `last_collected = {}`**，
>    并且**同时**写出**伪的** `daily_summary_<今日>.json` + **append 一行伪记录进 `daily_history.jsonl`** ⇒ **账本级污染**（= H38 形态 B，2026-09-30 首次命中）。
>
> ⇒ **判据反转（重要）**：**盘中/中断后看到 `last_collected = {}`，不是"普通状态"，而是"提前收盘已发生、账本已被写脏"的警报。**
> （09-29 我把它定为"无害噪声"，错在这里。）
> ⇒ **判"今天到底采到数没有"一律看 `heartbeat.last_success_at` + 数据文件水位**，**不看 `health.json`**。
> 🚨 **`collect_errors` 有两个版本，引用前必须先声明读的是哪个文件**（2026-09-29 实测：同一时刻 **`health`=7 vs `heartbeat`=1**）：
> `health.collect_errors` = **写入 health 那一刻**该实例的累计值（若是提前收盘，**不是全天终值**）；`heartbeat.collect_errors` = **当前实例实时值**。
> ⚠️ 该坑**已坑到人一次**（2026-09-29 复核者据 health 的 7 推断"恢复后又断续失败 7 次"，实际实例⑥ 全程仅失败 1 次）——
> **人读文件同样会中招**，所以判据要写死、不要靠记忆。
> 📌 这也**重述了 H38 休市日污染的成因**（09-30 更正）：休市日 ⇒ 开盘后无当日数据 ⇒ `latest` = **上一交易日** 14:59 ⇒
> **提前收盘分支** ⇒ 写 health（空 `last_collected`）+ **伪 `daily_summary_<休市日>.json`** + **`daily_history` 伪行**。
> （原文写的"实例退出 ⇒ 写 health"是**因果倒置**。）
> ⚠️ 且若当日**永不恢复**（**H35**：`latest` 卡在盘中时刻 < 14:59 ⇒ 收盘块整段不可达），health 会**一直停在"空"状态**、且**伪行永久留存**。

```bash
tail -2 live_outputs/300308.SZ/grid_trade/daily_history.jsonl   # 逐日累计（★ 当日行写没写看这里）
cat live_outputs/300308.SZ/grid_trade/daily_summary.json        # = 最新一份（每日覆盖）
ls  live_outputs/300308.SZ/grid_trade/daily_summary_*.json      # 按日归档的历史份
```
⚠️ **两个名字并存、别混读**：`daily_summary.json` 是**每日被覆盖的"最新份"**，`daily_summary_YYYYMMDD.json` 才是**按日归档**。
**核"某天有没有跑"要看归档份**（最新份只能证"最后跑过的那天"）。
🚨 **收益读法**：一律读 **「权益 − 初始」** 列（`equity - 1000000`）。
**当日盈亏 = 权益_t − 权益_{t−1}**。**绝不要跨日累加任何一列**（`day_pnl` 每格都是"自成立以来累计"，相加 = 重复计算）。
🚨 **E1 累计回撤读文件字段 `cumulative_drawdown`，不要按收盘价自算**（基准是**跑动峰值**，非当日高点）。

#### 🚨🚨 必做机械检查：提前收盘伪摘要（2026-09-30 首次命中，账本级污染）

**提前收盘会把上一交易日的状态盖上当日日期写进账本。** 一条命令即出结论：

```bash
S=live_outputs/300308.SZ/grid_trade
today=$(D:/Anaconda3/python.exe -c "import datetime;print(datetime.datetime.now().strftime('%Y-%m-%d'))")
# ① 归档摘要是否在收盘前就存在（本机 10:10 前存在 = 提前收盘）
ls -l --time-style=+%H:%M $S/daily_summary_$(echo $today | tr -d '-').json 2>/dev/null
# ② history 末行日期 = 今日，但值与昨日**完全相同** ⇒ 伪行
tail -2 $S/daily_history.jsonl | D:/Anaconda3/python.exe -c "
import sys,json
r=[json.loads(l) for l in sys.stdin if l.strip()]
if len(r)<2: print('  行数不足'); sys.exit()
a,b=r[-2],r[-1]
same = all(a.get(k)==b.get(k) for k in ('equity','cash','day_pnl','cumulative_drawdown'))
print('  末两行日期: %s -> %s' % (a['date'], b['date']))
print('  值完全相同 =', same, '⚠️ 伪行' if same and a['date']!=b['date'] else '✅')
"
```

| 结果 | 含义 | 处置 |
|---|---|---|
| 归档存在 + 末两行值相同 | **已提前收盘，账本被写脏** | ① 先看今日是否还能恢复（登录/链路）；② **能恢复 ⇒ 真收盘会按日期 upsert 覆盖伪行，自动修复，不要手改**；③ 不可恢复 ⇒ 走 D13 流程（留证 → 冷备比对 → 删已确认污染产物，**须用户授权**） |

📌 **upsert 是这套账本的自愈机制**：`_write_daily_summary` 按 `date` 覆盖同日期行 ⇒ 09-29 的 6 次提前收盘**被当日的真收盘覆盖掉了**，所以当时没被发现。
**⇒ 判据：提前收盘本身不可怕，可怕的是"提前收盘 + 当日永不恢复"（H35）—— 那才会让伪行永久留存。**

### ⑤ E5 验证机会（工具复算，勿人工数）
```bash
D:/Anaconda3/python.exe scripts/tool_e5_trend_audit.py
```
看两口径（原始同日 / live shift1）是否一致 + 占比 ≥80% 判定。`trend_ok` 结构性 False（MA20 < MA60）属正常形态，非 bug。

### ⑥ QMT 对账 —— 🚨 **先确认 QMT 已开** + **必须显式 `--local-csv`**
```bash
# ⓿ 前置：QMT 极简版必须在跑（它不随开机自启！重启后必查）
ps -W 2>/dev/null | grep -iE "XtMiniQmt|miniquote" || echo "❌ QMT 没开 —— 先去开『迅投QMT交易终端 国盛证券实盘版』并登录"
D:/Anaconda3/python.exe -c "import socket;s=socket.socket();s.settimeout(3);s.connect(('127.0.0.1',58610));print('✅ 58610 可连')"
# ⓿' 🔥 仅"端口可连"不够 —— 必须验真实数据 **且核末根日期**（2026-10-12 实证：端口通、读回仍 0 行）
#     🚨 2026-10-22 实证（H49）：**根数万级也可能全是旧数据** —— QMT mini 启动后**不自动补当日**，
#        实测 36,632 根、末根却是昨日 15:00，格式全对、零告警 ⇒ **只数根数会假绿**。必须看末根日期。
D:/Anaconda3/python.exe - <<'PY'
from xtquant import xtdata
from datetime import datetime, timezone, timedelta
d = xtdata.get_market_data_ex(['time'], ['300308.SZ'], period='1m', count=-1)
df = d.get('300308.SZ')
if df is None or len(df) == 0:
    print('FAIL 1m 读回 0 行 —— QMT 会话未就绪')
    raise SystemExit(1)
last = datetime.fromtimestamp(float(df['time'].iloc[-1]) / 1000, timezone(timedelta(hours=8)))
today = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
print('OK 1m %d 根，末根 %s（北京）' % (len(df), last.strftime('%Y-%m-%d %H:%M')))
print('判读：末根 %s %s 今日 %s' % (last.strftime('%Y-%m-%d'), '>=' if last.strftime('%Y-%m-%d') >= today else '<', today))
if last.strftime('%Y-%m-%d') < today:
    print('❌ 数据是「旧」不是「坏」—— 先补当日：')
    print("   xtdata.download_history_data('300308.SZ','1m','%s','%s')" % (today.replace('-', ''), today.replace('-', '')))
PY
# ① 对账（脚本内置同一硬判据，绕过上面这段也不会假绿 —— H49 起 expect_day 生效）
D:/Anaconda3/python.exe scripts/tool_qmt_ad_reconcile.py \
  --code 300308 --date 2026-09-22 \
  --local-csv "data/live/stock_300308_1m.csv"
```
🚨 **不给 `--local-csv` 会默认读 `data/stock_{code}_1m.csv`（静态池，末根常年停在旧日期）** ⇒ 要么报 `ValueError`（响亮），要么**对陈旧数据静默对账**（危险）。这是"只扫 data/ 会漏 data/live/"的复现。
🚨 **QMT 未开时的报错指纹**：`Exception: 无法连接xtquant服务，请检查QMT-投研版或QMT-极简版是否开启`
（2026-09-24 实证：笔记本重启后 QMT 不自启 ⇒ 对账整件做不了；**与 live 采集无关，采集走 AD 不走 QMT**）。
🚨 **第二指纹（更隐蔽，2026-10-12 实证，排查耗时约 1 小时）**：`xtdata` **连得上**、`get_instrument_detail('300308.SZ')` 正常返回"中际旭创"、但 `period='1m'` **读回 0 行**。
当时被误读为"QMT 本地 1m 缓存缺失 / 服务端有但通道读不回"，**两个定性都是错的** —— 登录极简版后**同一命令**读回 **4,983 → 34,945 根（145 个交易日，首 2026-03-09）** ⇒ **不是"某两天数据丢了"，是整个数据源处于降级／未就绪状态**。
👉 **遇到 1m 读回 0 行：先回 ⓿'，不要去查 AD、不要去动 `data/`。**

**📌 三层防线（2026-10-12 立，2026-10-22 修订）**：① 本 skill 的 ⓿/⓿' 前置；② 台账 §五 **§5.2**「QMT 对账前置检查」（主清单同项，防"绕过 skill 直接跑脚本"）；③ `scripts/tool_qmt_ad_reconcile.py` **已内置自检**（10-12 加 `preflight_qmt()`，未过 ⇒ exit 2 零产物；**10-22 H49 起把「末根日期 ≥ 期望交易日」并入硬判据**）。
> ⚠️ 本行原文曾写「③ 待加自检（**未做**，观察期纪律待用户授权）」—— **与事实不符**（自检 10-12 就已随 `f96706d` 上线），2026-10-22 更正。教训：**"文档说没做"和"文档说做了"一样要核**。
看：`best_shift`（应 −1，bar开始 vs bar结束）、末根收盘是否相等、量比中位（应 ~0.01）、`close` 一致率（~0.9958）。
- **根数差 1 属正常**：AD 240 根（无 15:00 标签）vs QMT 241 根（末 15:00）——AD 把收盘量并入 14:59。
- ⚠️ **QMT 索引按本机时区渲染**（本机 GMT+3 会给 `20260924043000`）⇒ **用 `epoch` 字段或 +5h 换算，别硬编码其它偏移**。

#### 🔥 ⑥-b 自动触发规则：**AD 当日根数 < 240 ⇒ 必须跑缺口探针**（2026-10-19 立，用户拍板）

```bash
D:/Anaconda3/python.exe scripts/tool_gap_probe.py                 # 默认取 CSV 末根日
D:/Anaconda3/python.exe scripts/tool_gap_probe.py --date 2026-10-12
D:/Anaconda3/python.exe scripts/tool_gap_probe.py --no-qmt        # QMT 未开时：只列缺口
```

**触发条件**：② 里核出当日 AD 根数 **≠ 240**（缺根或多余）⇒ 不得停在「源端缺」这一步，必须判定缺口性质。

**输出三分法**：

| 判定 | 含义 | 退出码 |
|---|---|---|
| **AD 侧缺失** | QMT 同分钟**有成交量** ⇒ AD 漏了，非市场无成交 | **3**（关注） |
| 市场无成交 | QMT 同分钟 `volume = 0` ⇒ 正常空窗口 | 0 |
| 两源均缺 | QMT 也没有该根 ⇒ 查停牌/半日市 | 4 |

QMT 未就绪（⓿' 不过）= **2**；本地 CSV 当日无数据 = **5**；无缺口 = **0**。

**为什么这条必须存在（立规理由，2026-10-19）**：
10-12（D19）只采到 236 根、缺 `09:39–09:42`，当时定性为「AD 源端缺、非本机丢失」就收尾了，
隐含结论是「缺口不可避免 / 市场无成交」。**实际用 QMT 逐根核对，这 4 分钟有完整真实成交**
（`1458 / 1149 / 792 / 1143` 手）⇒ 是 **AD 侧数据缺失**。
这是 §6.9 第八形态：**否定性断言「源端没有」必须声明扫描范围** —— 当时只扫了 AD，没扫 QMT。

**三条硬口径**（脚本已内置，人工复核时也别踩）：
1. `AD[t] ↔ QMT[t+1min]` —— AD 标签是 bar **开始**、QMT 是 bar **结束**。
   🚨 **列缺口数据时最容易错位一分钟**：AD 缺 `09:39` 对应的是 QMT **`09:40`**，不是 QMT `09:39`
   （QMT `09:39` 对应 AD `09:38`，那根 AD 是有的）。脚本会打印**缺口外侧各 1 根的互证**，两侧都吻合才可信。
2. QMT 索引按**本机时区**渲染 ⇒ 一律用 `time` 字段（epoch）换算北京时间，**不得硬编码 ±5h**。
3. 量：QMT = 手、AD = 股 ⇒ `AD量 ≈ QMT量 × 100`。

**处置边界**：探针只做**判定**，**不做回补**。是否把 QMT 数据补进 `data/live/` 需用户拍板 ——
混源会破坏单一源一致性与历史可追溯性（用户 2026-10-19 已拍板：**不回补**，只登记）。

#### 🔥 ⑥-c 自动触发规则：**`engine_state.json` 的 `stopped` / `defense` ⇒ 必须解释**（2026-10-20 立，H46）

```bash
D:/Anaconda3/python.exe -c "
import json;d=json.load(open('live_outputs/300308.SZ/grid_trade/engine_state.json'))
print({k:d.get(k) for k in ('stopped','defense','grid_bought','base_price','anchor_price','prev_day')})"
```

**为什么必须有这条**：`orders` / `fills` 是**累计值**，「策略已停止交易」与「在跑但没有信号」在计数上**完全同形**。
2026-10-15 网格 `hard_stop` 触发后 `stopped` 置真且**原实现无复位路径**，10-16 / 10-19 / 10-20 连续零买入，
而 ① 心跳、② 水位、④ 权益摘要**全部正常** ⇒ **三个交易日无人发现**，靠偶然读 `engine_state.json` 才暴露。

**判读口径**：

| 读取结果 | 含义 | 处置 |
|---|---|---|
| `stopped=false` | 正常，网格可交易 | — |
| `stopped=true` | **当日**被硬止损线或账户亏损线锁住 | 必须写出：触发日 / 触发价 vs 止损线 / 属 `hard_stop`（价）还是 `loss_stop`（账户亏损） |
| `defense=true` | 趋势防御生效（前收 < MA60 **且** MA60 五日斜率向下） | **与 `stopped` 分开报**，两者都能单独阻止买入 |
| `grid_bought` 长期 0 | 从未买入过（卖出分支要求 >0） | 结合 `defense` 判「没触发」还是「被锁」 |

⚠️ **归因纪律**：买入门是 `not stopped and not defense` ⇒ **两把锁必须分别核**，不得把零交易笼统归给 `stopped`。
（2026-10-20 首次判读就犯过此错：实际 09-09~10-20 **24/24 个交易日 `defense=true`**，`stopped` 只是第二道锁。）

**修复后语义（H46-① @ 2026-10-20）**：`stopped` 改为**只锁当日**，每交易日盘初复位重判，同日仍触线立即重新置真。
⇒ `stopped=true` 现在表示「**今天**被锁」，**不再等于「永久停用」**；连续多日 `true` 说明价格持续低于止损线，属正常。

**H46-③（同日）**：`daily_summary*.json` / `daily_history.jsonl` **已增 `stopped` / `defense` / `grid_bought` / `base_price` 四字段**
（仅 grid_trade），④ 处可直接从摘要读到，不必每次手翻 `engine_state.json`。


### ⑦ 回归（日常档）
```bash
D:/Anaconda3/python.exe -m pytest -p no:cacheprovider -m "not slow and not external" > /tmp/daily.txt 2>&1
tail -3 /tmp/daily.txt
```
- **权威三档：全量 119 / 含慢 116 / 日常 104**（2026-10-21 晚实测复核；此前记的 82/79/70、111/108/99、111/108/96 均已过时）。日常档**必须带 `-m "not slow and not external"`**。仅慢档 **15** 例。
  - 2026-10-21 变更一：`test_ma_swing.py::test_ma_swing_smoke`、`test_ma_swing_live.py::{test_future_mutation_does_not_change_past_signals, test_no_duplicate_signal_per_day_and_original_run_unchanged}` 三例标 `@pytest.mark.slow`（全历史逐 bar 回放，单跑合计约 184–309s）⇒ 日常档 **99 → 96**、耗时 **534s → 7.48s**；全量与含慢档**不变**。
  - 2026-10-21 变更二：新增 `test_skill_sync.py`（8 例，`skill_sync.py` 漂移检测的防线）⇒ 三档 **各 +8**：111/108/96 → **119/116/104**。
- ⚠️ **核数用 `--collect-only -q` 会得到"分文件计数"而非总数**，需对 `: N` 求和；直接跑则以末行 `N passed` 为准。
- ⚠️ 裸 pytest 经 shell 重定向可能**截断**（退出码假 0）⇒ 落盘后再 `tail`。
- 🔁 **顺手跑一次 skill 同步检查**（只读，2 秒）：
  ```bash
  D:/Anaconda3/python.exe scripts/skill_sync.py; echo "exit=$?"
  ```
  **`exit=1` = 仓库副本与 C 盘部署副本漂移**（改 skill 忘了 `--push`，或有人直接改了 C 盘）
  ⇒ 先看它列出的「仅源侧 / 仅部署侧 / 内容不同」，再决定 `--push` 还是 `--pull`。
  **`exit=0` 才继续看回归结果** —— 否则你照着跑的可能是旧版规程。


## 2. 台账落账（仅在确有状态变化时）

改动点通常 3 处：策略 A 表行、策略 B 表行、小计行（天数 + 累计 + 一致性 n/n）。
- 表列名是「权益−初始」**不是"日盈亏"**（H10）。
- 🚨 **改表格行时绝不能往行内插换行** —— 会把表格行劈开，且"单字符行连续段"检查**查不到**。
  提交前**必须**跑（应为 0 行）：
  ```bash
  awk '/^\|/{ if (substr($0,length($0),1) != "|") print FILENAME": 行 "NR }' 观察期台账.md 量化研究规范_纠偏记录.md
  ```
  ⚠️ 台账 §三「09-18 `.git` 那格」是**先天断行**×1（该格横跨数行、行首不以 `|` 结尾），属已知例外、勿擅自改。
  🚨 **核对时用内容锚 `grep -n '^| \*\*09-18\*\*'`，不要用写死的行号**——行号会随上文增删漂移
  （2026-09-24 实测：在 §2.1 加 2 行后由 **L112 → L114**）。判定阈值仍是「断行总数 = 1」。
  **✅ 用户已裁定（2026-09-23）：保留原样，不动** —— 属**历史事故现场记录**，
  与「已发布数字不改、只标注」同口径（**给历史做格式美容 = 篡改痕迹**）。
- 🚨 **第二类表格破坏：单元格内写裸 `|`**（2026-09-27 踩过：写成"末行含 `|` OK"）——
  markdown 会把它当**列分隔符**，表格当场被劈开，而**"单字符行连续段"检查同样查不到**。
  ⇒ 表意请用**转义 `\|`** 或**改文字**（如"末行状态为 OK"）。
  🚨🚨 **该坑已复发 3 次，说明"写完再校验"不够 —— 必须加前置防御**：
  | 次 | 日期 | 场景 |
  |---|---|---|
  | 1 | 2026-09-27 | 本技能文档自身（末行状态那句里用了裸竖线） |
  | 2 | 2026-09-29 | 观察期台账 §三 ⑯（Fatal Python error 与 Aborted 之间用了裸竖线） |
  | 3 | 2026-09-29 | 纠偏记录 §6.10 —— **在"记录这个坑"的句子里又用了裸竖线（元级自踩）** |
  ⇒ **前置规则（写之前执行，而非写之后检查）**：**表格单元格内一律不得出现半角竖线**；
  写含代码 / 字段名的单元格时，**先把竖线替换为全角或斜杠**。
  ⇒ **两道防线并存**：**前置**（写作时替换）+ **后置**（下方列数校验）。**只有后置 = 每次都靠返工**（本日 3 次里 2 次返工）。
  📌 **注意**：下方的列数校验脚本按 `count("|")` 计数 ⇒ **连转义写法也会被计进去**（已知误报源之一）⇒ 故此处刻意全部改用文字描述。
  提交前**必须**加跑**表格列数一致性校验**（列数 ≠ 表头 = 已破坏）：
  ```bash
  D:/Anaconda3/python.exe -c "
  for fn in ['观察期台账.md','量化研究规范_纠偏记录.md']:
      lines=open(fn,encoding='utf-8').read().split('\n'); bad=[]; hdr=None; inT=False
      for i,l in enumerate(lines,1):
          if l.startswith('|'):
              n=l.count('|')
              if not inT: hdr,inT=n,True
              elif n!=hdr: bad.append((i,n,hdr))
          else: inT=False
      print(fn, bad or 'OK')"
  ```
  ⚠️ **两处已知误报**：单元格内的**转义管道符 `\|`** 会被计入；**连续空行**会触发"≤1 字符段"检查。
  ⇒ 判据是**与基线逐项比对**：`git show HEAD:<file>` 跑同一脚本，**行号偏移 = 本次新增行数、异常集合相同 ⇒ 未引入新异常**（2026-09-27 实证：基线 `[(2025,7,5),(2214,5,3)]` vs 当前 `[(2040,7,5),(2229,5,3)]`，偏移 +15、集合一致 ✅）。
  ⇒ **不要"顺手修"**；将来若要转对外格式，届时统一处理（那是**发布动作**，不是改历史）。
  （判据见技能 `doc-batch-edit-safety`：**历史记录 → 只标注；索引/变更日志 → 必须修**。）
- 🆕 **另三项必跑检查（2026-09-30 起，三项均被真实踩坑验证）**：
  1. **`## ` 级标题计数必须与基线一致** —— 在 §三 这类子章节内部误用 `##` 会**破坏文档结构**（本轮实测踩中，当场改回"加粗段首"）。
  2. **`≤1 字符连续段`（≥3 行）必须与基线一致** —— **插入式编辑最容易在标题前后留下 3 个连续空行**（本轮实测踩中）。
  3. 🆕 **跨源比较前必须归一化换行符**（2026-09-30 实测踩中）——
     `git show HEAD:<file>` 输出的是**原始 blob（LF）**，而 `core.autocrlf=true` 下**工作区文件是 CRLF**
     ⇒ 两边直接 `split()` 做差集**会全盘失效**（实测误报「新增 1842 行 / 缺失 1 行」，
     那个"缺失 1"其实是**整个 HEAD 文件**当作一行）。
     ⇒ 正确做法：**先 `.replace('\r\n','\n')` 归一化，再比较**。判据：归一化后 `新增 = 本次实际新增行数`、**`缺失 = 0`**。
- 🔧 **验"零丢失"的可靠方法（2026-09-30 新增，段落搬移/重排场景必用）**：
  搬移类改动**不要只看 `git diff` 的增删行数**（它受算法口径影响，实测同一改动 `-U0` 数出 19 行、`--stat` 数出 27 行）。
  ⇒ 用**多重集合差集**证明：`Counter(新) - Counter(旧)` = **应恰好是本次新增内容**、`Counter(旧) - Counter(新)` = **必须为 0**。
  ⇒ 再叠三条机械自证：**元素数不变 / 前 N 行未变 / 末 20 字符未变**。
- 📌 **段落顺序自查（2026-09-30 新增）**：往时间线文档里追加新段时，**确认插入点在上一段的末尾之后**，
  否则会出现"**新段标题下挂着上一段的剩余内容**"（实测：⑲「晚间收口」标题下挂着**白天**的探针段，形成倒序）。
  ⇒ 快速判据：**读一遍每个 `**⑯/⑰/⑱...` 段的头 3 行与末 3 行**，看标题描述与内容是否同时段。
  ⇒ 🚨 **插入式编辑的锚点纪律**：**用标题行当 `old_string` 时，必须把该标题一并写回 `new_string`** ——
  本轮曾因此**整行删掉 H35 标题**（当场补回，并逐段核 `H34–H38 标题各恰 1 次`）。
  ⇒ ⚠️ **核 emoji 标题不要用 shell `grep -c "^#### 🔴 H3x"`**（不可靠）⇒ **必须用 Python 读文件精确核**。
- 新发现缺陷按 **H 编号**登记进 `量化研究规范_纠偏记录.md`，并**按升级条件**判断是否升级（升级条件未达成 ⇒ 明确写"维持原级"，不要含糊）。
- 落账后提交推送：
```bash
git -c http.version=HTTP/1.1 push origin master
```
⚠️ **推送必须带 `-c http.version=HTTP/1.1`**（本机有透明代理，HTTP/2 会挂起）。失败先隔几秒重试（链路间歇抖动），别急着怀疑凭据。

🚨 **提交信息里别出现 "PowerShell" 这个词（2026-09-30 实测）**：Ranger/Bash 的安全策略会把它**误判成"从 Bash 调 PowerShell"并直接拦掉整条命令**
（报错原文：`Invoking PowerShell from Bash bypasses PowerShell security checks; use the PowerShell tool instead`）。
⇒ 写提交信息时用「本机命令行工具」「PS」等替代表述；
⇒ 长信息建议用 **heredoc（`git commit -F - <<'EOF' … EOF`）**，可避免 `-m "..."` 里引号/竖线被 shell 二次解析。
（同类：提交信息里带裸竖线本身不影响 git，只影响 Markdown —— 两件事别混。）
⚠️ **该坑同日被踩两次，第二次是「元级自踩」**：**在"记录这个坑"的那一行里又写了这个词** ⇒ 整条命令被拦。
⇒ 与"裸竖线"那个坑**同族**（**描述坑的句子本身触发该坑**）⇒ 写"教训条目"时，**先把敏感 token 替换掉再写**。

🚨 **推送耗时实测 15 s – 9 分钟（波动极大，2026-09-28 实测 9 min 8 s 写在案；勿据此判"卡死"）**，
前台调用极易被 SIGTERM 截断（输出为空 + exit 1）** ⇒ **放后台跑**（`run_in_background`），别在前台干等。
🚨 **绝不用"大概推上去了"下结论**：SIGTERM 截断 ≠ 推送失败，也 ≠ 成功。**唯一判据**是核对远端：
```bash
cat .git/refs/remotes/origin/master   # 本地只读，无需联网，推送成功才会更新
git rev-parse HEAD
```
两者相等才算推上。若不等，**后台重试**（实测第 1 次即成功，只是慢）：
```bash
for i in 1 2 3; do git -c http.version=HTTP/1.1 push origin master 2>&1; \
  [ "$(cat .git/refs/remotes/origin/master)" = "$(git rev-parse HEAD)" ] && break; sleep 10; done
```

## 2.5 已知故障指纹（撞上就按对应条目走，别当新问题查）

| 指纹 | 判定 | 处置 |
|---|---|---|
| 🆕 **全天 `RuntimeError: 星耀接口未返回数据`（今日全部标的/周期）** | 🚨 **先查"今天是不是交易日"，别当故障查**（2026-09-25 实证：中秋休市） | 三条对照就能定性：① `fetch_kline` 拉**上一交易日**必须成功（证明登录通）② 换标的（`000001`）+ 换周期（`day`）**同样为空** ⇒ 非代码/权限 ③ 网络 `socket` 直连全通 ⇒ 非网络。**三条同向 ⇒ 休市，不是 bug。** 然后按 **H38** 处置（伪记录，见下） |
| 休市日产出 `daily_summary_<当日>.json` + `daily_history` 当日条目 | **H38**（`live_run.py:77 _past_hard_close()` 与 `live_match_loop.py:385` 的 `date=datetime.now()` **只读时钟、不查交易日历**） | 非新缺陷——`观察期台账.md` §5.1 已于 2026-09-18 预判。**数据一律不删、只标注**（删除线 + 🚫），横向汇总必须排除该行。**⚠️ H35/H38 同函数反向失效，须同批修** |
| `TypeError: 'NoneType' object is not subscriptable` @ `data_fetch.py:87 → base_data.py:50` | **H34**（AD 会话故障，无重登路径） | 观察期内只登记。⚠️ **不要再写"零自愈、只能重启"** —— 2026-09-28 实测反例，见下两行 |
| 失败间隔恒 **38–40s**（每轮必败、成簇且**到窗口结束仍无一次成功**） | **H34 · 死锁形态**（D11 / 09-23） | 确认会话死锁 ⇒ **只能重启进程** |
| 失败间隔 **~188s** 成簇、**但最终自行恢复成功** | **H34 · 自愈形态**（2026-09-28 首次实证） | ⚠️ **不重启也会好** ⇒ **间隔长短不能单独作"是否死锁"的判据**，须叠加"是否持续到窗口结束仍无一次成功" |
| 全天零 `TypeError`、但有多轮采集失败 | 看**失败明细的分模式计数**（§1 ③）——可能是 `TimeoutError`（登录超时）或 🆕 `Exception: 查询失败`（`market_data.py:236`） | 分别记录、**不得合并进 H34** |
| **收盘后（本机 >10:10）python 进程仍在 + 无当日 `daily_summary`** | **H35**（硬收盘兜底不可达：`live_run.py:178` 的 `_past_hard_close()` 陷在 `latest >= 14:59` 内） | 三重后果：**摘要不写 / health 不写 / 进程不退出**。⇒ 需人工终止（`taskkill /F /PID`），否则**次日 04:30 双进程抢 AD 单点登录互踢** |
| 看门狗日志记录 `last_success` 长期冻结但**未动作** | **设计使然**（其前提"重启解决不了网络问题"对**会话死锁**不成立） | 是已知缺陷，勿误判为看门狗坏了；修在观察期后 |
| 采集长期失败但 `updated_at` 仍在刷 | 进程**活着**、只是采集坏 | **不要**据此判"进程死了" |
| 全天 `last_success_at=null` + `TimeoutError: 星耀登录超时>45s` 成串 | **网络层中断**（≠ H34：H34 是 `TypeError: NoneType`） | 网络恢复后重启进程即可；数据**可回补**，见 **§2.7** |
| `console_*.log` 出现 **`Fatal Python error: Aborted`**，栈含 `tgw/interface.py:22 Login` ← `tgw_login.py:82` | **H37**（登录卡死致原生层 abort，2026-09-24 首次实证；09-21~23 均 0 次） | 🚨 **别再说"进程静默消失、无痕迹"**——痕迹在 **`console_*.log`**，`live_*.log` 里确实没有（前者收 stdout/stderr + faulthandler，后者只收 `_LOGGER`）⇒ **查"进程为何没了"必须两个文件都看** |
| 看门狗连续多轮 `[ALERT-DEAD]` → 拉起 rc=0 → 心跳刷新 → **`collect_errors` 又从 3 重新爬** | **H36**（看门狗只判「心跳+进程」，不判「是否真采到数」） | 机制没坏，但它治不了"活着干不了活"；病根不除就空转（D12 实测 3 轮全白拉）。**修复项 = D11 已拍板的窄口径 `last_success_at` 冻结告警** |

⚠️ **终止进程前必须确认看门狗不会把它拉起**：窗口 **本机 04:35–10:30**（`watchdog.py:72-75`），**出窗后静默不动**。窗口内它也会因"有进程"而只告警不重启。
⚠️ **AD 单点登录**：任何补数/重启**必须先确认无进程持有登录**，不能并行。

### 🕐 休市日（节假日）专项 —— 2026-09-25（D13）实证

**🚨 第一铁律：`RuntimeError: 星耀接口未返回数据` ≠ 故障。先核交易日历。**

```bash
# ① 今天到底开不开市？（节假日一定先查这个，再查别的）
#    近期已知：2026 中秋 09-25~09-27 休市 / 09-28 复市；国庆 10-01~10-07 休市 / 10-08 复市
# ② 三条对照，同向 ⇒ 休市（不是 bug）
D:/Anaconda3/python.exe -c "
import sys; sys.path.insert(0,'.')
from core import data_fetch as D
for code,day,per in [('300308','<上一交易日>','min1'),('300308','<今天>','min1'),('000001','<今天>','min1')]:
    try: print('OK ',code,day,per,len(D.fetch_kline(code,day,day,period=per)))
    except Exception as e: print('FAIL',code,day,per,type(e).__name__,str(e)[:90])"
# 判据：上一交易日 OK（⇒登录通） + 今天全 FAIL + 换标的/周期也 FAIL ⇒ 休市
# ③ 失败间隔若稳定在 ~74s（不是 45s 登录超时的阻塞）⇒ AD 是"确定空返回"，不是挂死
```

**🚨 第二铁律：任务的触发日窗（周一~周五）只挡得住周末，挡不住工作日休市。**
$5.1$ 的原始推理"按设计休市日不该跑"**是错的**——09-25 是周五，任务照跑。
⇒ **国庆连休 10-01~10-07 含 7 个工作日 ⇒ 将全部空跑、反复写伪记录**（H38）。

**🚨🚨 第三铁律（最易犯错）：休市日的"污染"不在 `data/`，在 `live_outputs/` 的派生产物。**
> **2026-09-25 真实教训**：助手只核了 `data/live/*.csv`（确实未改动）就下结论"**没坏任何东西**"——
> **结论错误**。污染在**四个派生产物**上，且**每个被拉起的实例都会重写一遍**（当日 **7 次**）。
> ⇒ **核"某天有没有跑"必须同时看 `live_outputs/` 下这 4 类文件，不能只核 csv。**

**休市日会污染的四处（H38，§5.1 已预判、09-25 实证）**：

| # | 产物 | 变化 | 处置 |
|---|---|---|---|
| 1 | `daily_summary_<休市日>.json` ×N | **会生成**（内容 = 上一交易日复制） | 🚫 伪记录 |
| 2 | `daily_summary.json`（最新副本） | **被覆盖**为休市日版 | ⚠️ **下游脚本引用的就是它** ⇒ 最易取到伪值 |
| 3 | `daily_history.jsonl` | **追加**休市日条目 | 🚫 **横向汇总必须排除** |
| 4 | `health.json` | `date=休市日` 但 `last_collected={}` | ⚠️ 部分污染 |
| — | `data/live/*.csv` | **零改动** | ✅ 无需处理 |

**🔴 处置纪律（用户授权前不动手）**：
1. **先留证**：`quant_app_backups/pollution_removal_pre_<TS>/` + `sha256sum`（删除前必须做）。
2. **回滚源**：用**前一晚 22:30 冷备**（那是纯"上一交易日"状态）—— 核验方法：`daily_history` 行数、零休市日条目、无休市日摘要。
3. **清理**：删休市日摘要；`daily_history` 用冷备**恢复**（等价删末条）；`daily_summary.json` / `health.json` 冷备覆盖。
4. **验收五项**：① 休市日摘要已不存在（**日志文件保留作现场证据**）② 与冷备 **sha256 逐字节一致** ③ history 行数回到上一交易日 ④ 全部 JSON 可解析 ⑤ **数据文件指纹未变**。
5. **口径定性**：这是**删除已确认的污染产物**（休市日无真实数据 ⇒ 错误产物），**不是改写历史**——
   ⚠️ 与"已发布数字留档不改"**不冲突**：那套纪律保护的是**真实记录**，不保护**空跑产物**。

**🔴 止血：光看进程不够，必须让看门狗停手**
> 2026-09-25 实测：进程在 08:28 就自行退出了，但**看门狗窗口到 10:30、阈值 20 分钟**（心跳停 08:28 ⇒ **08:48 又会拉起**）
> ⇒ **只杀进程无效，污染会继续累积**。正确做法 = **禁用 `QuantLiveWatchdog`**（可逆，复市前恢复）。

```bash
# 禁用后必须"读文件"验证，不能信命令自述（§6.9 第十二形态）
tr -d '\000' < /c/Windows/System32/Tasks/QuantLiveWatchdog | tr '<' '\n<' | grep -iE "^Enabled>"
# 期望 Enabled>false
# 并用 watchdog.log 确认不再新增记录（最后一行的时刻应早于下一次应检时刻）
```

**休市日收盘核查清单**：

| # | 查什么 | 干净（预期） | 出现则 |
|---|---|---|---|
| 1 | 有无残留 python 进程 | 无 | **必须终止**——否则复市首日**双进程抢 AD 单点登录互踢** |
| 2 | **`QuantLiveWatchdog` 是否还在跑** | 已禁用 | 未禁 ⇒ **它会持续拉起并反复写污染** |
| 3 | `daily_summary_<休市日>.json` | 不存在 | 登记 H38；**先留证再按授权清理** |
| 4 | `daily_history.jsonl` 休市日条目 | 无 | 同上；汇总时排除 |
| 5 | `daily_summary.json` 最新副本 | 应 = 上一交易日 | 若为休市日 ⇒ 覆盖回上一交易日 |
| 6 | `health.json` | 应 = 上一交易日 | 若为休市日 ⇒ 回滚 |
| 7 | `data/live/*.csv` **sha256 + mtime** | 未改动 | 被改动 ⇒ 升级排查 |
| 8 | 备份 | 已做（污染态留证 + 冷备回源） | — |

**⚠️ 不要踩的三个坑（2026-09-25 助手自己踩过）**：
1. **`wc -l` vs 数据行**：台账水位是**数据行**（不含表头），`wc -l` 恒多 1。**别把这两个数当"差 1 = 有问题"** —— 它们是同一个值的两种写法。
2. **判"数据文件是否被动"只看 `sha256` + `mtime`，不看 `wc -l`** —— 先比对备份哈希再下结论。
3. **`grep -c '星耀接口未返回'` 会双计**（traceback 源码行 + 异常行，**恰好 2×**）：当日真值 **48**，误读成 **96**；用户用同一方法得 **208**。
   ⇒ **权威计数一律 `grep -c '^RuntimeError:'`** / `'^TimeoutError:'` / `'^TypeError:'`，再与 `采集失败` 行数交叉核对（48+3=51 ✅）。

**💡 修复方向（H38，两条并行评估）**：
- **A 日历版（建议为主）**：接入交易日历（**本地缓存**，避免 H34 式每轮联网）。
- **B AD 空返回版（建议为辅）**：连续 N 轮"接口未返回数据"且 `last_collected` 零增长 ⇒ 判休市 ⇒ 主动退出。
  D13 实证支持（失败间隔稳定 ~74s ⇒ 确定空返回，非挂死），**但单独用有误判风险**（数据源真故障也会连续空返回）
  ⇒ **必须叠加"当日 `last_collected` 从未增长" + 日历交叉校验**（双条件）。
- 🚨 **与 H35 反向兼容**：同一函数 `_past_hard_close()`。H35 = 采集断了**该收却收不了**；H38 = 休市**不该收却收了**。
  **加日历校验时不能简单 return，否则堵死 H35 通道** ⇒ 建议抽 `_should_close_now()`，两条判据**显式分离**。

**🗓️ 节假日处置口径：方案 C（用户 2026-09-25 拍板，取代"跑了再清"）**

> **原理**：休市期任务跑着 = **纯空转烧日志 + 写污染**，**禁掉是零成本止损**，比"跑了再清"干净。

| 时点 | 动作 |
|---|---|
| **假期前最后交易日收盘后**（国庆 = **09-30 周三**，已核实为交易日） | **禁用 `QuantLiveRun` + `QuantLiveWatchdog`**（两个都要禁！只禁主任务，看门狗仍会拉起）。⚠️ **等当晚 22:30 冷备跑完再禁** ⇒ 让冷备把**干净运行态**备进去，假期回滚基线才干净 |
| **复市前**（国庆 = **10-08 周四**） | **恢复两个任务 + 晨检三项（复用下表）** |

- ⚠️ **禁/启都可能被安全策略拒**（`schtasks.exe` 在本机黑名单）⇒ 备选走**任务计划程序 GUI**，**一律读 XML 验证**。
- ⚠️ **执行窗口要"可设闹钟"**：定在 **"前一交易日晚间"**，**不要写"X 月 X 日前"这种模糊区间**——
  否则拖到假期首日早上才禁，**当天 04:30 已跑过一轮**（8 实例 × 1 轮污染已发生）⇒ 止损失效。
- ✅ **不违反观察期纪律**：只动调度开关、不改代码；休市期本就不该有采样 ⇒ **不产生样本缺口**，无需按"缺失日"标注。
- ✅ **比方案 A/B 都干净**：A（每次复市后清理）= 事后补救、**每次都要人工清**；B（期末统一清）= ⚠️ **不可取**，连休期 `daily_summary.json` 被持续覆盖为休市日版，**周期内取数即取到伪值**。

**🔴 恢复日 = 高危日（用户 2026-09-25 指出，本轮新增）**

> **两任务同时被禁 N 天后恢复，任何一边忘了恢复另一边照常跑** ⇒ 两种方向都是坏的：

| 遗漏情形 | 后果 |
|---|---|
| 恢复主任务、忘看门狗 | **无兜底跑全天** |
| 恢复看门狗、忘主任务 | **空转写污染**（休市日事故复现） |

⇒ **恢复动作必须是"恢复 + 验证"两件事，不是一件**。**禁得越久（如国庆 7 天）越危险** ——
09-28 只禁 1 天（仅看门狗）主链路连续；**10-08 是两任务禁 7 天后恢复，风险更高**。

**🌅 复市首日晨检三项（2026-09-25 用户裁定：从"待办"升级为"晨检第一项"，不设自动提醒）**

> 用户裁定：**不设自动提醒** —— 靠"写进台账 + 自己的手机闹钟"，**起床后先做这三项、再干别的**。
> 📌 **本表复用于任何"中断后首个交易日"**：09-28（复市）/ 10-08（长假后复市）**同一个表**。

| # | 查什么 | 判据 |
|---|---|---|
| 1 | **两任务均已恢复** | 读任务 XML：**只有出现 `Enabled>false` 才算没恢复**；**`Enabled` 元素整个消失 = 已启用**（schema 缺省 `true`，序列化器不写）。**两个都要核**，不信命令自述。📌 **别用"必须找到 `Enabled>true`"当判据** —— 会把正常任务误判成禁用（详见下文「XML 读法陷阱」） |
| 2 | **04:30 实例已起** | 有 python 进程 / 日志有首轮记录 |
| 3 | **心跳已刷新** | 🚨 **看 `heartbeat.json` 的 `last_success_at`（本日 + 距今几分钟内）—— 不是 `health.json`。** `health.json` **只在收盘流程里写**（H35 的"三重后果"之一就是"health 不写"）⇒ **盘中它必定还是上一交易日的版本**，拿它当判据会**误判成"心跳没刷"**。📌 2026-09-28 实测：盘中 `health.json` 仍是 `date=2026-09-24`，而 `heartbeat.last_success_at` 已是当日的 |

⇒ 三项**任一不满足** ⇒ 按"当日为空跑"处置（先查是否又一个休市 / 任务没起），再决定是否手动补跑。
📌 **这三项应排在收盘七件套之前**（周一收盘巡检时由技能显式列出）。

## 2.6 次日开机预检（收盘后或晚间，验证"明天还能不能跑"）

按顺序跑，**前 4 项任一不过就别指望明天**：

```bash
cd "D:/Documents/ChatGPT/daily work/日内做T策略/quant_app"
# ① 无残留进程（有残留 = 明天双进程抢 AD 单点登录 ⇒ 互踢）
# ⚠️ 这个计数会把"我自己的 python 辅助进程"也算进去（见下方致命陷阱）⇒ 只作辅助
# ★ 判 live 进程是否存活，一律看 heartbeat.updated_at 是否停刷
ps -W 2>/dev/null | grep -i python || echo "无进程 ✅"
# ⭐ 要看会话/内存来区分真假实例：
tasklist //FI "imagename eq python.exe" //FO CSV //NH
# ② 无原子写残留（进程被 kill 时可能留半截 .tmp）
find data live_outputs -name "*.tmp" 2>/dev/null; echo "(空=✅)"
# ③ live 数据文件结构（末字符必须是 0a；末行时间应等于当日末根）
F=data/live/stock_300308_1m.csv
stat -c %s "$F"; tail -c1 "$F" | xxd -p; head -1 "$F"; tail -1 "$F"
# ④ 状态文件 JSON 可解析（被 kill 时可能写坏）
for f in live_outputs/heartbeat.json live_outputs/300308.SZ/*/paper_state.json live_outputs/300308.SZ/*/engine_state.json; do
  D:/Anaconda3/python.exe -c "import json,sys;json.load(open(sys.argv[1],encoding='utf-8'));print('OK',sys.argv[1])" "$f"; done
```

🚨🚨 **"数 python 进程"有个致命陷阱（2026-09-30 实测踩到）—— 别拿它当"进程是否存活"的主判据**：
- **陷阱**：`ps -W | grep -ci python` 和 `tasklist /FI "imagename eq python.exe"` **会把我自己刚启动的 python 辅助进程也算进去**
  ⇒ 实测同一条巡检命令里先跑了两段 `python.exe -c`，紧接着计数就从 **1 变 2**，看起来像"双实例"（**假红**）。
- **区分办法（看会话名 + 内存量）**：
  **live 实例 = `Services` / Session 0，内存数百 MB ~ 数 GB**；
  **我自己的辅助进程 = `Console` / Session 2，内存几十 MB**。
- **★ 唯一可靠的存活信号 = `heartbeat.json` 的 `updated_at` 新鲜度**（**只有 live 进程会写心跳**）⇒
  **判"live 死没死"一律看心跳是否停刷**；进程计数只作辅助，且必须按会话过滤。
- ⇒ **推论（比本条本身更重要）**：**任何"守护 / 巡检脚本"用"数自己这类进程"来判活，都会自我欺骗** ——
  因为**它自己就是用这类进程实现的**。**自指型判据必须换成正交信号。**
  （同类：看门狗用 `updated_at` 判活 → 采集失败时 `updated_at` 照刷 ⇒ 判不出"瘫"，即 H36。）

**⑤ 计划任务定义（`schtasks` 被黑名单，直接读 XML）**：

```bash
for t in QuantLiveRun QuantLiveWatchdog QuantAppColdBackup; do
  tr -d '\000' < "/c/Windows/System32/Tasks/$t" | grep -oE \
   "<(StartBoundary|Enabled|MultipleInstancesPolicy|ExecutionTimeLimit|DisallowStartIfOnBatteries|StopIfGoingOnBatteries|StartWhenAvailable|LogonType)>[^<]*"
done
tr -d '\000' < /c/Windows/System32/Tasks/QuantLiveRun | tr '<' '\n<' | grep -iE "^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
```

| 必查项 | 期望值 | 为什么 |
|---|---|---|
| 电池三项 | `DisallowStartIfOnBatteries=false` / `StopIfGoingOnBatteries=false` / `StartWhenAvailable=true` | `schtasks` 默认与 GUI 相反，笔记本上会**静默卡 Queued 且 LastTaskResult 仍显示 0** |
| `LogonType` | `S4U`（主任务）| `InteractiveToken` 在"开机但无人登录"时不运行 |
| 触发日 | `Monday`–`Friday` | 确认次日确实在触发日窗内 |
| `ExecutionTimeLimit` | ≥ 实际运行时长（约 5.7h）| 太小会在盘中把进程掐掉 |
| 🚨 `MultipleInstancesPolicy` | 记下它（本项目 = `IgnoreNew`）| **若上次实例卡在 Running，下次触发会被静默忽略**（不报错）|

### 🔌 系统电源与「唤醒能力」核验（2026-09-29 新增）—— **决定"任务到点到底能不能跑"**

🚨 **任务定义全对 ≠ 到点能跑** —— 还要过**系统电源**这一关。

| 查什么 | 本项目实测（2026-09-29） | 含义 |
|---|---|---|
| 任务 `WakeToRun` | **主任务 = `true`**；看门狗 / 冷备**未设**（= false） | 只有主任务声明"能唤醒机器" |
| `在此时间后睡眠`（AC/DC） | **0 / 0** ⇒ **永不自动睡** | ✅ 机器不会自己睡过去 |
| `在此时间后休眠`（AC/DC） | **0 / 0** ⇒ **永不自动休眠** | ✅ |
| **`允许使用唤醒定时器`（RTCWAKE）** | **AC = 0 / DC = 0** ⇒ **禁用** | 🚨 **任务声明的 `WakeToRun` 实际唤不醒机器** |
| 快速启动 `HiberbootEnabled` | **1（开启）** | ⚠️ "关机"实为混合关机 ⇒ **关机状态下定时任务无法冷启动** |
| 睡眠状态 | **S0 低电量待机（Modern Standby）**、**S3 不可用** | 合盖 / 电源键行为**不由 powercfg 暴露**（`SUB_BUTTONS` 只有"开始菜单电源按钮"）⇒ **配置侧判不了** |

```bash
# ① 任务唤醒能力
tr -d '\000' < /c/Windows/System32/Tasks/QuantLiveRun | grep -oE "<WakeToRun>[^<]*"
# ② 睡眠/休眠超时 + 唤醒定时器（0 = 永不 / 禁用）
D:/Anaconda3/python.exe -c "import subprocess;print(subprocess.run(['powercfg','/q','SCHEME_CURRENT','SUB_SLEEP'],capture_output=True).stdout.decode('gbk','replace'))"
# ③ 快速启动（1 = 开启，⚠️）
D:/Anaconda3/python.exe -c "import winreg;k=winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\Control\Session Manager\Power');print(winreg.QueryValueEx(k,'HiberbootEnabled'))"
```

🚨 **由此得到的四条判据（关键）**：
1. **`WakeToRun=true` 是"假保障"** —— 系统禁用唤醒定时器后，它**形同虚设**
   ⇒ **"任务设了唤醒" ≠ "机器能被唤醒"**（与 §6.8「代码事实 ≠ 运行事实」同族，属**第十一形态**）。
2. **唯一真保障 = "机器不睡"** ⇒ 靠的是**"睡眠/休眠超时 = 0（永不自动睡）"**，**不是** `WakeToRun`。
3. **操作纪律**：**当晚不要"关机"**（快速启动下无法冷启动）、**不要手动睡眠 / 合盖**；**保持插电**。
4. ⚠️ **配置侧判不了时改用实证**：合盖行为在本机**读不出来** ⇒ 判据换成
   "**近 N 个交易日 04:30 是否真跑起来**"（本项目 09-14~09-29 连续自动启动 ⇒ **实证通过**）。
   ⇒ **能够用"历史实际行为"替代"配置推断"时，优先用前者**。
### 🔑 任务「启用 / 禁用」状态核验 —— 三路径（2026-09-27 实证；复市恢复 / 假期禁用必用）

🚨 **任何单一来源都会假绿**（本机三重陷阱）：PowerShell 工具**无回显**（exit 0 + 空输出）、`schtasks.exe` 在**安全策略黑名单**、
`schtasks /End` 对未运行任务**也报"成功"**（no-op）。⇒ **一律三路径交叉**：

```bash
# ① 读任务 XML（最硬、无需任何工具）
tr -d '\000' < /c/Windows/System32/Tasks/QuantLiveWatchdog | tr '<' '\n<' | grep -iE "^(Enabled|StartBoundary|DaysInterval)>"
```

> 🚨 **XML 读法陷阱（2026-09-27 踩过；2026-10-21 再次复核）**：
> **禁用 = 显式写 `Enabled>false`；启用 = 该元素整个消失**（启用态 = schema 默认值 `true`，序列化器不写）。
> ⇒ **"grep 不到 `Enabled` 行"本身就是"已启用"的证据**，**不是漏读、更不是异常**。
> **别把"没看到 false"误判成"没读到 / 状态不明"** ⇒ 否则会去做无谓的重复操作。
> 📌 **2026-10-21 实测补充**：`QuantLiveRun`（`schtasks` 旧式 **Task version 1.3**）**整个 XML 里连一个 `<Enabled>` 都不存在**；
> 而 `QuantLiveWatchdog` / `QuantAppColdBackup`（**version 1.2**，GUI 建）各有 2 个 `<Enabled>true</Enabled>`。
> ⇒ **同一台机器上两种序列化形态并存**，判据必须统一为"**是否有 `false`**"，不能按"有没有 `true`"写脚本。
> 交叉验证用事件日志（EID 107 `TimeTrigger` / 200 `ActionStart` / 201 `ActionCompleted rc=0`），**比 XML 更硬**。

🚨 **判据更正（2026-09-30 实证）—— `Next Run Time` 在禁用态下【不会消失】**
- 实测：两任务禁用后，`Next Run Time` **仍显示缓存的 2026-10-01 04:30 / 04:35**（任务计划程序保留该缓存值）。
- ⇒ **禁用与否只看 XML `Enabled` + COM `Task.Enabled`**；**绝不能**把"NextRun 消失"写进验收条件
  （否则每次禁用都会被误判为"没生效"）。

🔧 **本机执行动作的三个环境事实（2026-09-30 实测）**：
- **`Disable-ScheduledTask` 在本机该 cmdlet 版本不支持 `-Confirm` 参数** ⇒ 报"找不到与参数名称 Confirm 匹配的参数"
  ⇒ **不带 `-Confirm` 直接执行**。
- **本机 PS 侧 COM 实例化（`New-Object -ComObject Schedule.Service`）被宿主安全策略拦截**
  ⇒ **COM 交叉一律走临时 `.py`（pywin32）**，别在 PS 里建 COM 对象。
- 命令返回仍是**"退出码 0 + 空输出"的假绿** ⇒ **结论一律以读 XML + COM 实测为准**。

🚨 **验收要把"动作成功"与"报告准确"分开看（2026-09-30 实证）**
- 实况：禁用动作**执行正确**（三源一致 `false`），但**自动化自己生成的报告里把 `QuantLiveWatchdog` 的触发器误标为 `Weekly`**
  ——**实测是 `Daily`**（`ScheduleByDay` + `DaysInterval=1`，与 2026-09-27 结论一致；周末闸门在**脚本层**）。
- ⇒ **执行成功 ≠ 报告正确**，而**报告里的错字段恰恰是它被派去验证的那个**。
- ⇒ 纪律：**接受任何"自动生成的报告"时，必须独立复核至少一个关键字段**（§6.9 第十二形态家族）。

```bash
# ② COM 直读：唯一能同时拿 Enabled + State + NextRun 的路径
#    ⚠️ 必须写成临时 .py 再跑，勿内联（MSYS 会吞反斜杠）
```

```python
import win32com.client
S = {0: "Unknown", 1: "Disabled", 2: "Queued", 3: "Ready", 4: "Running"}
svc = win32com.client.Dispatch("Schedule.Service"); svc.Connect()
root = svc.GetFolder("\\")
for n in ["QuantLiveRun", "QuantLiveWatchdog"]:
    t = root.GetTask(n)
    print(n, bool(t.Enabled), S.get(t.State, t.State), t.NextRunTime)
```

```bash
# ③ NextRun 时刻：QuantLiveRun 次日 04:30、QuantLiveWatchdog 次日 04:35（本机 GMT+3）
```

> ⚠️ COM 返回的 `NextRunTime` 带 **`+00:00` 标签但实际是本机时间**（pywin32 标签错误）
> ⇒ 按**本机 GMT+3** 解读，**别当成 UTC 再换一次**（否则会差 3 小时）。

### 🚨 周末闸门在「脚本层」不在「任务层」（2026-09-27 实证）

| 任务 | 触发器 | 谁挡周末 | 强度 |
|---|---|---|---|
| `QuantLiveRun` | `Weekly DaysOfWeek=62`（周一~五） | **任务层** | ✅ **硬闸** |
| `QuantLiveWatchdog` | `Daily DaysInterval=1`（**每天**） | **脚本层**：`watchdog.py:73` 的 `if now.weekday() >= 5` | ⚠️ **单点** |

- **实证**：`watchdog.log` **零** 09-19（周六）/ 09-20（周日）记录（交易日 **74 条/天** vs 周末 **0 条**）。
- ⇒ **判断"周末恢复是否安全"必须查脚本层**：只看任务层会得出**两个相反的错误结论**
  （以为看门狗周末也跑 → 不敢恢复；或以为它周末不跑是任务层的功劳 → 误判防护强度）。
- ⇒ **推论（供 H38 修复用）**：脚本层闸门**只在"任务已被拉起后"才生效**
  ⇒ **硬闸只能是任务层**（这就是国庆方案 C「禁任务层」姿势正确的原因）；
  H38 加日历校验时**应把周末/节假日判定收敛到一处**（勿新增第三处判定，`watchdog.py:73` 改为复用同一判定）。

**⑥ 磁盘 + 仓库 + 依赖**：

```bash
df -h /c /d
git status --short && git rev-parse --short HEAD
for f in live_run.py live_match_loop.py watchdog.py core/data_fetch.py \
         core/market_collector.py core/adjustment.py core/sentiment_data.py; do
  [ -f "$f" ] && echo "✅ $f" || echo "❌ 缺 $f"; done
```

### ✅ 任务实例状态（Ready / Running）—— 已找到可用查法（2026-09-23 实证）

`IgnoreNew` 下，若上次实例仍 Running，次日触发**静默不生效**。三条常规路全堵：

| 路径 | 结果 |
|---|---|
| `schtasks /query` | **安全策略黑名单**（Program Blacklist） |
| 终端工具（PS） | **「exit 0 + 空输出」= 假绿** |
| `wevtutil qe Microsoft-Windows-TaskScheduler/Operational` | **报「指定的参数太多／参数错误」**（多种写法均失败） |

**✅ 可行替代：用 pywin32 读 TaskScheduler 操作日志**（Anaconda 自带 `win32evtlog`）：

```bash
# 🚨 脚本在【技能目录】下、不在项目里 ⇒ 必须用绝对路径（2026-09-27 核出：原写法 `scripts/...` 相对项目根，跑不通）
SK="C:/Users/27329/.workbuddy/skills/quant-close-inspection/scripts/check_task_state.py"
D:/Anaconda3/python.exe "$SK"            # 默认 QuantLiveRun
D:/Anaconda3/python.exe "$SK" QuantLiveWatchdog
```
（脚本随本技能：`scripts/check_task_state.py`，**只读**）

> ⚠️ **实测局限（2026-09-27）**：该脚本靠 `win32evtlog` 扫事件日志，**只覆盖最近约 6000 条事件**。
> 若任务上次运行在数天前（如长假后首次查），会报 **"命中 0 条"** —— **这不是"任务没跑"，是扫描窗口不够**。
> ⇒ **此时改用 COM 直读 `State`**（见上方「任务启用/禁用三路径核验」），`State=Ready` 即可判"未卡在 Running"。

**判据**：看最近事件——**最近的 `EID=102 TaskCompleted` / `111 TaskTerminated` 晚于最近的 `EID=100 TaskStart` ⇒ 已 Ready**。
事件含义：`100 TaskStart` / `129 CreatedTaskProcess` / `107 TimeTrigger` / `200 ActionStart` / `201 ActionCompleted` / `202 ActionFail` / **`102 TaskCompleted(OK)`** / `103 TaskCompleted(FAIL)` / `111 TaskTerminated(byUser)`。

### 🚨🚨 反面教训：**`schtasks /End` 对「未运行」的任务也报「成功」= 假绿**

**实况（2026-09-23 19:49）**：在**任务早已 Ready**（10:52:54 已 TaskCompleted）的情况下，
人工执行 `schtasks /End /TN QuantLiveRun` 返回 **「成功: 计划任务 "QuantLiveRun" 已经被成功终止。」**，
而**事件日志里那条 `/End` 没留下任何事件**（什么都没有可终止）。
- ⇒ **`/End` 的"成功"不能作为"它刚才在运行"的证据**，也**不能作为"现在已经干净"的证据**——它是个 **no-op 假绿**。
- ⇒ 判断状态**一律以事件日志为准**（上面的脚本），**不要用命令的自我报告**。
- ⚠️ 该脚本输出里 **`201 ActionCompleted` 的 `rc`** 有诊断价值：正常运行收尾 `rc=0`；
  被 `taskkill /F` 强杀则报 `rc=2147942401(0x80070001 ERROR_INVALID_FUNCTION)`——
  **但同一时刻的 `102 TaskCompleted(OK)` 仍会把实例置回 Ready**（⇒ 强杀**不会**导致次日被 IgnoreNew 跳过；此前的担心已被实证排除）。

**兜底链（即使真卡住也不是完全无声）**：看门狗 04:35 会看到 stale heartbeat + 无进程 → `[ALERT-DEAD]` → `schtasks /Run`（被 IgnoreNew 忽略）→ 20 秒后复查心跳仍未刷 → 在 `watchdog.log` 写 **「⚠️ 重启后心跳仍未刷新 → 需人工介入」**。

> ⚠️ **latent（未验证）**：看门狗的进程检测 `_live_run_processes()` 依赖 PowerShell。
> 本机 PowerShell 假绿 ⇒ 该函数会走 `return []` 分支。**至今从未被真实执行过**（心跳一直新鲜，早于该分支就 return 了）⇒ **此路径可靠性未知**。

### 🧹 仓库卫生：自动化会话记忆会落进项目仓库（2026-09-30 实测）

**现象**：`git status` 冒出未跟踪的 `?? .workbuddy/`（含 `memory/*.md` 与 `memory/automations/<id>/memory.md`）。
**根因**：**给自动化（定时动作）设 `cwds` = 某 git 仓库时，它会把会话记忆写进那个仓库**。
**风险**：未跟踪 ⇒ **任何 `git add .` 都会误提交它**；且工作区**永不干净**（巡检的"工作区是否干净"判据失效）。
**处置**（已做，提交 `fd5527f`）：`.gitignore` 加一条 `.workbuddy/`（放在"数据与运行产物"组，与 `live_outputs/`、`_tmp*.py` 同类）。
- ⚠️ **加 ignore ≠ 删目录**：目录与文件仍留在磁盘，**且冷备是复制目录、不受 gitignore 影响** ⇒ 记忆仍在备份保护内。
- ⚠️ **改 `.gitignore` 前先实测它的换行符**：本项目 `.gitignore` 是 **LF**（而 `.md` 台账是 **CRLF**）
  ⇒ Python 就地改写**必须带 `newline=''`**，否则会把整个文件的换行符改掉。
- 📌 本项目 `core.autocrlf=true` 而 `.gitignore` 工作区为 LF ⇒ git 提示 `LF will be replaced by CRLF the next time Git touches it`。
  **属既存不一致，不要顺手改**（会平白产生 28 行 diff）。
**下次设自动化时的检查项**：`cwds` 若指向 git 仓库 ⇒ **同步确认该仓库已 ignore `.workbuddy/`**。

## 2.7 盘中中断后「补跑采集」（2026-09-24 实证）

**前提：先确认网络已恢复**，否则白跑。用 `socket` 直连，**不要用 `ping`**（本机 ping 输出会被 shell 当二进制，`grep` 匹配结果不可读）：

```bash
D:/Anaconda3/python.exe -c "
import socket,time
for h,p in [('www.amazingdata.cn',443),('push2.eastmoney.com',443),('www.baidu.com',443)]:
    try: t=time.time(); socket.create_connection((h,p),timeout=6).close(); print('OK  ',h,round((time.time()-t)*1000),'ms')
    except Exception as e: print('FAIL',h,type(e).__name__,e)"
```

**① 备份先行（铁律）** —— 数据文件要被追加，先留底：

```bash
TS=$(date +%Y%m%d_%H%M%S); BK="C:/Users/27329/quant_app_backups/live_data_backfill_$TS"
mkdir -p "$BK/data" "$BK/live_outputs"
cp -p data/live/stock_300308_1m.csv "$BK/data/"
cp -p live_outputs/heartbeat.json "$BK/live_outputs/"
cp -a live_outputs/300308.SZ "$BK/live_outputs/"
sha256sum data/live/stock_300308_1m.csv; wc -l < data/live/stock_300308_1m.csv
```

**② 启动** —— 🚨 **不要用 `schtasks /Run`**：本机 `schtasks.exe` 在**安全策略黑名单**（`Permission denied`，不可绕过），且会与手跑进程形成**双份抢 AD 单点登录**。直接以后台任务方式跑生产入口：

```bash
cd "D:/Documents/ChatGPT/daily work/日内做T策略/quant_app"
D:/Anaconda3/python.exe live_run.py      # 须以「受管理的后台任务」方式运行
```

- ❌ **`subprocess.Popen(DETACHED_PROCESS)` 无效**：会话结束时子进程被一并收走（2026-09-24 实测 pid 2420 起后即死、心跳没动）。
- ⚠️ 手工启动的实例**绑在当前会话上**；会话结束它就死 ⇒ 之后靠看门狗兜（阈值 **20 分钟**），或让用户在自己终端里跑。
- ⚠️ 启动前确认**无进程持有 AD 登录**（任何重启都不能并行）。

**③ 核验（30–90 秒内应看到）**：
- `live_outputs/heartbeat.json`：`started_at` 更新、**`last_success_at` 不再是 `null`**、`collect_errors=0`
- `grep 采集完成 live_outputs/logs/live_$(date +%Y%m%d).log`
- 数据行增长；`[match]` 行报当轮 **`新bar` = 缺口根数**（例：`新bar: 120`）

**④ 数据完整性四查**：重复时间戳 = 0 / 时间单调 / 末字符 `0a` / `wc -l − 1` = 数据行（**口径差 1 = 表头**）。

**⑤ 关键认知：回补 bar 与实时处理「等价」**（这条决定"能不能计入样本"）
`test_h9_p0_gaps.py:49`（**H9-P0-1**）已证：**live 契约下 `now == market.timestamp` ⇒ 行情延迟恒为 0，120 秒 `stale_quote` 阈值永不触发**。
⇒ 回补的 bar **不会被"报价延迟"拒单**，引擎按 bar 序列确定性处理 ⇒ **交易结果与实时一致**。
- ⚠️ **唯一差异**：`day_high_equity` 按**循环轮次**采样（非逐 bar），中断期间无轮次 ⇒ **当日盘中权益高点缺失**，E1 口径须标注。

**⑥ 样本质量分层（2026-09-29 补全；**落账时必标档位**）**

| 档 | 判据 | 实例 | `day_high_equity` 失真 |
|---|---|---|---|
| **纯实时** | 全天 bar 均为当轮实时采到 | 多数正常日 | 无 |
| **混合** | 半天实时 + 半天回补 | **09-29**（上午回补 + 下午实时） | **仅缺中断的那半天** |
| **回补完整** | 全天靠事后回补（`fetch_today` 一次拉全天） | D6 / 09-24 | **缺整个交易日盘中高点** |
| **缺失 / 作废** | 数据不全、不可用 | D11（09-23，作废） | —（**不计入样本**） |

⇒ **排序：纯实时 > 混合 > 回补完整 > 缺失**。
🚨 **落账必写档位** —— 否则"权益高点 / E1 日内回撤分量"的失真程度**事后无法追溯**。
📌 **降档只影响 `day_high_equity` 与 E1 日内分量**：回补 bar 在引擎侧与实时**等价**（见 ⑤）
⇒ **成交、`engine_consistency`、日终权益均不受影响**。**不要把"降档"读成"整天数据不可用"**。

## 3. 输出格式（用户偏好）

1. **一句话结论**先行。
2. **对比表**呈现当日 vs 前一交易日（关键指标逐项对照）。
3. 七件终值用**编号表格**。
4. 明确回答**当日的预设看点/触发器**是否达成。
5. **警示**：权益变动若为底仓盯市（现金未变、orders/fills 未变），必须写明"**非策略做T收益**"，防误读为策略亏损。
6. 改动文件 + commit hash + 远端状态。

## 4. 纪律红线

- **live 观察盘标的池不能动**（改 = 前序不可比、计时归零）。
- **观察期内不改生产代码**；发现缺陷只登记不修。
- 结论强度必须匹配样本量；不确定处标「假设/待打点」。
- 任何"我核过了"的转述，**自己再跑一遍**（本次即靠全序列检索把"两处抽样否定"升级为"全序列确认"）。
- 🚨 **声称"某机制会解决 X"之前，必须 grep ① 调用点、② 实际传入参数**——**"机制存在" ≠ "机制被调用"**。
  只命中函数定义而无调用方 = 该能力**没有任何路径在使用**。
  ```bash
  rg -n "<机制函数名>" --glob '!tests/**'          # 只命中定义 = 零调用方
  rg -n "strftime|now\(\)|start|end" <调用方文件>   # 实际传进去的参数覆盖你的场景吗
  ```
  （实例：`fetch_incremental` 支持任意起止日 ⇒ 看似"可补历史"，但**零调用方**；
  实际只走 `fetch_today(code, now())` ⇒ **只拉当天**。据此下的"次日自动补齐"结论是错的。）
- ⚠️ **水位有两套口径，差恒为 1**：**数据行**（不含 CSV 表头，= `heartbeat.last_collected` = `_write_atomic` 的 `len(out)`，**权威**）
  vs **`wc -l`**（含表头）。验收**一律用数据行口径**，或直接读"**较昨日增量**"避免歧义。
