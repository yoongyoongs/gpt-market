# gpt-market V3 实时双流水线与 AI 买卖决策交互
## 修改方案及详细设计（V3 Integration / Product Closure）

> 文档版本：1.0  
> 日期：2026-09-02  
> 用途：交给 Codex 作为“实时行情 + 盘中决策 + 盘后复盘 + 持仓买卖 Review”集成收口的实施设计依据。  
> 设计性质：在既有 V3 Architecture Baseline 1.0、需求规格、详细设计、数据库设计和已实现 Phase 1–11 基础上做 **增量集成设计**，不是推翻重做。  
> 最高原则：**系统负责可验证事实与触发事实；AI 负责最终综合判断；用户确认真实资金行为。**

---

# 0. 执行摘要

当前系统已经具备大量 V3 基础模块，但存在一个关键产品形态问题：

- 盘后 EOD 数据链可以作为“正式日终结算”；
- 但如果只在每天收盘后完整运行一次，系统就会退化为“盘后选股/复盘系统”；
- 这无法满足实时行情系统的核心价值，也无法支撑用户在 10:00、11:00、13:40、14:40 或任意盘中时点询问“现在能不能买”“现在要不要减仓/卖出”。

因此，本次不应把系统简单改成“每天 18:45 全量跑一次”，而应形成 **双流水线 + 决策交互闭环**：

```text
                         gpt-market V3
                              │
          ┌───────────────────┴───────────────────┐
          │                                       │
          ▼                                       ▼
  Intraday Fast Lane                       EOD Slow Lane
  盘中实时事实与触发                         盘后正式结算与复盘
          │                                       │
  Quote / 指数 / 板块                         正式日 K / Revision
  1m / 5m / 15m / 60m                       Full Feature Run
  未收盘日 K / 周 K                          Market Regime EOD
  盘中量价 / 相对强弱                        Evidence 补齐
  轻量全市场异常扫描                         Full Recall
  Candidate / Watchlist / Portfolio 深度       Raw Opportunity
  Entry / Exit Trigger                       Performance Mature
          │                                   Recall Miss / Replay
          └───────────────────┬───────────────────┘
                              ▼
                         Context Pack
                              ▼
                          ChatGPT
                   综合判断买 / 等 / 卖 / 减 / 加
                              ▼
               Decision / EntryPlan / PositionReview
                              ▼
                    用户确认真实成交行为
                              ▼
                         Trade Ledger
                              ▼
                       Portfolio / Review
```

本次改造必须解决四件事：

1. **盘中数据持续更新，而不是只在收盘后更新。**
2. **盘中只做必要的增量计算，避免每分钟重跑全套 V3。**
3. **把 Entry Plan / Position Review 真正用于“什么时候买、什么时候卖”。**
4. **让 ChatGPT 能稳定读取系统事实，并把结构化判断回写为 Decision/Entry/Review；真实 Trade 仍需用户确认。**

---

# 1. 与原始 V3 设计的关系

## 1.1 不改变的原始目标

原 V3 最终闭环继续保持：

```text
全市场发现
→ 深度分析
→ 自选观察
→ 等待买点
→ 真实成交录入
→ 持仓分析
→ 卖点判断
→ Review
→ Performance
→ 历史复盘
```

本次改造只是把其中此前“模块存在但横向未真正接通”的部分连起来。

## 1.2 不改变的核心职责

### 系统负责

- 行情事实；
- K 线事实；
- 指数/行业事实；
- 基本面事实；
- Evidence；
- 数据质量；
- 技术结构计算；
- 支撑/压力的结构化事实；
- Entry Plan 条件是否客观满足；
- Stop/Target/Cancel 条件是否客观触发；
- Portfolio 数量、成本、盈亏；
- Trigger Event；
- 审计、版本、Hash、known_at。

### ChatGPT 负责

- 多周期综合判断；
- 正反 Thesis；
- 候选横向比较；
- Action Candidate 判断；
- Entry Ready / Wait / Cancel；
- Entry Plan；
- 持仓 HOLD / ADD / REDUCE / EXIT 判断；
- 市场变化后是否需要修改 EntryPlan；
- Risk / Time Efficiency 判断。

### 用户负责

- 是否实际买入；
- 是否实际卖出；
- 真实成交确认；
- Strategy 激活审批；
- 资金行为最终责任。

## 1.3 本次明确不做

- 不接券商自动下单；
- 不允许 AI 直接写真实 Trade Ledger；
- 不允许行情 Trigger 直接变成真实 BUY/SELL；
- 不用固定 `final_total_score` 替代 ChatGPT；
- 不把 Portfolio 变成全市场硬过滤器；
- 不每分钟重算 5500 只股票的全部 250 日 Feature；
- 不长期保存全市场所有 Tick/分钟 K；
- 不因为实时化而破坏 `known_at` / Replay 约束。

---

# 2. 当前问题重新定义

## 2.1 问题 A：把“每天 18:45 跑一次”误当成整个实时系统的更新机制

18:45 只应该代表：

> 当天行情的 **正式 EOD Finalization / Settlement**。

不能代表：

> 系统一天只更新一次。

否则盘中：

- 最新价不进入 Context；
- 5m/15m/60m 不更新；
- 当日日 K 未收盘形态不可见；
- 突破、回踩、冲高回落无法实时判断；
- 量能扩张、板块转强无法实时判断；
- Entry Plan Trigger 无法监控；
- Stop/Reduce/Exit 条件无法监控。

## 2.2 问题 B：数据更新频率、Feature 计算频率、Recall 计算频率被混为一谈

必须区分：

```text
Data Fetch Frequency
!=
Feature Full Recompute Frequency
!=
Recall Full Scan Frequency
!=
Deep Context Refresh Frequency
!=
AI Analysis Frequency
```

实时系统并不意味着每 5 秒跑完整 Phase 1–11。

## 2.3 问题 C：Entry Plan / Position Review 有模型与表，但没有真正变成实时买卖闭环

当前已有：

- Action Candidate；
- Entry Assessment；
- Entry Plan Version；
- Position Review；
- Watchlist State；
- Trade Draft / Confirm；
- Position Projection。

但仍需将它们连成：

```text
实时行情
→ Trigger Engine
→ 最新 Context
→ ChatGPT
→ BUY_READY / WAIT / CANCEL
→ 用户成交
→ Position
→ 实时风险变化
→ ChatGPT Position Review
→ HOLD / ADD / REDUCE / EXIT
```

## 2.4 问题 D：ChatGPT 与系统之间仍缺少明确交互协议

