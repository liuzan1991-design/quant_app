# 实时增量循环架构（2026-09-07）

## 定位
把 `live_loop.py` 从「离线回放自检」升级为「实时增量撮合循环」。
只读 `data/live/`，不碰 AmazingData SDK。观察网格 + 均线波段两个组合。

## 三处关键设计

1. 水位线：存「已处理 bar 的 time 集合」，而不是最后一个时间戳。
   - 每次读最新 CSV，取 `time > 上一批已处理最大 time` 的新 bar；
   - 跨日不手动重置（新 bar 天然晚于旧 bar），但为防重复，用 time 集合去重。

2. 账户/状态只预热一次：
   - 首次启动用当前已有数据构造 broker + engine + 预热指标；
   - 之后每轮只对新 bar 逐根 `on_bar`，不重新 prepare，不重放历史。

3. 落盘：`quant_app/live_outputs/<symbol>/<strategy_id>/`。
   - `paper_state.json`：账户快照（PaperStateStore.save）
   - `paper_audit.jsonl`：审计日志（PaperStateStore.append_audit）
   - `daily_summary.json`：当日收盘摘要（含单日回撤 + 累计回撤）
   - `signals.jsonl`：信号流水（时间、方向、股数、原因）
   - `compare.json`：引擎一致性与执行层损耗（两个数分开记）

## 信号对比（决策5）
- 引擎一致性：实时逐根信号 ↔ 当天数据离线快速路径信号，重合度（观察期当回归监控）。
- 执行层损耗：经过真实撮合的成交信号 ↔ 当天离线快速路径信号，漏掉/拒掉的占比（观察期真正要盯）。
- 基准 = 当天实时数据本身跑快速路径，不翻更长历史区间。

## 实时主循环
1. 启动：读 live CSV → 若为空则等待（不硬跑）。
2. 首次：prepare 一次，设 watermark=已有最大 time 集合。
3. 每轮：读新 bar → 只处理 > watermark 的 bar → on_bar → 撮合 → 持久化。
4. 收盘判定：当日最后一根 bar 到 14:59 后写 daily_summary，reset 当日计数。

## 启动前必须冻结的参数（已拍板 2026-09-07）

- 仓位规则：单票 10%（2026-09-07 用户拍板），用启动当天现价算股数，但用「启动当天现价」计算股数，不用历史价。
- 300308（现价 880.11）：
  - base_position = int(100000/现价)//100*100 = 100
  - trade_shares = max(100, base*0.1//100*100) = 100
  - max_position = max(base*2, base+100) = 200
- 均线波段：position_pct = 10.0（回测脚本第 74 行口径，覆盖 default 90）。
- 单票敞口：按 10% 执行（用户拍板）；与回测 30% 口径不同，观察期绝对收益不可与回测直接比。
- 观察记录必须标注：绝对收益不与回测直接比，只比策略行为一致性（信号方向、成交模式、回撤特征、执行一致性）。

## 启动前必须冻结的参数（阻塞实时观察）

- 30% 仓位口径尚未接入 default_params。按 100 万×30%：
  - 300308 现价约 880 → 底仓 300 股、单笔 100 股（下取整到整百）；
  - 网格 trade_shares 也应同口径（约 30 股取整 100 或不交易，需确认）。
- 直接跑 default 会建 1000 股（88 万），仓位 88%，与回测 30% 口径不符。
- 结论：实时循环必须在启动时按当天价格显式计算 base_position / trade_shares /
  max_position，并冻结参数，否则观察期结果不可对照。

## 未完待定
- 30% 仓位口径已用 RiskManager 限仓，但 base_position 等参数仍是 default，观察期前需冻结。