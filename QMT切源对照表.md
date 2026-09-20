# QMT 切源对照表（AmazingData → xtdata）

> 性质：**活文档**，随代码维护（每次改动相关调用点后必须同步本表，否则它会退化成陷阱）。
> 建立：2026-09-20 ｜ 依据：迅投官方 `nativeApi/xtdata.html`（已归档 `D:\Codex输出\国盛QMT官方文档_20260920\`）
> 实测依据：`scripts/tool_qmt_ad_reconcile.py`（12 天 × 3 标的，`best_shift` 12/12 = −1 分钟）
> ⚠️ 本表只做**纸面对照**，不改任何代码。切源实施必须在**观察期结束 + 复权修复之后**（见 §4）。

---

## 0. 结论先行

| 问题 | 答案 |
|---|---|
| 要改几个文件？ | **3 个**：`core/data_fetch.py` / `core/adjustment.py` / `core/sentiment_data.py` |
| 要改几个调用点？ | **5 个**（其中 2 个含 AD SDK 直连） |
| 不用改的 | `incremental_update`（纯本地合并）、`core/data.py`（读本地 CSV）、`core/market_collector.py`（经 data_fetch 间接）、**全部策略与引擎** |
| 最大风险点 | 🔴 **复权因子**（§1.4）—— AD 与 QMT 给的不是同一个东西，且 **H13/H14 复权 bug 尚未修** |
| 代码格式要不要换？ | **不用**：两边的代码格式都是 `300308.SZ` 这种 `code.market`，`code_to_ad()` 可直接复用 |

---

## 1. 逐调用点对照（仅生产路径）

### 1.1 `core/data_fetch.py:43` — `_login()`

| | |
|---|---|
| **现在（AD）** | `ad.login(username, password, host, port)`，包了 45 秒软超时 + 独立线程 + `suppress_sdk_output` |
| **QMT 对应** | **无需登录**。`xtdata` 只连本地 miniQMT（`xtdata.connect()`；实测服务 `127.0.0.1:58610`），**登录态在客户端侧** |
| **要做的** | 整段（含 `load_credentials()` 与 `ad_credentials.json`）可删 |
| **风险** | ⚠️ 失去自愈能力：AD 掉线可脚本重登，**QMT 掉线只能靠客户端重登**；`get_market_data_ex` 会静默返回空 → 必须在 `fetch_kline` 里把"空返回"显式报错，不能当"无数据" |

### 1.2 `core/data_fetch.py:79` — `fetch_kline(code, begin, end, period="min1")` ← **核心**

| | |
|---|---|
| **现在（AD）** | `base = ad.BaseData()` → `calendar = base.get_calendar()` → `market = ad.MarketData(calendar)` → `market.query_kline([ad_code], begin_date=int, end_date=int, period=...)` |
| **QMT 对应** | 两步：① `xtdata.download_history_data(code, '1m', start, end)` ② `xtdata.get_market_data_ex([], [code], period='1m', start_time, end_time)`（或 `get_market_data`，见官方文档"获取行情数据"） |
| **要做的转换** | 见下表 |
| **风险** | ① **QMT 必须先 download 再 get**（官方原文："使用时需要先确保 MiniQmt 已有所需要的数据"）⇒ 失败模式与 AD 不同，多一个可能失败环节；② 返回 schema 不同（11 列 vs 7 列） |

**转换清单（7 项，全部已实测）**：

| # | 维度 | AmazingData | QMT xtdata | 处理 |
|---|---|---|---|---|
| 1 | 周期名 | `min1` / `day` | **`1m`** / `1d` | 建映射，别改调用方签名 |
| 2 | 代码格式 | `300308.SZ` | `300308.SZ` | **相同**，复用 `code_to_ad()`（BJ 段前缀集合略有差异，**需逐一核**） |
| 3 | 时间戳基准 | datetime（**bar 开始**） | epoch ms（**bar 结束**） | **shift −1 分钟**（12/12 天实测恒定） |
| 4 | 时间戳来源 | 直接用 `time` 列 | **索引字符串按本机时区渲染 ⇒ 禁用**；只用 `time`(epoch ms) 换算北京时 | 若误用索引，**差 5 小时**且随本机时区漂移 |
| 5 | 开盘竞价 | **并入** 09:30 一根 | **单列** 09:30 一根（连续竞价从 09:31 起） | 需把 QMT 的 09:30 与 09:31 合并成一根 ⇒ **合并规则 12/12 天实测通过，见 §1.2b** |
| 6 | 成交量单位 | **股** | **手** | **×100**（比值中位数 0.01；手数取整差**不可逆**） |
| 7 | 输出列 | `STD_COLS = [time, open, high, low, close, volume, amount]` | `[time, open, high, low, close, volume, amount, settelementPrice, openInterest, preClose, suspendFlag]` | 映射后裁剪到 `STD_COLS`，**下游零改动** |


#### 1.2b 开盘集合竞价合并规则 ← 7 项里**唯一涉及"语义选择"**的一项

⚠️ 前 6 项都是**已验证的事实**（12 天 × 3 标的恒定）；只有这一项要**定义"怎么合并"** ⇒ 单独写明，
**不许拍脑袋**。

| 字段 | 合并规则 |
|---|---|
| `open` | 取 **QMT 09:30 的 open**（= 集合竞价成交价） |
| `close` | 取 **QMT 09:31 的 close**（= 首根连续竞价收盘） |
| `high` | `max(QMT 09:30.high, QMT 09:31.high)` |
| `low` | `min(QMT 09:30.low, QMT 09:31.low)` |
| `volume` | `(QMT 09:30.volume + QMT 09:31.volume) × 100`（手→股；允许 1 手取整误差） |

**✅ 已完成多日复验（2026-09-20，12/12 天全部命中）**：

```bash
python scripts/tool_qmt_ad_reconcile.py --code 300308 \
    --local-csv "data/live/stock_300308_1m.csv" --check-merge --days 12