当前应区分：

1. **READ**：ChatGPT 从系统读取行情事实；
2. **AI Result**：ChatGPT 产生 Decision / EntryPlan / PositionReview；
3. **Import**：结果进入系统；
4. **Trade**：真实资金行为必须由用户确认，不能由 AI Result 自动产生。

---

# 3. 目标总体架构

## 3.1 四层运行模型

```text
┌────────────────────────────────────────────────────────────┐
│ Layer 1: Market Fact Plane                                 │
│ Quote / Index / Sector / Kline / Evidence / Fundamental    │
└────────────────────────────┬───────────────────────────────┘
                             │
┌────────────────────────────▼───────────────────────────────┐
│ Layer 2: Signal & Trigger Plane                            │
│ Intraday Overlay / Structure / Recall / Entry Trigger      │
│ Stop/Target/Cancel / Attention Event                       │
└────────────────────────────┬───────────────────────────────┘
                             │
┌────────────────────────────▼───────────────────────────────┐
│ Layer 3: AI Decision Plane                                 │
│ Comparison / Context / ChatGPT / Decision / Entry / Review │
└────────────────────────────┬───────────────────────────────┘
                             │
┌────────────────────────────▼───────────────────────────────┐
│ Layer 4: Human-confirmed Capital Plane                     │
│ Trade Draft → User Confirm → Ledger → Position             │
└────────────────────────────────────────────────────────────┘
```

## 3.2 两条数据流水线

### Intraday Fast Lane

用于：

- 用户盘中问“现在怎么买”；
- Watchlist 触发；
- 已持仓风险变化；
- 新的盘中异常机会；
- Entry / Exit Trigger。

### EOD Slow Lane

用于：

- 当日正式日 K；
- 全市场完整 Feature；
- Market Regime 收盘版；
- Evidence 收盘补齐；
- Full Recall；
- 次日 Candidate Pool；
- Performance Mature；
- Recall Miss；
- Replay/Regression 低频工作。

---

# 4. Intraday Fast Lane 详细设计

## 4.1 盘中数据分层

### L0：全市场实时 Quote 层

建议默认：

- 交易时段 09:25–11:30、13:00–15:00；
- 如果上游支持批量请求：10–30 秒刷新一次；
- 如果上游限制较严：30–60 秒；
- 具体频率配置化，不硬编码。

需要字段：

```text
security_id
code
market
last_price
open
high
low
prev_close
change
change_pct
volume
amount
turnover_rate
volume_ratio
bid/ask（可取得时）
event_time
fetch_time
known_at
source
upstream_source
quality
stale
```

### L1：全市场 Intraday Overlay 层

不重新计算完整 250 日 Feature。

直接复用最近一个 Published EOD Feature，将今天实时 Quote 作为 Overlay：

```text
latest_price
vs_ma5
vs_ma10
vs_ma20
vs_ma60
vs_prev_high_20d
vs_prev_low_20d
intraday_return
intraday_range_pct
intraday_volume_ratio
intraday_turnover
intraday_relative_index
intraday_relative_industry
breakout_now
pullback_now
failed_breakout
near_support
near_resistance
```

此层可对全市场轻量更新。

建议：30–120 秒一次，依 Provider 容量动态配置。

### L2：重点池分钟结构层

只服务：

```text
EOD Candidate Pool
+ Watchlist
+ Portfolio
+ 盘中新异常股票
```

典型数量目标：100–300 只，而不是 5500 只。

周期：

- 1m：原始短周期；
- 5m；
- 15m；
- 60m；
- 日 K provisional；
- 周 K provisional。

建议：

- 1m Bar：每分钟；
- 5m/15m/60m：由 1m 或 Provider Kline 聚合；
- 可用时同时取 Provider 周期结果做一致性校验。

## 4.2 provisional / closed 语义

任何未完成 K 线必须明确：

```text
bar_status = PROVISIONAL
```

完成后：

```text
bar_status = CLOSED
```

例如盘中日 K：

```json
{
  "period": "1d",
  "trade_date": "2026-09-02",
  "bar_status": "PROVISIONAL",
  "as_of": "2026-09-02T10:30:00+08:00"
}
```

收盘确认后：

```json
{
  "period": "1d",
  "trade_date": "2026-09-02",
  "bar_status": "CLOSED",
  "as_of": "2026-09-02T15:00:00+08:00"
}
```

周 K 同理。

禁止把 provisional Bar 冒充正式历史 Bar。

## 4.3 DeepMarketDataAdapter

建议新增/补齐：

```python
class DeepMarketDataAdapter(Protocol):
    async def get_intraday_bars(
        security_id,
        periods=("1m", "5m", "15m", "60m"),
        as_of=None,
    ) -> IntradayBarsResult: ...

    async def get_intraday_structure(
        security_id,
        as_of=None,
    ) -> IntradayStructureSnapshot: ...
```

优先复用现有 Legacy：

- `MarketDataService`；
- ProviderManager；
- Eastmoney/Tencent/Sina 等现有能力；
- Kline aggregation。

不要为了 V3 再平行造一套行情 Provider。

## 4.4 IntradayStructureSnapshot

建议 Payload：

```json
{
  "security_id": "uuid",
  "as_of": "...",
  "known_at": "...",
  "latest_price": 9.35,
  "weekly": {
    "trend": "DOWN|SIDEWAYS|UP|UNKNOWN",
    "support": [],
    "resistance": [],
    "bar_status": "PROVISIONAL"
  },
  "daily": {
    "trend": "...",
    "reversal_state": "NONE|POSSIBLE|CONFIRMED|UNKNOWN",
    "support": [],
    "resistance": [],
    "bar_status": "PROVISIONAL"
  },
  "60m": {
    "trend": "...",
    "structure": "...",
    "support": [],
    "resistance": []
  },
  "15m": {
    "trend": "...",
    "structure": "..."
  },
  "5m": {
    "trend": "...",
    "structure": "..."
  },
  "volume_price": {},
  "relative_strength": {},
  "quality": {},
  "source_revisions": []
}
```

## 4.5 多周期判断强制规则

必须把用户要求固化为 Context Facts / AI Task Instruction，不作为服务器固定买卖规则：

1. 周 K 判大趋势；
2. 日 K 判断反弹是否升级为反转；
3. 60m 判断 Action / Entry 结构；
4. 15m/5m 优化实际执行点；
5. 周 K 下降时，默认把日内/日线拉升标记为“下降趋势中的反弹候选”，除非存在足够反转事实；
6. 不同周期冲突必须显式返回；
7. 不允许因为 5m 强就忽略周 K 明显下降。

---

# 5. 盘中全市场发现设计

## 5.1 为什么不能只依赖昨晚候选

EOD Candidate Pool 是盘中主要观察池，但盘中新发生的：

- 异常放量；
- 突然突破；
- 板块转强；
- 强于指数；
- 急跌后收回；
- 新闻/公告催化；

可能产生新机会。

因此必须有 Lightweight Intraday Scanner。

## 5.2 Intraday Lightweight Scanner

全市场仅使用轻指标：

```text
price change
volume / amount acceleration
turnover
volume ratio
relative index return
relative industry return
breakout distance
pullback distance
EOD support/resistance distance
intraday high/low behavior
liquidity
stale/quality
```

输出：

```text
IntradayAttentionCandidate
```

而不是直接输出最终买入股票。

## 5.3 Candidate Pool 合并

```text
EOD Raw Opportunity
+ EOD Top Candidates
+ Watchlist
+ Portfolio
+ IntradayAttentionCandidate
= Active Intraday Universe
```

去重后进入 DeepMarketData。

## 5.4 盘中不要重复 Full Recall

Full Recall 仍以 EOD 为主。

盘中只允许：

- Intraday Overlay Recall；
- Event-driven channel；
- Existing candidate reevaluation。

这样避免：

```text
每分钟读取 5500 × 300 日 K
```

造成数据库和 Provider 压力。

---

# 6. Evidence 双时段设计

## 6.1 盘中 Evidence

目标：及时发现：

- 临时公告；
- 午间公告；
- 业绩预告；
- 重大合同；
- 停复牌/风险事项；
- 行业/政策事件；
- 重大新闻。

建议：

- 重要官方公告源：5–15 分钟增量；
- 新闻：5–15 分钟增量；
- 财务：按事件/报告期；
- Policy：按现有 Provider 合理频率。

全部走现有：

```text
Fetch → Raw → Parse → Dedup → Normalize → Entity Link → Conflict → Evidence DB
```

## 6.2 EOD Evidence

18:45 流水线至少再执行一次：

- 当日公告补齐；
- 新闻补齐；
- 去重；
- Entity Link；
- Conflict；
- Decay；
- Coverage 计算。

## 6.3 Evidence 触发重新 Review

当 Evidence 满足以下条件：

```text
materiality >= threshold
AND
linked_security in Active Intraday Universe / Portfolio
```

生成：

```text
AttentionEvent(type=NEW_EVIDENCE)
```

而不是自动改变 Decision。

---

# 7. EOD Slow Lane 详细设计

## 7.1 运行时间

建议正式 EOD Pipeline：

```text
18:45 Asia/Shanghai
```

仅 A 股真实交易日运行。

原因：

- 给主要数据源一定收盘后稳定时间；
- 不与盘中 Fast Lane 混淆；
- 允许数据源收盘数据完成归档。

具体时间必须配置化。

## 7.2 正式链路

```text
Step 01 Trading Calendar Confirm
Step 02 Universe Refresh / LKG
Step 03 Daily Bar Increment
Step 04 Corporate Action / Adjustment
Step 05 Daily Bar Publish
Step 06 Weekly/Monthly Aggregate
Step 07 Full Market Feature Run
Step 08 Market Regime EOD
Step 09 Evidence EOD Increment
Step 10 Full Multi-Recall
Step 11 Raw Opportunity Publish
Step 12 Candidate Comparison Source Prepare
Step 13 Expected POST_MARKET Task Run
Step 14 Performance Mature
Step 15 Recall Miss Mature
Step 16 Projection Verify / Data Quality
```

## 7.3 失败重试

每一步必须有：

```text
status
attempt
retryable
last_error
started_at
finished_at
input_run_id
output_run_id
```

不允许：

```text
18:45 某一步失败 → 当天永远缺数据
```

## 7.4 Catch-up

系统启动或早晨检查：

```text
previous_trade_date EOD pipeline complete?
```

若：

```text
Bar SUCCESS
Feature FAILED
```

则从 Feature 继续，而不是重抓所有数据。

要求所有 Job 幂等。

---

# 8. “什么时候买入”完整决策闭环

## 8.1 买入不是一个单点价格

系统不应该只输出：

```text
9.30 买
```

正式 Entry Plan 应包含：

```text
entry_mode
entry_zone
trigger_conditions
confirm_conditions
cancel_conditions
stop_loss
first_target
second_target
expected_horizon
max_wait_sessions
risk_budget / suggested_size (optional)
reason
contrary_evidence
```

## 8.2 建议 Entry Mode

```text
PULLBACK_ENTRY      回踩买
BREAKOUT_ENTRY      突破确认买
RECLAIM_ENTRY       跌破后重新收回关键位
RANGE_LOW_ENTRY     震荡区低位
NO_ENTRY            当前不允许进入
```

这不是服务器硬判定，ChatGPT 结合事实选择。

## 8.3 Entry Readiness

复用现有 `EntryReadiness`：

```text
NOT_READY
WAIT_TRIGGER
READY
CANCELLED
```

建议 UI/ChatGPT 映射：

```text
NOT_READY     → 暂不考虑
WAIT_TRIGGER  → 等条件
READY         → 条件已满足，可考虑执行
CANCELLED     → 原计划失效
```

## 8.4 买点实时评估流程

用户问：

> “现在 XX 股票能买吗？”

系统流程：

```text
1. 查最新 Market Regime
2. 查当前股票 Decision / EntryPlan 最新版本
3. 拉最新 Quote
4. 拉周 / 日 / 60m / 15m / 5m
5. 查今天 volume / turnover / relative strength
6. 查最新 Evidence
7. 查 Trigger / Cancel 是否客观满足
8. 生成 DEEP Decision Context
9. ChatGPT 综合判断
10. 输出 READY / WAIT / CANCEL + 买入区间 + 止损 + 失效条件
11. 可生成新 EntryAssessment / EntryPlanVersion
12. 用户实际成交后才进入 Trade Draft / Confirm
```

## 8.5 ChatGPT 买入输出 Schema

建议新增正式 Contract：