```

| 字段 | 规则 | 命中 |
|---|---|---|
| `open` | = `QMT 09:30.open` | **12/12** |
| `close` | = `QMT 09:31.close` | **12/12** |
| `high` | = `max(两高)` | **12/12** |
| `low` | = `min(两低)` | **12/12** |
| `volume` | = `(两量和) × 100` | **12/12** |

窗口：**300308 / 2026-09-03 ~ 09-18**。量命中放宽到 ±1 手（手数取整不可逆，实测最大差 43 股）。
产物：`test_outputs/qmt_ad_reconcile/auction_merge_300308_2026-09-03_2026-09-18_mergecheck.json`

**⇒ 第 5 项不再是"靠推"的规则，与其余 6 项同级（有实测依据）。**

> ⚠️ **差点误读，记一笔**：首跑输出里出现 `900.3900.3000000000001` —— 那是 **float repr 把相邻两列
> 拼在一起**（实为 `900.3` 与 `900.3000000000001`，**同一个值**）。
> ⇒ 已把打印统一为 **4 位小数**。**教训：数值列相邻拼接会伪造"差异"；
> 判定一致必须以数值断言为准（命中率 12/12），不能以肉眼读列为准。**


### 1.3 `core/data_fetch.py:113` — `incremental_update(...)`

**不用改。** 纯本地逻辑（读已有 CSV → 判覆盖 → `fetch_kline` → 去重合并 → `to_csv`）。
只要 1.2 的出口仍是 `STD_COLS`，这里一个字都不用动。

> 调用方：`core/market_collector.py:110`（live 采集增量）、`test_batch_ui.py` 等。
> `core/market_collector.py:98` 的 `fetch_today` 调 `fetch_kline(code, trade_day, trade_day, period="min1")`
> ⇒ **live 采集路径同样只经 `fetch_kline` 一个门**，切源后自动受益。

### 1.4 🔴 `core/adjustment.py:28` — `load_backward_factor(code)` ← **最大风险点**

| | |
|---|---|
| **现在（AD）** | `ad.BaseData().get_backward_factor([ad_code], is_local=False)` → **直接拿到"后复权因子"序列** |
| **QMT 对应** | `xtdata.get_divid_factors(stock_code, start_time='', end_time='')` → **除权数据（事件）** |
| **要做的** | ❗**不是改名，是重建口径**：QMT 给的是除权事件，得自己积乘成"每日后复权因子序列"，并保证与 `prepare_signal_prices` 的逐日对齐语义一致 |
| **风险** | 🔴 **本表最高**。① 两源给的不是同一种东西；② `prepare_signal_prices` 用 `factor.reindex(dates).ffill().bfill()` 做逐日对齐，一旦序列口径变，**全部历史 `signal_*` 价都变**；③ **H13/H14 复权类 bug 尚未修** ⇒ 若"修 bug"与"换源"同时动，**回归无法归因**（违反"一次只改一项"） |

⇒ **纪律：复权必须作为独立一步，排在取数切换之前**（见 §3）。

### 1.5 `core/sentiment_data.py:112` — `load_industry_level1()`

| | |
|---|---|
| **现在（AD）** | `ad.BaseData().get_calendar()` + `ad.InfoData().get_industry_base_info()` → 申万一级行业名 ↔ 指数代码 |
| **QMT 对应** | **无直接对应**。QMT 有 `get_stock_list_in_sector`（板块成分），但"申万一级行业 → 指数代码"映射属另一套体系，**待核** |
| **要做的** | 维持现状：**继续用本地缓存 `data/industry_level1.csv`**（已落盘，不再需要联网） |
| **风险** | 低。且 `sentiment_t` **不在 live 组合**，对观察期零影响 |

> 同类：`build_stock_list.py:57` 用 `ad.BaseData().get_code_list()` → QMT `get_stock_list_in_sector(...)`（一次性脚本，可缓）。

---

## 2. 非生产路径（切源时**不动**，但要记账）

`scripts/` 下仍有 **8 个脚本直连 AD SDK**（`compare_newstocks.py`、`compare_sector_peers.py`、`explore_industry_api.py`、`explore_sentiment_api.py`、`fetch_fin_301717.py`、`screen_202408_202508.py` 等）。

- 它们是**手动工具**，不跑 pytest ⇒ **不在 H19 离线守卫的覆盖范围内**。
- ⚠️ 将来若有人批量跑它们，**仍会抢 AmazingData 单点登录**（H19 同族风险）。
- ⇒ 切源时**不必改**，但应在收尾时标注"这批脚本已随 AD 退役"或补进守卫名单。

---

## 3. 建议实施顺序（**观察期结束后**才动）

| 步 | 动作 | 验收判据 |
|---|---|---|
| 0 | **前置**：先修复权类挂起项（H13/H14/H10/H11） | 复权相关信号价可解释、记录层可信 |
| 1 | 把 `fetch_kline` 改成**双源可切换**（如 `QUANT_DATA_SOURCE=ad\|qmt`，**默认仍 `ad`**） | 观察期零影响；AD 路径逐字节不变 |
| 2 | 用对账工具验证 QMT 分支：同一天两源 → 过完 §1.2 七项转换后**逐根一致** | `tool_qmt_ad_reconcile.py` 扩一个"转换后比对"模式，close 一致率 = 1.0 |
| 3 | **单独一步**处理复权：`get_divid_factors` → 自建因子序列 → 与 AD 因子序列**逐日对齐验证** | 因子序列逐日相对误差 < 某阈值（先摆分布再定，§6.2） |
| 4 | 复权对齐通过后，才切 **live 采集** | 采集落盘数据与 AD 路径同口径；`engine_consistency` 基线重跑并留档 |

> ⚠️ **步 2 与步 3 不可合并**：一个是"取数"，一个是"复权"，混做则回归失败时无法定位。

---

## 4. 与现有挂起项的关系

| 项 | 关系 |
|---|---|
| **A15**（接源口径实施 + `open` 定性） | **本表即 A15 的前置纸面工作**；§1.2 的 7 项转换就是 A15 要实施的内容 |
| **H13 / H14**（复权相关，仍未修） | 🔴 **切源必须排在它们之后**（§1.4） |
| **H10 / H11**（记录层） | 无直接冲突，但同属"观察期后先做"队列 |
| **H19 同族** | `scripts/` 8 个 AD 直连脚本不在守卫名单内（§2） |
| **H25**（本表来源的四条口径） | 已 12 天 × 3 标的验证恒定 |

---

## 5. 本表未覆盖

- **撮合与费用口径**：`xttrader` 侧（`XtQuantTrader` / `order_stock` / `query_stock_asset`）尚未对照
  —— 属"进仿真/极小仓位试跑"阶段的事（`A16` 已定案：国盛无模拟盘）。
- **tick / level2 订阅**：本项目当前只用 1 分钟线，未涉及 `subscribe_quote` 实时推送路径。

---

## 6. 修订记录

| 日期 | 变更 |
|---|---|
| 2026-09-20 | 建立。核实改动面 3 文件 / 5 调用点；7 项转换清单；复权定为最高风险 |
| 2026-09-20 | 采纳 codex 审阅意见：**新增 §1.2b 竞价合并规则**（写明 5 字段合并语义 + 1 天实证 + 复验命令；工具加 `--check-merge`）；**状态标为「待复验」** —— 当日 miniQMT 客户端已关闭，未取得多日样本 |
| 2026-09-20 | **§1.2b 复验完成**：miniQMT 恢复后跑通 `--check-merge`，**5 字段 × 12 天 = 60/60 全部命中** ⇒ 第 5 项由"待复验"升为**已验证**。同时修掉一处显示瑕疵（float repr 相邻拼接易误读 → 统一 4 位小数） |