```json
{
  "subject": {"code": "000000", "market": "SZ"},
  "decision": "BUY_READY|WAIT|NO_BUY|INVALIDATED",
  "entry_readiness": "READY|WAIT_TRIGGER|NOT_READY|CANCELLED",
  "entry_mode": "PULLBACK_ENTRY",
  "entry_zone": {"low": 9.28, "high": 9.36},
  "trigger_conditions": [
    "60m support holds",
    "15m volume-price confirmation"
  ],
  "cancel_conditions": [
    "daily structure invalidated"
  ],
  "stop_loss": {"price": 9.02, "reason": "..."},
  "targets": [
    {"price": 9.85, "type": "T1"},
    {"price": 10.30, "type": "T2"}
  ],
  "expected_horizon": "D3_10",
  "time_efficiency": "NORMAL",
  "bull_case": [],
  "bear_case": [],
  "risk": [],
  "confidence": "MEDIUM",
  "context_pack_id": "uuid",
  "context_pack_hash": "sha256",
  "as_of": "..."
}
```

注意：价格是当时 Decision 的计划，不是保证成交或收益。

## 8.6 买入后的状态变化

```text
WATCHING
→ WAIT_ENTRY
→ ACTION_READY
→ TRIGGERED
→ 用户真实 BUY Confirm
→ HOLDING
```

`TRIGGERED` 不能等价于 `HOLDING`。

---

# 9. “什么时候卖出”完整决策闭环

## 9.1 卖出判断必须基于真实持仓

卖出必须读：

```text
真实 quantity
average_cost
realized/unrealized PnL
Trade History
原始 Decision
Entry Plan
最新 Entry Plan Version
持有交易日
Market Regime
周/日/60/15/5
最新 Evidence
stop / target
支持/阻力
thesis status
```

不能只根据“今天跌 3%”判断。

## 9.2 Position Review 建议动作

建议冻结枚举：

```text
HOLD
ADD
REDUCE
EXIT
```

如兼容现有 `SELL`，可在 API 兼容层映射：

```text
SELL → EXIT
```

不要同时长期存在多个语义重复的动作。

## 9.3 触发 Review 的情况

### Price Trigger

- Stop 触发；
- Target 触发；
- 支撑破坏；
- 关键压力突破；
- 浮盈快速回撤。

### Structure Trigger

- 周/日趋势改变；
- 60m 结构破坏；
- 15m/5m 出现明显执行风险。

### Evidence Trigger

- 利空公告；
- 业绩大幅变化；
- 重大风险；
- 原 Thesis 被证伪。

### Time Trigger

- 超出 expected horizon；
- 时间效率持续下降；
- 多日无进展且机会成本上升。

## 9.4 卖出实时流程

```text
1. Trigger Engine 检测到持仓 AttentionEvent
2. 构建最新 Position Context
3. ChatGPT Position Review
4. 输出 HOLD / ADD / REDUCE / EXIT
5. 输出原因、比例建议、触发条件、失效条件
6. 写 PositionReviewResult（不是 Trade）
7. 用户决定是否真实卖出
8. 用户确认 Trade Draft
9. Ledger 更新
10. Position Projection 重建
```

## 9.5 PositionReviewResult 建议 Schema

```json
{
  "account_id": "uuid",
  "security_id": "uuid",
  "recommended_action": "HOLD|ADD|REDUCE|EXIT",
  "quantity_snapshot": 300,
  "average_cost_snapshot": 9.12,
  "latest_price": 9.65,
  "thesis_status": "MAINTAINED",
  "time_efficiency": "NORMAL",
  "reduce_ratio": 0.0,
  "exit_trigger": null,
  "add_zone": null,
  "updated_stop": 9.20,
  "updated_targets": [10.10, 10.60],
  "supporting_evidence": {},
  "contrary_evidence": {},
  "changed_facts": {},
  "new_risks": [],
  "reason": "...",
  "context_pack_id": "uuid",
  "context_pack_hash": "sha256",
  "as_of": "..."
}
```

## 9.6 REDUCE 与 EXIT 的差别

### REDUCE

适合：

- 到 T1；
- 短周期过热但大趋势未坏；
- 风险收益比下降；
- 事件不确定性提高；
- 需要锁定部分利润。

必须建议：

```text
reduce_ratio / suggested_quantity
```

### EXIT

适合：

- Thesis INVALIDATED；
- Stop / 大周期结构破坏；
- 原计划失效；
- 明确风险需要退出。

仍然只是建议，不自动成交。

---

# 10. Trigger / Attention Engine

## 10.1 为什么需要 Trigger Engine

如果没有 Trigger Engine，就只能靠用户不断问：

> “现在到了没？”

Trigger Engine 的作用是：

> **系统实时判断“哪些客观条件变了，值得让 AI 重新看”。**

它不负责最终买卖判断。

## 10.2 AttentionEvent 类型

建议新增：

```text
ENTRY_TRIGGER_NEAR
ENTRY_TRIGGER_MET
ENTRY_CANCEL_MET
STOP_NEAR
STOP_HIT
TARGET_NEAR
TARGET_HIT
STRUCTURE_CHANGED
NEW_EVIDENCE
RELATIVE_STRENGTH_CHANGED
TIME_EFFICIENCY_CHANGED
INTRADAY_ANOMALY
DATA_QUALITY_DEGRADED
```

## 10.3 AttentionEvent Schema

```text
attention_event_id
subject_type
security_id
account_id nullable
entry_plan_id nullable
position_review_id nullable
event_type
severity
facts
as_of
known_at
source_snapshot_ids
status = OPEN|ACKED|RESOLVED|EXPIRED
content_hash
```

## 10.4 去抖 / 防骚扰

同一条件不能每 10 秒发一次。

必须支持：

```text
dedupe_key
cooldown_seconds
material_change_threshold
```

例如 Stop 已触发后，不重复创建 100 个 `STOP_HIT`。

---

# 11. ChatGPT 与行情系统的交互设计

# 11.1 当前现实约束

当前系统主要 AI 客户端是 ChatGPT Web。

在不引入模型 API / Browser Bridge 的前提下，应将交互分为：

### Pull

ChatGPT 主动从服务器读取事实。

### Result Import

ChatGPT 产生结构化结果后，通过既有 AI Result Import 流程进入服务器。

### Human Confirm

真实资金行为仍由用户确认。

因此，不应假设服务器可以在任意秒级事件发生时“主动调用 ChatGPT Web”。

## 11.2 推荐三种交互模式

### Mode A：当前立即可用 — HTTPS READ + 用户在 ChatGPT 发起分析

用户：

> 今天全市场扫描一下。

ChatGPT：

```text
GET Market Overview
GET Raw Opportunities
GET Candidate Comparison Pack
GET TopK Context
→ 分析
```

用户：

> XX 现在能买吗？

ChatGPT：

```text
GET current decision context
→ 分析
```

优点：立即可用。

缺点：AI 结果回写仍需 Import/UI 协助。

### Mode B：Remote MCP READ — 推荐正式使用

现有 `/mcp/` 作为 ChatGPT 工具入口，提供只读 Tools：

```text
get_market_overview
get_market_regime
scan_intraday_attention
get_raw_opportunities
build_candidate_comparison
get_stock_decision_context
get_watchlist
get_position_context
get_attention_events
```

其中：

- Market Tools 可以 MARKET_READ；
- Portfolio Tools 必须 PORTFOLIO_READ；
- MCP Token 与业务 Scope 分离。

这样 ChatGPT 不需要理解每个 HTTP URL。

### Mode C：受限 Proposal Write — 后续可选

如果决定把受限 MCP Write 提前从 V4 下放到 V3.x，只允许：

```text
preview_ai_result_import
submit_ai_proposal_draft
```

禁止：

```text
confirm_trade
write_trade_ledger
activate_strategy
```

任何 AI Result Import 仍需：

```text
Preview → Human Confirm
```

如果不希望改变原 V3/V4 边界，则继续使用现有人类 UI 完成 Import。

## 11.3 新增聚合 READ API

为了减少 ChatGPT 多次调用和 Token，建议新增 **组合读取接口**，但它只是聚合层，不复制业务逻辑。

### Entry Decision Context

```http
GET /api/v3/stocks/{code}/decision-context?mode=ENTRY
```

返回：

```text
market_regime
security
latest_quote
intraday_overlay
multi_timeframe
feature_eod
latest_recall
latest_raw_opportunity
latest_action
latest_entry_assessment
latest_decision
latest_entry_plan
support_resistance
fundamental
evidence
attention_events
data_quality
```

### Position Decision Context

```http
GET /api/v3/portfolio/{code}/decision-context?account_id=...
```

返回：

```text
position
market
multi_timeframe
entry_plan
latest_decision
latest_position_review
trades
levels
evidence
risk
attention_events
data_quality
```

此接口必须要求 `PORTFOLIO_READ`。

## 11.4 聚合接口原则

- 只调用已有 Service；
- 不在 Router 内重新算业务；
- GET 不得创建持久对象；
- 如果需要持久 Context Pack，使用显式：

```http
POST /api/v3/context-packs/preview-or-build
```

或内部 Task Builder，不要让普通 GET 隐式写数据库。

---

# 12. AI Result 回写闭环

## 12.1 盘后

```text
MarketReview
CandidateComparisonResult
DecisionResult
EntryPlanResult
WatchlistProposal
```

可组成 Bundle。

## 12.2 盘中 Entry Review

生成：

```text
ReviewResult
EntryPlanResult (仅需要新 Version 时)
```

或：

```text
ActionCandidate
EntryAssessment
```

必须避免同一语义同时在两套模型重复保存。

建议职责：

- ActionCandidate / EntryAssessment：事实与 AI 当前执行层评估；
- Decision / EntryPlan：正式可审计计划；
- Review：已有计划发生变化后的追加解释。

## 12.3 Position Review

生成：

```text
PositionReviewResult
```

不得直接写：

```text
SELL trade
BUY trade
```

## 12.4 用户成交后

```text
Trade Draft
→ Preview
→ User Confirm
→ Trade Ledger
→ Position Projection
→ Watchlist HOLDING / CLOSED 根据 Ledger 投影
```

---

# 13. Task Profile 修改

原首批 Profile 保留：

```text
PRE_MARKET
OPEN_1000
MID_MORNING
AFTERNOON
CLOSE
POST_MARKET
WATCHLIST_REVIEW
POSITION_REVIEW
FIRST_DECISION
DEEP_REPLAY
```

建议补充或明确：

```text
INTRADAY_ATTENTION_REVIEW
ENTRY_TRIGGER_REVIEW
POSITION_TRIGGER_REVIEW
```

## 13.1 Task 的意义

Task 表示：

> 什么时候应该生成/更新 AI 判断。

不是：

> 行情什么时候刷新。

行情刷新由 Fast Lane Worker 管理。

## 13.2 用户原有盘中时间点

如果继续使用固定盘中 Review，可配置：

```text
10:00
11:00
13:40
14:40
```

这些任务用于 ChatGPT Review，而不是行情更新。

除此之外，AttentionEvent 可形成事件驱动 Review 候选。

## 13.3 无模型 API 时的事件处理

没有自动模型调用时：

```text
AttentionEvent
→ Dashboard / API 标记 NEED_AI_REVIEW
→ 用户进入 ChatGPT 或固定 Task 时一次性读取
```

不能把“系统已经产生 AttentionEvent”记录成“ChatGPT 已分析”。

---

# 14. Market Regime 盘中版与盘后版

## 14.1 EOD Regime

正式不可变 Snapshot：

```text
regime_type = EOD
bar inputs = CLOSED
```

## 14.2 Intraday Regime

实时/短 TTL：

```text
regime_type = INTRADAY
as_of = now
```

包括：

- 主要指数涨跌与结构；
- 上涨/下跌家数；
- 成交额进度；
- 涨跌停结构；
- 行业强弱；
- 大小盘/风格（数据可靠时）；
- 风险状态。

## 14.3 禁止事项

Regime 仍然只是事实：

```text
不能硬编码：BEAR → 禁止买入
```

ChatGPT 综合判断。

---

# 15. 支撑 / 压力 / Trigger 结构化计算

## 15.1 服务器提供事实级 Levels

建议 `StructureLevelService` 输出：

```text
level_type = SUPPORT|RESISTANCE|PREV_HIGH|PREV_LOW|MA|GAP|BREAKOUT_LEVEL
price
period
strength_facts
source
as_of
known_at
```

## 15.2 不用服务器输出“神奇买点”

服务器不说：

> 9.31 就是最佳买点。

服务器说：

```text
日线支撑：9.28–9.34
60m 前低：9.30
15m 当前回踩：9.33
成交量状态：...
```

ChatGPT 决定：

> 9.30–9.35 可作为计划区间，但要满足 xxx。

这样可解释、可复盘。

---

# 16. 数据存储与缓存设计

## 16.1 不长期保存全市场所有分钟数据

遵循原 Baseline。

### 最新 Quote

建议：

- Memory / Redis / existing cache；
- TTL；
- 可选短期持久化用于排错。

### 重点池分钟 K

可：

- Redis；
- SQLite/短期 PostgreSQL partition；
- TTL 5–20 个交易日。

具体由当前部署能力选择。

### 决策使用过的分钟事实

必须通过 Context Pack 保存 **当时实际被 AI 使用的结构化事实**，包括：

- Bar values / summarized structure；
- source；
- known_at；
- Hash；
- Input IDs。

这样即使原始分钟 K 过期，也能审计当时为什么判断。

## 16.2 永久保留

继续长期保留：

- Decision；
- EntryPlan Version；
- Review；
- PositionReview；
- Trade Ledger；
- Audit；
- Context Hash/Payload；
- 被引用 Evidence；
- 正式 EOD Revision；
- Replay 必要数据。

---

# 17. 新增/调整 Domain Contract

## 17.1 建议新增

```text
IntradayQuoteSnapshot
IntradayOverlayFeature
IntradayStructureSnapshot
AttentionEvent
EntryPlanPayload (typed)
PositionReviewPayload (typed)
DecisionContextResponse
PositionDecisionContextResponse
```

## 17.2 EntryPlan JSONB 继续使用但必须类型化

当前 `EntryPlanVersion.plan: dict[str, Any]` 可不改表。

应用层新增 Pydantic Schema：

```python
class EntryPlanPayload(...):
    entry_mode: ...
    entry_zone: ...
    triggers: ...
    confirms: ...
    cancels: ...
    stop: ...
    targets: ...
    max_wait_sessions: ...
    suggested_position: ... | None
```

这样避免不同 AI Result 各写各的字段。

## 17.3 PositionReview `recommended_action`

当前 DB 是字符串。

应用层建议加 Enum：

```text
HOLD
ADD
REDUCE
EXIT
```

Migration 是否需要改取决于当前已有数据兼容情况。

---

# 18. API 修改清单

## 18.1 保留已有

```http
GET /api/v3/market-overview
GET /api/v3/market-regime
GET /api/v3/universe/query
GET /api/v3/recalls
GET /api/v3/raw-opportunities
GET /api/v3/candidates/comparison-pack
GET /api/v3/stocks/{code}/context-pack
GET /api/v3/stocks/{code}/evidence
GET /api/v3/watchlist
GET /api/v3/portfolio/{code}/context
POST /api/v3/ai-results/imports/preview
POST /api/v3/ai-results/imports/{id}/confirm
```

## 18.2 建议新增 READ

```http
GET /api/v3/market/intraday-status
GET /api/v3/intraday/attention
GET /api/v3/stocks/{code}/intraday-structure
GET /api/v3/stocks/{code}/decision-context
GET /api/v3/portfolio/{code}/decision-context
GET /api/v3/entry-plans/{id}/versions
GET /api/v3/entry-plans/{id}/status
GET /api/v3/pipeline/eod/latest
GET /api/v3/pipeline/intraday/status
```

## 18.3 参数建议

```text
as_of
market
account_id
context_level
include_intraday
include_evidence
```

## 18.4 所有实时 READ 必须返回

```text
as_of
known_at
source
coverage
stale
quality
provisional flags
```

---

# 19. MCP Tool 设计

建议把 HTTP 聚合接口映射成 MCP Tool：

```text
market_overview()
market_intraday_status()
scan_opportunities(mode="EOD|INTRADAY")
candidate_comparison(codes|candidate_set)
stock_decision_context(code, mode="ENTRY|REVIEW")
stock_intraday_structure(code)
watchlist()
attention_events()
position_context(account, code)
position_decision_context(account, code)
```

## 19.1 Scope

### MARKET_READ

允许：

- 市场；
- Kline；
- Candidate；
- Evidence（公共）。

### PORTFOLIO_READ

允许：

- 持仓；
- 成本；
- 交易；
- Position Context。

### AI_PROPOSAL_WRITE（可选）

只允许：

- AI Result Preview / Proposal Draft。

### V3_WRITE

人类 UI 使用。

### STRATEGY_ADMIN

独立。

---

# 20. Security / 真实资金边界

必须保持：

```text
AI Recommendation != Trade
Attention Event != Trade
Trigger Met != Trade
Target Hit != Sell Trade
Stop Hit != Sell Trade
```

真实流程：

```text
AI recommends EXIT
→ user sees recommendation
→ user actually sells
→ Trade Draft / manual input
→ user confirms
→ Ledger
```

## 20.1 禁止 AI 自报 Human

所有 `confirmed_by` / `actor_type=HUMAN` 必须来自认证 Context，而不是 Body 自报。

## 20.2 Cloudflare Quick Tunnel

只作为当前开发测试入口。

地址不能写死在代码/Contract。

正式环境切固定 HTTPS 后无需修改业务 API。

---

# 21. Pipeline Worker 设计

建议拆：

```text
IntradayQuoteWorker
IntradayOverlayWorker
DeepPoolWorker
IntradayEvidenceWorker
AttentionEngineWorker
EODPipelineOrchestrator
CatchUpWorker
PerformanceMatureWorker
ProjectionVerifyWorker
```

不一定每个都独立 systemd 进程。

小规模部署可一个 Worker Process + 内部 Scheduler，但 Domain Job 必须逻辑隔离。

## 21.1 Intraday 时间控制

交易时段：

```text
09:15 prepare
09:25 auction/live start
09:30–11:30 running
11:30–13:00 lunch low-frequency / evidence continue
13:00–15:00 running
15:00–15:15 final intraday snapshot
```

休市时停止高频 Quote。

---

# 22. 推荐默认刷新频率

这些是初始配置建议，不是硬编码：

| 能力 | 初始建议 |
|---|---:|
| 全市场 Quote | 15–60 秒 |
| 指数 Quote | 10–30 秒 |
| 板块/行业 | 30–120 秒 |
| 全市场 Intraday Overlay | 30–120 秒 |
| 重点池 1m K | 1 分钟 |
| 重点池结构 | 1–3 分钟 |
| Attention 条件 | 30–60 秒 |
| Evidence 公告/新闻 | 5–15 分钟 |
| 固定 AI Review | 10:00 / 11:00 / 13:40 / 14:40（可配置） |
| Full EOD Pipeline | 18:45 |
| Performance Mature | EOD 后 |
| Replay | 非盘中低频 |

如果 Provider 限流，必须动态降级。

---

# 23. 数据质量降级

## 23.1 单源失败

```text
Primary fail
→ fallback provider
→ source explicit
```

## 23.2 分钟 K 不可用

Entry Context：

```text
60m = UNKNOWN
15m = UNKNOWN
5m = UNKNOWN
```

ChatGPT 不得给“精确短线买点”假装数据完整。

## 23.3 Market Regime stale

仍可返回，但必须：

```text
stale=true
reason
```

## 23.4 关键数据缺失时的 Decision Policy

如果：

- 最新 Quote stale；
- 60m 缺失；
- Entry Trigger 无法验证；

则 Entry Readiness 不得 `READY`，应为：

```text
WAIT_TRIGGER / NOT_READY / UNKNOWN-supported response
```

---

# 24. Dashboard 修改建议

新增两个区域。

## 24.1 Live Status

```text
Market Status: OPEN/CLOSED
Quote as_of
Quote Coverage
Intraday Worker
Active Pool Size
Attention Events
Evidence Freshness
```

## 24.2 EOD Pipeline

```text
Trade Date
Bar
Feature
Regime
Evidence
Recall
Raw Opportunity
Performance
Latest Success
Retry Count
Last Error
```

## 24.3 股票详情

显示：

```text
周 / 日 / 60 / 15 / 5
Entry Plan
Current Readiness
Triggers
Cancel Conditions
Stop / Targets
Attention Events
Latest AI Review
```

## 24.4 持仓详情

显示：

```text
成本 / 数量 / 盈亏
最新价
周 / 日 / 60 / 15 / 5
Decision / Entry Plan
Stop / Targets
最新 Position Review
Attention Events
```

---

# 25. ChatGPT 实际使用流程

# 25.1 用户问：“今天全市场有没有低位埋伏？”

```text
ChatGPT
→ market_overview
→ EOD raw opportunities + intraday overlay (如果盘中)
→ candidate comparison 20–100
→ TopK 10–20
→ TopK NORMAL/DEEP Context
→ 横向比较
→ 输出 A/B/C 候选
```

每只候选必须说明：

- 周 K；
- 日 K；
- 60m；
- 必要时 15m/5m；
- 位置；
- 支撑/压力；
- 量价；
- 相对指数/行业；
- 基本面；
- Evidence；
- 催化；
- 反方证据；
- 失效条件；
- 是否现在可买/等待。

# 25.2 用户问：“XX 现在能买么？”

ChatGPT 必须重新拉最新 Context，不能复用几小时前价格直接回答。

返回：

```text
READY / WAIT / NO_BUY / INVALIDATED
```

以及：

```text
买入区间
Trigger
Cancel
Stop
Targets
Horizon
```

# 25.3 用户问：“我的 XX 现在卖不卖？”

必须拉 `PORTFOLIO_READ` Context：

```text
成本
当前价格
真实数量
多周期
EntryPlan
Thesis
Stop/Target
Evidence
```

再输出：

```text
HOLD / ADD / REDUCE / EXIT
```

不能只基于当前价格。

# 25.4 用户已经成交

用户录入/截图：

```text
Draft
→ Confirm
→ Ledger
```

AI 不得替用户宣称已成交。

---

# 26. 主动提醒能力：当前与未来

## 26.1 当前 V3.x

系统可以实时创建 `AttentionEvent`，Dashboard 可以提示。

ChatGPT 可通过：

- 用户主动询问；
- 固定盘中 Review Task；
- 已配置的 ChatGPT 定时任务（如适用）；

读取 AttentionEvent 后分析。

## 26.2 限制

没有模型 API / Bridge 时，gpt-market 服务器不能假定自己能在任意事件发生时直接唤醒 ChatGPT Web 并获得分析结果。

因此要区分：

```text
SYSTEM_TRIGGERED
AI_REVIEW_PENDING
AI_REVIEW_COMPLETED
```

## 26.3 V4

未来接：

```text
LLM API / Browser Bridge / Event callback
```

可实现真正事件驱动 AI Review。

但依然复用本设计的 Context / Result Import / Human Confirm。

---

# 27. 实施阶段

不要一次大爆改，建议按以下顺序。

## RT-00：设计冻结与现状对齐

目标：

- 将本文与原 Baseline / Detailed Design 做变更追踪；
- 不删除旧设计；
- 明确这是 Product Closure Delta。

验收：

- 设计冲突列表；
- 需求追踪更新。

## RT-01：Intraday Market Data Adapter

实现：

- Quote Snapshot；
- 1m/5m/15m/60m；
- provisional 日/周；
- 数据质量。

验收：

- 任意重点股票可实时返回；
- source/known_at/stale 完整。

## RT-02：多周期结构与 Levels

实现：

- weekly/daily/60/15/5；
- support/resistance；
- 周日冲突；
- weekly_state 字段统一。

验收：

- 不再长期 UNKNOWN（数据可用时）；
- 周下降 + 日反弹能显式表达。

## RT-03：Intraday Overlay + Lightweight Scanner

实现：

- 全市场轻量 Overlay；
- IntradayAttentionCandidate；
- Active Pool。

验收：

- 不全市场重跑 250 日 Feature；
- 能发现盘中新异常。

## RT-04：Evidence Intraday + Attention Engine

实现：

- Evidence 增量调度；
- AttentionEvent；
- 去重/冷却。

验收：

- 新公告能触发候选/持仓 Attention。

## RT-05：EOD Pipeline Orchestrator

实现：

- 18:45 全链；
- retry；
- fallback；
- catch-up；
- Job Run 状态。

验收：

- `market-overview stale=false`（正常源情况下）；
- Recall / Raw 与最新 Feature 同链。

## RT-06：Entry Decision Context + Typed EntryPlan

实现：

- decision-context；
- EntryPlanPayload；
- Trigger / Cancel Evaluate；
- EntryAssessment。

验收：

- 可以回答“现在能买吗”；
- 缺关键实时数据不得 READY。

## RT-07：Position Decision Context + Sell Review

实现：

- Position Context 完整集成；
- HOLD/ADD/REDUCE/EXIT；
- Stop/Target/Thesis/Time Efficiency。

同时修复已有 Portfolio 重放问题。

验收：

- 可以回答“现在卖不卖”；
- Position Review 不创建 Trade。

## RT-08：ChatGPT/MCP Read Integration

实现：

- 聚合 READ；
- MCP tools；
- MARKET_READ / PORTFOLIO_READ；
- Attention read。

验收：

- ChatGPT 一次会话可完成扫描→深度→Entry/Position Review。

## RT-09：AI Result Import 收口

实现：

- Entry/Review Schema；
- Preview / Confirm；
- Audit；
- Idempotency。

验收：

- AI Result 能回系统；
- 不能写真实 Trade。

## RT-10：Performance / Replay / Runtime Closure

完成此前代码审计剩余：

- Performance Mature；
- Real Replay；
- Shadow runtime；
- Projection Verify；
- Audit chain。

此阶段不阻塞最早的实时分析可用性，但阻塞正式 Strategy 激活。

---

# 28. 测试设计

## 28.1 Unit

必须新增：

```text
intraday aggregation
provisional/closed
multi timeframe trend
support/resistance
entry trigger
cancel condition
attention dedupe
position review action enum
```

## 28.2 Integration

### Intraday

模拟：

```text
09:31
10:00
11:20
13:40
14:50
15:00
```

验证 Context 随行情变化。

### Entry

构造：

```text
WAIT_TRIGGER
→ 价格进入 Entry Zone
→ 60m 条件满足
→ READY
```

然后构造 cancel：

```text
关键位跌破
→ CANCELLED
```

### Position

构造：

```text
持仓 + Stop Hit
→ AttentionEvent
→ PositionReview EXIT
→ Ledger 不改变
```

只有用户 Confirm SELL 后：

```text
quantity changes
```

## 28.3 E2E

### Case 1：盘后发现 → 次日买点

```text
EOD Recall
→ Watchlist
→ 次日实时回踩
→ Entry Trigger
→ ChatGPT READY
→ 用户 BUY
→ HOLDING
```

### Case 2：持仓卖点

```text
HOLDING
→ 实时结构恶化
→ Attention
→ ChatGPT REDUCE
→ 用户 SELL 部分
→ quantity > 0
→ HOLDING
```

### Case 3：完整退出

```text
EXIT Review
→ 用户确认剩余 SELL
→ quantity = 0
→ CLOSED
```

## 28.4 Data Leakage

所有决策 Context：

```text
known_at <= as_of/decision_time
```

盘中 provisional 不能被 Replay 冒充正式历史。

---

# 29. 性能目标

## 29.1 READ

建议：

```text
latest quote cache P95 < 100ms
intraday structure P95 < 500ms（缓存命中）
decision context P95 < 1s
position context P95 < 1s
```

具体需生产压测确认。

## 29.2 Worker

- 网络调用锁外；
- 批量 Provider 优先；
- Active Pool 控制；
- 不因一个 Provider 卡死所有盘中行情。

---

# 30. 监控 / 运维

至少监控：

```text
quote freshness
quote coverage
active pool size
minute bar freshness
provider errors
fallback rate
attention event count
EOD pipeline status
feature/recall run age
evidence freshness
portfolio projection consistency
```

告警重点：

```text
市场 OPEN 但 Quote stale > 阈值
60m/15m 数据普遍缺失
EOD 上个交易日未完成
Portfolio projection mismatch
```

---

# 31. Codex 开发约束

Codex 必须遵守：

1. 以既有正式设计 + 本文 Delta 为依据；
2. 不重写 V3；
3. 优先复用 Legacy MarketData/Provider；
4. 一个 RT Task 一次开发；
5. 不顺手改无关 Phase；
6. 每个任务先读对应详细设计章节与相关代码；
7. 发现与正式设计冲突时报告 `DESIGN_CONFLICT`；
8. 每个 Task 有测试；
9. 不自动进入下一个 Task；
10. 不为了过验收伪造 `stale=false`、known_at、最新日期；
11. 不把 provisional 当 closed；
12. 不让 AI Recommendation 直接进入 Trade Ledger。

---

# 32. 最终产品验收标准

只有满足以下条件，才能称为“实时行情 AI 决策闭环基本完成”。

## 32.1 盘中行情

- 市场 OPEN 时 Quote 持续更新；
- 重点股票有 5m/15m/60m；
- 日/周 provisional 可见；
- 数据带时点/质量。

## 32.2 全市场

- EOD 有完整 Full Recall；
- 盘中有 Lightweight Scanner；
- 新机会可进入 Active Pool。

## 32.3 买入

用户问“现在能买吗”时：

- 自动获取最新行情；
- 自动获取多周期；
- 自动获取 EntryPlan；
- 自动检查 Trigger/Cancel；
- ChatGPT 给 READY/WAIT/CANCEL；
- 给出计划区间、Stop、Target、失效条件；
- 真实买入仍需用户确认。

## 32.4 卖出

用户问“现在卖不卖”时：

- 自动读取真实成本/数量；
- 最新多周期；
- Thesis / Evidence；
- Stop / Target；
- ChatGPT 给 HOLD/ADD/REDUCE/EXIT；
- 真实卖出仍需用户确认。

## 32.5 系统交互

- ChatGPT 能通过 HTTPS/MCP 读取所需事实；
- Market 与 Portfolio 权限分离；
- AI Result 可审计回写；
- AI 结果不能直接确认 Trade。

## 32.6 盘后

- EOD 全链自动；
- 失败自动重试；
- 次日自动补跑；
- 最新 Feature/Regime/Recall/Raw 同一数据链；
- Performance 自动成熟。

---

# 33. 最终使用体验

改造完成后，用户应该只需要自然语言提问。

### 场景一

> 今天全市场扫描，有没有低位埋伏？

系统 + ChatGPT 自动完成事实读取与筛选。

### 场景二

> 亚太股份现在 9.32，能买吗？

ChatGPT 不再要求用户手工报周 K、60m、量比，而是从 gpt-market 取最新事实后回答。

### 场景三

> 我持有 XX，成本 9.12，现在怎么办？

系统直接提供真实成本和当前行情，ChatGPT 输出 HOLD/ADD/REDUCE/EXIT。

### 场景四

系统发现：

```text
Entry Trigger Met
```

Dashboard 标记：

```text
NEED_AI_REVIEW
```

用户进入 ChatGPT：

> 看一下刚才触发的买点。

ChatGPT 读取最新 Context，给最终判断。

未来 V4 接入模型 API / Bridge 后，这一步可进一步事件驱动自动化。

---

# 34. 结论

本次改造的核心不是“把 18:45 调度修好”这么简单。

真正目标是把 gpt-market 从：

```text
盘后数据仓库 + 模块化 V3
```

升级为：

```text
实时事实底座
+ 盘中机会发现
+ 多周期 Entry 判断
+ 持仓实时 Review
+ ChatGPT 综合买卖决策
+ 用户确认真实资金行为
+ 盘后正式复盘学习
```

最终职责关系必须始终保持：

```text
行情系统：告诉 AI “现在发生了什么”
Trigger Engine：告诉 AI “哪些条件值得重新看”
ChatGPT：告诉用户 “基于这些事实，现在倾向买/等/减/卖，以及为什么”
用户：决定是否真的交易
Ledger：只记录用户确认后的真实行为
```

这才是实时行情系统存在的真正意义，也是原 V3“发现 → 买点 → 持仓 → 卖点 → 复盘”闭环的完整落地。
