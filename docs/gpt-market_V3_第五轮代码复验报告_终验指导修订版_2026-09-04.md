# gpt-market V3 第五轮代码复验报告（2026-09-04）

> 复验对象：最新完整代码包 `23f215fe-96c8-40c4-9ad9-b8192cf734ed.zip`
>
> 复验目标：
>
> 1. 复查第四轮报告 R4-01～R4-08 是否真正关闭；
> 2. 不以 `docs/V3第四轮整改落实记录_2026-09-04.md` 中的 “8/8=100%” 作为验收结论；
> 3. 继续沿真实交易 Runtime、Fast Lane、Decision Context、Attention、Point-in-Time、MCP 和数据质量向下检查；
> 4. 判断当前代码是否已经可以进入“部署 → 真实 A 股交易时段 → ChatGPT 每日低位埋伏选股”的最终阶段。
>
> 验收原则：**实际代码、实际调用路径、可复现行为、测试结果优先于文档状态。**

---

# 1. 执行结论

这版代码较第四轮之前又有明显进步。

第四轮要求中的下列内容已经有真实实现：

- CURRENT_ONLY API 边界；
- stale/suspended Attention Engine Gate；
- Intraday Fast Lane 代码与 Scheduler Runtime；
- Entry Decision Context 增加了较多聚合字段；
- Public Evidence subject allowlist / MarketReview 权限收紧；
- Kline period provenance；
- MCP candidate comparison + INTRADAY scan；
- Worker graceful shutdown / Shanghai timezone / 内存 heartbeat。

但是：

> **第四轮不能判定 8/8 全部 CLOSED。**

继续按真实交易路径检查后，本轮发现一个新的 **P0/P1 Runtime Blocker**，以及多项 **Fast Lane/Attention/Context 横向接线问题**。

当前最重要的问题不是“还缺多少模块”，而是：

> **当前实时事实的时间语义、全市场覆盖、Active Pool 深度刷新、有效 EntryPlan、盘中事件驱动链，仍有可能让系统在真实交易中给出错误或不完整事实。**

因此当前仍不建议直接标记：

```text
PRODUCT_CLOSED
LIVE_TRADING_ANALYSIS_READY
```

---

# 2. 当前代码包与测试

## 2.1 仓库

ZIP 包含完整 V3：

```text
app/v3/
app/api/v3.py
app/mcp/v3_tools.py
scripts/v3_scheduler.py
tests/v3/
migrations/
docs/
```

第四轮落实记录声明：

```text
R4-01 → R4-08 = 8/8 完成
tests/v3 = 396 passed / 90 skipped
```

本报告逐项重新核验。

---

## 2.2 Compile

实际执行：

```bash
python -m compileall -q app tests
```

结果：

```text
PASS
```

---

## 2.3 关键模块专项测试

执行：

```bash
PYTHONPATH=. pytest -q \
  tests/v3/test_entry_decision_context.py \
  tests/v3/test_intraday_market_data.py \
  tests/v3/test_intraday_fast_lane.py \
  tests/v3/test_attention_engine.py \
  tests/v3/test_phase6_read_api.py \
  tests/v3/test_v3_auth.py \
  tests/v3/test_intraday_loop.py \
  tests/v3/test_scheduler.py
```

结果：

```text
89 passed
1 skipped
7 failed
```

7 个失败全部是当前审计环境缺：

```text
asyncpg
```

导致 Scheduler 测试在构造 SQLAlchemy asyncpg engine 时失败。

没有出现业务断言失败。

---

## 2.4 较大范围 V3 测试

排除当前环境无法收集/运行的 Scheduler 与 FastMCP 相关项后：

```bash
PYTHONPATH=. pytest -q tests/v3 \
  --ignore=tests/v3/test_scheduler.py \
  --ignore=tests/v3/test_error_envelope.py \
  --ignore=tests/v3/test_feature_api.py
```

实际结果：

```text
358 passed
93 skipped
3 failed
```

其中：

- 2 个失败：当前审计环境缺 `asyncpg`；
- 1 个失败：ZIP 提取环境将中文文件名转换为 `#Uxxxx`，测试硬编码中文路径后找不到。

因此这 3 个失败目前不属于业务逻辑回归证据。

---

## 2.5 OpenAPI

实际构建 V3 OpenAPI 后：

```text
paths = 81
operations = 84
duplicate operation ids = []
```

此前 Duplicate Operation ID 已真实关闭。

---

# 3. 第四轮问题重新判定

| R4 | 第五轮判定 | 结论 |
|---|---|---|
| R4-01 CURRENT_ONLY / PIT | 🔴 REGRESSION / NOT CLOSED | 历史 as_of 拒绝有效，但 current 请求产生新的 false FUTURE_KNOWN_AT |
| R4-02 stale/suspended Attention | 🟢 逻辑已关 | Engine Gate 正确；但会被 R5-01 的 false stale 误触发 |
| R4-03 Intraday Fast Lane Runtime | 🟡 PARTIAL | 已接 Runtime，但 Active Pool Deep、coverage、来源隔离、cadence 等仍有问题 |
| R4-04 Entry Decision Context | 🟡 PARTIAL | 增加很多字段，但 Evidence 仍被错误写死 NOT_AVAILABLE，Recall/Raw 仅查前 200 |
| R4-05 Public 权限 | ✅ CLOSED | Evidence subject allowlist / MarketReview 收回有效 |
| R4-06 Kline provenance | 🟡 PARTIAL | per-period source 有了，但 known_at 语义错误，顶层 known_at 也过早 |
| R4-07 MCP | 🟢 MOSTLY CLOSED | comparison + intraday scan 已存在；读写边界正确 |
| R4-08 Worker Ops | 🟡 PARTIAL | graceful shutdown/Shanghai/内存 heartbeat 有；跨进程可观测性与 cadence 未闭环 |

---

# 4. R5-P0-001：CURRENT_ONLY Future Guard 会把正常新鲜 Quote 误判成未来数据

这是本轮最重要的问题。

当前 API：

```python
effective_as_of = _require_current_as_of(as_of)
```

如果调用方没有传 `as_of`：

```python
_require_current_as_of(None)
```

会在 **发起行情网络请求之前** 捕获：

```text
as_of = now
```

之后：

```text
GET Provider Quote
```

Provider 在真正拿到行情时生成：

```text
server_timestamp / known_at
```

自然会比请求开始时的 `as_of` 晚几毫秒到几秒。

但 `map_quote_snapshot()` 当前判断：

```python
if snapshot.known_at > as_of:
    stale_reason = "FUTURE_KNOWN_AT"
```

于是：

> **网络请求本身产生的正常时间差，会被当成前视数据。**

---

# 5. 本轮已经直接复现

构造一个正常 CURRENT 请求：

```text
as_of    = T
known_at = T + 10ms
event    = T + 5ms
quote.stale = false
quality = GOOD
```

实际 `map_quote_snapshot()` 输出：

```text
stale = true
quality = UNTRUSTED
stale_reason = FUTURE_KNOWN_AT
```

也就是说：

```text
请求开始
↓
真实 Provider 花 10ms 返回
↓
系统认为这 10ms 是“未来数据”
```

这是 CURRENT_ONLY 语义实现错误。

---

# 6. 为什么这个问题很严重

它会直接影响四条真实链路。

## 6.1 Entry Decision

正常新鲜 Quote 可能变：

```text
stale=true
```

从而：

```text
READY
↓
NOT_READY / MISSING_REALTIME_DATA
```

---

## 6.2 Position Decision

正常新鲜 Quote 可能被标 stale：

```text
stop_hit = UNKNOWN
target_hit = UNKNOWN
```

导致真正该检查止损/目标时被降级。

---

## 6.3 Attention Engine

R4-02 现在正确规定：

```text
stale / untrusted
→ DATA_QUALITY_DEGRADED
→ 不允许 STOP_HIT / TARGET_HIT
```

但 R5-01 会把**正常新鲜行情误判 stale**，导致：

```text
正常行情
→ DATA_QUALITY_DEGRADED
```

R4-02 的安全规则反而被错误时钟语义误触发。

---

## 6.4 Fast Lane

这个影响尤其大。

Fast Lane：

```python
as_of = loop_start_time

get_all_a_shares()
```

获取 5500+ 股票需要时间。

每个 Quote 的：

```text
server_timestamp
```

天然可能晚于 loop 开始时 `as_of`。

因此 cache miss / 新抓行情时：

```text
大量 Quote
→ FUTURE_KNOWN_AT
→ stale
```

Scanner 又明确：

```text
stale Quote 不进入候选
```

结果可能：

```text
candidate_count 大幅下降
甚至 0
```

所以这是当前真实盘中 Runtime 的直接阻断问题。

---

# 7. 正确的时间语义不能靠“多容忍几秒”解决

不要简单：

```text
known_at <= as_of + 5s
```

因为这会污染真正的 Historical PIT。

建议正式区分两个模式。

---

## 7.1 CURRENT Snapshot

对于：

```text
现在能买吗？
现在卖不卖？
盘中 Fast Lane
```

语义应该是：

```text
request_started_at = T0
facts fetched during request
max_fact_known_at = T1
context_as_of = max(T0, T1)
context_known_at = T1 / finalization time
```

即：

> 当前请求允许使用“本次请求过程中刚刚获得”的事实。

最后 Context：

```text
as_of >= max(component known_at)
```

---

## 7.2 Historical PIT

Replay/历史 Context：

```text
fixed_cutoff = T
```

必须严格：

```text
known_at <= T
event_time <= T
```

这里才做 Future Guard。

---

# 8. 还存在三个关联的时间语义问题

## 8.1 Entry Decision 顶层 known_at 太早

当前：

```python
known_at = self._clock()
```

发生在：

```text
Quote fetch
Structure fetch
EOD facts fetch
```

之前。

所以顶层 Context：

```text
known_at
```

可能反而早于其中的实际事实。

应在聚合完成后：

```text
known_at = max(component known_at, finalization time)
```

---

## 8.2 IntradayBarsResult 顶层 known_at 太早

当前：

```python
known_at = datetime.now(...)
for period:
    await get_kline(...)
```

同样在网络请求之前记录 known_at。

应在全部周期抓完后计算。

---

## 8.3 每周期 Kline known_at 使用了错误字段

当前：

```python
"known_at": result.data_timestamp
```

但：

```text
data_timestamp = 市场事件/数据时点
server_timestamp = 系统实际知道/抓取到事实时点
```

`known_at` 应更接近：

```text
server_timestamp
```

否则 Point-in-Time 审计语义错误。

---

# 9. R5-P1-002：Active Pool 建出来了，但 Deep 只跑 Scanner Candidate

原实时设计：

```text
EOD Candidate
+ Watchlist
+ Portfolio
+ Intraday Attention
= Active Intraday Universe
↓
DeepMarketData
```

当前代码确实 merge 了：

```python
pool = self._pool_service.merge(
    eod_candidates=...,
    watchlist=...,
    portfolio=...,
    intraday_attention=...
)
```

但是随后：

```python
report["deep"] = await self._deep_summaries(candidates, as_of)
```

传进去的是：

```text
scanner candidates
```

不是：

```text
merged pool
```

---

# 10. 实际影响

如果一只股票：

```text
Portfolio 持仓
```

但今天没有触发 Scanner 异常：

```text
candidate_count = 0
pool_size = 1
```

那么：

```text
deep = []
```

也就是：

> 持仓虽然进入 Active Pool，却没有得到 5m/15m/60m 深度刷新。

Watchlist-only / EOD candidate-only 同理。

这与 Active Pool 的产品目的不一致。

---

# 11. 正确修复

Deep 应基于：

```text
merged Active Pool
```

做 bounded prioritization。

建议优先级：

```text
1. Portfolio
2. ACTION_READY / WAIT_ENTRY Watchlist
3. EOD Candidate
4. Intraday Attention
```

然后：

```text
Top N / 100–300 bounded pool
→ Deep 1m/5m/15m/60m
```

不要只针对 Scanner Candidate。

---

# 12. R5-P1-003：Fast Lane 状态读取的失败隔离实际上没有做到

`_load_state()` 文档写：

> 特征失败不阻断 Recall / Watchlist / Portfolio。

但代码把：

```text
feature pagination
recall
watchlist
portfolio
```

放在同一个：

```python
try:
    async with uow:
        ...
except Exception:
    ...
```

如果：

```text
Feature Query 第一页失败
```

流程直接跳入 except。

于是：

```text
Recall 不读
Watchlist 不读
Portfolio 不读
```

---

# 13. 本轮直接构造验证

模拟：

```text
features.query → RuntimeError
watchlist 有数据
portfolio 有持仓
```

最终：

```text
features_error = RuntimeError
pool_size = 0
```

也就是说：

> 一个 Feature Repo 故障可以把 Active Pool 其它来源一起清空。

这与“来源独立降级”设计相反。

---

# 14. 正确方式

分别隔离：

```text
load_features()
load_eod_candidates()
load_watchlist()
load_portfolio()
```

每个来源单独：

```text
status
error
count
```

例如：

```json
{
  "sources": {
    "features": {"status":"FAILED"},
    "eod": {"status":"AVAILABLE","count":30},
    "watchlist": {"status":"AVAILABLE","count":12},
    "portfolio": {"status":"AVAILABLE","count":5}
  }
}
```

Feature 失败：

```text
Overlay 降级
```

但不应导致：

```text
Portfolio 消失
```

---

# 15. R5-P1-004：Fast Lane 没有验证“全市场覆盖率”

`get_all_a_shares()` 返回：

```python
(total, quotes)
```

例如：

```text
total = 5553
quotes = 3500
```

这是完全可能的。

Eastmoney 全市场分页实现中：

- 单页失败返回 Exception；
- 后续直接过滤掉失败页；
- parse 失败行也直接 skip；
- 最终仍正常返回 `total, quotes`。

而 Fast Lane 当前：

```python
_, raw_quotes = await get_all_a_shares()
```

直接丢掉：

```text
total
```

随后只：

```text
status = AVAILABLE
quote_count = len(snapshots)
```

---

# 16. 这意味着什么

例如：

```text
expected = 5553
actual   = 3100
coverage = 55.8%
```

系统仍可能：

```text
status = AVAILABLE
```

然后告诉你：

> 今日全市场扫描完成。

实际上半个市场没扫。

这与你的核心要求：

> “最真实、最全量、最准确”

直接冲突。

---

# 17. 必须增加 Coverage Gate

Fast Lane 至少返回：

```text
expected_count
quote_count
missing_count
coverage
```

例如：

```json
{
  "expected_count": 5553,
  "quote_count": 5488,
  "coverage": 0.9883,
  "status": "AVAILABLE"
}
```

低于阈值：

```text
PARTIAL
```

严重不足：

```text
UNAVAILABLE_FOR_FULL_MARKET_SCAN
```

不要把部分市场扫描说成全市场。

---

# 18. R5-P1-005：active_price_trigger_plans 并不是真正“当前有效 Plan”

Repository 当前：

```text
读取所有 EntryPlan
↓
每个 decision_id 取最新版本
↓
所有这些 Decision 都参与 stop/target 监控
```

也就是说：

> 每个历史 Decision 的最后一版 Plan 都可能继续被后台监控。

没有判断：

```text
这个 Decision 是不是当前 Decision
Watchlist 是否 CLOSED / INVALIDATED
是否仍有持仓
EntryPlan 是否已经结束
```

---

# 19. 实际风险

一只股票历史有：

```text
Decision A
Decision B
Decision C
```

每个都有 EntryPlan。

当前代码可能同时监控：

```text
A latest plan
B latest plan
C latest plan
```

于是旧计划的：

```text
STOP_HIT
TARGET_HIT
```

仍能产生 AttentionEvent。

虽然 dedupe key 不重复，但**事件本身可能是历史垃圾提醒**。

---

# 20. 必须冻结“Active Plan”的正式定义

建议至少：

```text
Entry 计划：
当前 Watchlist state 属于 WATCHING / WAIT_ENTRY / ACTION_READY
且只取当前 Decision 最新有效 Plan

Position 计划：
当前 Portfolio quantity > 0
且只取与真实 Trade/Current Decision 有效绑定的 Plan
```

历史 CLOSED / INVALIDATED / 已替换计划：

```text
不进入 resident monitoring
```

---

# 21. R5-P1-006：Entry Decision Context 的 Evidence 仍是假“无能力”

当前代码：

```python
facts = {
    "fundamental": NOT_AVAILABLE("NO_FUNDAMENTAL_SOURCE"),
    "evidence": NOT_AVAILABLE("NO_EVIDENCE_READ_API"),
}
```

但是项目已经有：

```text
ReadEvidenceService
uow.evidence
GET /api/v3/stocks/{code}/evidence
```

所以：

```text
NO_EVIDENCE_READ_API
```

在当前代码事实下已经不成立。

---

# 22. 为什么必须接 Evidence

以后你问：

> XX 现在能买吗？

我不只是看：

```text
价格
周/日/60/15/5
```

还要看：

```text
公告
催化
利空
业绩
政策
风险事项
反方证据
```

否则“完整 Decision Context”仍然要我额外拉另一个 API。

---

# 23. 推荐修法

Entry Decision Context 内直接：

```text
EvidenceReadQuery(
    subject_type=SECURITY,
    subject_id=market:code,
    as_of=current_snapshot_as_of
)
```

聚合进：

```text
evidence
```

Fundamental 如果目前没有可靠结构化源，可以继续：

```text
NOT_AVAILABLE
```

但 Evidence 不应继续假装没有。

---

# 24. Recall / Raw Opportunity 还有分页漏检

当前聚合 Context：

```text
latest_recall
latest_raw_opportunity
```

只读取：

```text
前 200 条
```

然后客户端找目标股票。

目标股票如果在：

```text
第 201 条以后
```

就返回：

```text
NOT_IN_LATEST_PAGE
```

这不等于：

```text
它不在最新 Recall / Raw Opportunity
```

最好新增 Repository：

```text
latest_recall_for_security(...)
latest_raw_opportunity_for_security(...)
```

不要靠前 200 条再客户端过滤。

---

# 25. R5-P1-007：Worker heartbeat 只有 Worker 自己看得到

现在：

```text
IntradayTriggerLoop.heartbeat
```

确实实现了：

```text
last_success_at
last_error_at
consecutive_errors
plan_count
evaluated
quote_failed
provider_health
fast_lane status
```

这是正确进步。

但搜索整个仓库：

```text
heartbeat
```

主要只存在：

```text
worker memory
tests
--intraday-once 输出
```

---

# 26. API/Dashboard 实际看不到 Resident Worker heartbeat

API 与 Worker：

```text
不同进程 / 不同容器
```

Worker 内存里的：

```python
self.heartbeat
```

不会自动出现在：

```text
/market/intraday-status
Dashboard
health/data-quality
```

所以如果：

```text
Worker 连续失败两小时
API 仍 healthy
```

页面可能不知道 Fast Lane 已死。

---

# 27. 正确收口

不需要重系统。

只要把一个小 heartbeat 节流写入共享存储：

```text
OperationalHealthEvent
JobHealth
或现有健康表
```

例如 30–60 秒更新一次。

API 返回：

```text
worker_last_success_at
fast_lane_last_success
consecutive_errors
quote_coverage
provider_health
last_error
plan_count
```

这才是真正“生产可观测”。

---

# 28. R5-P1-008：一个 300 秒 cadence 同时跑止损监控和全市场 Fast Lane

当前：

```text
V3_INTRADAY_INTERVAL_SECONDS=300
```

一个 loop 每轮：

```text
evaluate stop/target
+
run full-market Fast Lane
+
sleep 300s
```

这两个任务不应该同频。

---

# 29. 为什么 300 秒不合理

Stop/Target：

```text
5 分钟一次
```

对盘中风险提醒明显太慢。

如果你把：

```text
300 → 30s
```

又会导致：

```text
全市场 Quote
Feature page
Scanner
Deep
```

每 30 秒一起重跑，负担过大。

---

# 30. 正确方式

仍然可以保持：

```text
一个 Worker Process
```

但内部拆 due-time：

```text
plan trigger      30–60s
all-market quote  30–120s
overlay/scanner   30–120s
deep pool         60–180s
evidence          5–15min
```

不需要增加多个容器。

---

# 31. R5-P1-009：Intraday Evidence → NEW_EVIDENCE 仍没生产调用方

AttentionEngine 已经实现：

```python
record_new_evidence(...)
```

但是代码搜索：

```text
只有测试调用
```

Resident Runtime 没有实际调它。

当前 Evidence：

```text
主要仍在 EOD Pipeline
```

---

# 32. 实际产品影响

例如中午 12:30：

```text
持仓股票发布重大公告
```

或者 13:20：

```text
Watchlist 出现重大利空/重大合同
```

系统不能按设计：

```text
Evidence increment
→ materiality
→ Active Pool / Portfolio
→ NEW_EVIDENCE
→ NEED_AI_REVIEW
```

这条盘中链仍没闭环。

---

# 33. R5-P1-010：后台 Entry Trigger / Cancel 仍未真正监控

Attention enum 已经有：

```text
ENTRY_TRIGGER_NEAR
ENTRY_TRIGGER_MET
ENTRY_CANCEL_MET
STRUCTURE_CHANGED
```

但 Resident plan loop 当前只取：

```text
stop_loss
take_profit
```

然后：

```text
STOP / TARGET
```

On-demand：

```text
ReadEntryDecisionContext
```

会评估 typed Entry trigger/cancel。

但后台常驻 Runtime 不会。

---

# 34. 结果

你的计划状态：

```text
WAIT_ENTRY
```

即使价格/结构已经满足：

```text
Trigger
```

后台也未必自动产生：

```text
ENTRY_TRIGGER_MET
```

所以：

> “等买点，到了系统提醒我让 AI 重看”

还没有完整实现。

---

# 35. R5-P2-011：L1 Overlay 默认只加载 2000 只 Feature

当前：

```text
V3_FASTLANE_FEATURE_LIMIT=2000
```

而全市场约：

```text
5553
```

即：

```text
约 3500 只没有 EOD Feature overlay
```

Fast Lane 仍能扫涨跌/量比，但：

```text
MA
位置
EOD趋势
```

等 L1 Overlay 对剩余股票缺失。

对于你的低位埋伏策略，这个偏差不能忽视。

---

# 36. 建议

不是每次重算 Feature。

而是：

```text
加载全市场最近 Published compact feature snapshot
```

可以：

```text
limit 6000
```

或者：

```text
预生成 compact LKG cache
```

同时返回：

```text
feature_coverage
```

---

# 37. R5-P2-012：EOD Active Pool 来源目前不是 Raw Opportunity Pool

当前 Fast Lane：

```python
uow.recalls.read_results(... limit=200)
```

然后把 Recall result 当：

```text
EOD candidate
```

但正式设计的 EOD Candidate Pool 更接近：

```text
Raw Opportunity / Candidate Set
```

Recall channel hit 只是上游发现事实。

而且这里只取第一批：

```text
200
```

---

# 38. 建议

Active Pool EOD 来源应使用：

```text
latest Raw Opportunity
或正式 Candidate Set
```

不要：

```text
第一批 Recall channel hits
```

直接等同 Candidate Pool。

---

# 39. R5-P2-013：Watchlist 使用的是“最近变更事件”，不是当前 Watchlist 表

Fast Lane 当前：

```python
watchlist_changes(limit=500)
```

再判断：

```text
current_state == WATCHING
```

问题：

一个长期稳定在：

```text
WATCHING
```

的股票，如果最近 500 条 event 里没有它：

```text
它就不会进入 Active Pool
```

但 Repository 已经存在：

```text
read_watchlist(state, limit)
```

---

# 40. 正确 Source of Truth

Active Pool 应直接读取：

```text
当前 Watchlist state
```

而不是：

```text
Watchlist 事件历史的最近 500 条
```

事件表用于审计，不应替代 current state。

---

# 41. R5-P2-014：Kline `known_at` 使用 data_timestamp，语义错误

每周期当前：

```python
known_at = result.data_timestamp
```

但：

```text
data_timestamp = 数据事件时间
server_timestamp = 系统获得/抓取时间
```

Point-in-Time 的：

```text
known_at
```

应该使用系统知道该事实的时间。

这项应与 R5-01 一并修，不要分散打补丁。

---

# 42. Fast Lane 全市场 Provider fallback 还有现实限制

虽然 R4-06 已将 V3 Kline 接到 ProviderManager：

```text
Eastmoney
→ Tencent fallback
```

但：

```python
ProviderManager.get_all_a_shares()
```

目前仍只支持：

```text
Eastmoney
```

所以：

> 单股 Quote / Kline 有 fallback，
> **全市场 Fast Lane Snapshot 本身仍是东财单源。**

这不一定是代码 Bug，因为 Tencent full-market 能力/成本可能不同。

但必须配合：

```text
Coverage Gate
page error
retry
PARTIAL 状态
```

否则全市场扫描可靠性不足。

---

# 43. 本轮最终严重度清单

## P0 / P1 必须先修

### R5-01
CURRENT Snapshot 时间三元组：

```text
request-start as_of
导致正常 fetched quote 被判 FUTURE
```

### R5-02
真正 Active EntryPlan：

```text
历史 Decision Plan 不得继续 Resident Monitor
```

### R5-03
Deep 应针对 merged Active Pool：

```text
Portfolio / Watchlist / EOD 也必须进入 Deep
```

### R5-04
Fast Lane：

```text
expected_count
coverage
partial-market gate
source failure isolation
```

### R5-05
Active Pool Source：

```text
真正 EOD Raw/Candidate
当前 Watchlist
全市场 Feature coverage
```

### R5-06
Entry Context：

```text
真正聚合 Evidence
按 Security 精确读 Recall/Raw
```

### R5-07
Worker：

```text
split cadence
shared heartbeat
```

### R5-08
Runtime Attention：

```text
Intraday Evidence
Entry Trigger
Cancel Trigger
Structure Change
```

---

# 44. 第四轮 8 项的真实状态

更准确应写成：

```text
R4-01  ❌ NOT CLOSED / REGRESSION
R4-02  ✅ Core Logic Closed
R4-03  🟡 Partial
R4-04  🟡 Partial
R4-05  ✅ Closed
R4-06  🟡 Partial
R4-07  🟢 Mostly Closed
R4-08  🟡 Partial
```

所以第四轮落实文档：

```text
8/8 = 100%
```

只能理解为：

> “八项都有代码提交”

不能理解为：

> “八项都达到原验收目标”。

---

# 45. 以前仍未完全关闭的 V3 大项

本轮没有改变这些事实。

## 45.1 Performance Mature

目前自动：

```text
SELECTION
INITIAL_ENTRY
USER_EXECUTION
RISK_CONTROL
```

仍缺：

```text
ADD
REDUCE
FINAL_EXIT
```

即：

```text
4 / 7
```

仍是 PARTIAL。

---

## 45.2 Deterministic Replay

当前真实重算：

```text
Feature
Regime
Context Evidence Selection
```

仍未完整：

```text
Recall Channels
Raw Opportunity / Comparison
Entry Trigger
```

仍是 Partial Strategy Replay。

---

## 45.3 Industry

可靠版本化：

```text
Industry Classification
Industry Benchmark
relative_industry_strength
Industry Rotation
```

仍未完成。

保持：

```text
UNKNOWN
```

是正确做法，不能猜。

---

## 45.4 Audit

Principal / request_id 已明显增强。

但正式 Final Gate 仍建议：

```text
关键 Write → 同事务 AuditEvent
```

做完整矩阵检查。

---

# 46. mode=V2 与 V3 每日选股还有一个产品裁决

当前：

```text
mode=V2
```

时：

```text
Data Chain RUN
full-recall SKIP
```

如果目标只是：

> V3 未激活就完全不产生 V3 Candidate

可以。

但用户当前明确计划：

> **V3 做完以后先让我每天真实跑低位埋伏选股，看效果。**

那么最好定义：

```text
LIVE_RELEASE_MODE
!=
RESEARCH_SHADOW_RECALL
```

例如：

```text
V2 仍是正式 live
V3 Recall/Raw Candidate 以 SHADOW / OBSERVATION 每天跑
不影响正式策略
不影响真实资金
```

这样不需要为了观察 V3 效果就提前正式激活 V3。

这项需用户/设计裁决，不应由 Claude Code 擅自决定。

---

# 47. 下一轮整改顺序

不要扩新功能。

按以下顺序：

```text
R5-01 Current Snapshot Time Triad / False FUTURE_KNOWN_AT
↓
R5-02 Active EntryPlan 定义与查询
↓
R5-03 Deep on merged Active Pool
↓
R5-04 Full-market Coverage + Source Failure Isolation
↓
R5-05 EOD Raw Candidate + Current Watchlist + Feature Coverage
↓
R5-06 Evidence 聚合 + Security-specific Recall/Raw
↓
R5-07 Split Cadence + External Worker Heartbeat
↓
R5-08 Intraday Evidence + Entry Trigger/Cancel/Structure Attention
↓
全量 tests/v3
↓
PostgreSQL Integration
↓
部署
↓
真实交易时段验收
```

---

# 48. R5-01 必须增加的测试

## Case A：正常 CURRENT fetch

```text
request_start = T0
provider fetch completes = T1
known_at = T1 > T0
```

必须：

```text
quote = AVAILABLE
stale = false
```

最终：

```text
context.as_of >= T1
```

绝不能：

```text
FUTURE_KNOWN_AT
```

---

## Case B：真正 Historical PIT

```text
cutoff = T0
fact.known_at = T1
T1 > T0
```

Replay/PIT：

```text
必须拒绝
```

CURRENT 语义和 Historical 语义不能混用。

---

## Case C：Fast Lane cache miss

批量 Quote 全部在：

```text
loop_started_at
```

之后 1–5 秒返回。

必须：

```text
正常参与 Scanner
```

而不是全部 stale。

---

# 49. R5-03 必须增加的测试

构造：

```text
scanner candidates = []
watchlist = [600000]
portfolio = [000001]
eod candidate = [600519]
```

期望：

```text
pool_size = 3
deep includes:
600000
000001
600519
```

按 deep_limit/优先级裁剪。

---

# 50. R5-04 必须增加的测试

```text
expected_count = 5553
actual_quotes = 3000
```

必须：

```text
coverage < threshold
status = PARTIAL
```

不能：

```text
AVAILABLE/full-market complete
```

同时：

```text
Feature repo fail
```

必须仍加载：

```text
Watchlist
Portfolio
EOD Candidate
```

---

# 51. R5-02 必须增加的测试

一只证券：

```text
Decision A old
Decision B current
Decision C invalidated/closed
```

只允许当前有效：

```text
Decision B EntryPlan
```

进入 Resident Monitoring。

---

# 52. 真实部署前最后 Gate

修完以上后：

## 静态

```text
compileall
OpenAPI
Auth
tests/v3
```

## PostgreSQL

```text
Migration
Scheduler
Portfolio
Performance
Replay
Attention
FastLane
```

## 实盘交易时间

必须真实跑：

```text
09:30–10:30
或
13:00–14:30
```

检查：

```text
全市场 quote expected/actual/coverage
quote known_at / event_time
Feature coverage
Candidate count
Active Pool
Portfolio Deep
Watchlist Deep
5m/15m/60m
Fallback
Attention
Worker heartbeat
```

---

# 53. ChatGPT 最终使用链验收

系统通过以后，真实测试：

```text
Market Overview
→ EOD/Intraday Opportunity
→ Candidate Comparison
→ TopK
→ Decision Context
→ Evidence
→ 周/日/60/15/5
→ ChatGPT A/B/C
```

盘中再：

```text
最新 Quote
→ Trigger
→ READY / WAIT / NO_BUY
```

持仓：

```text
Realtime Position Context
→ HOLD / ADD / REDUCE / EXIT
```

所有事实：

```text
source
event_time
known_at
quality
stale
coverage
```

必须可见。

---

# 54. 当前完成度判断

这版已经不是“大部分功能没做”的状态。

技术模块非常多已经存在：

```text
Auth
Ledger
Correction
Opening
Adjustment
Weekly
Benchmark
Regime
Deep Market Data
Portfolio
Realtime Quote
Performance
Replay
Shadow
Release
Scheduler
OCR
ExpectedRun
Fast Lane
Attention
MCP
```

剩余问题越来越集中在：

```text
时间语义
数据完整度
Runtime Source of Truth
主动事件链
生产可观测性
```

因此可以理解为：

> **V3 已进入最后约 10%～15% 的 Product Closure / Runtime Correctness 阶段。**

这个百分比只是工程完成阶段估计，不代表投资策略效果或胜率。

---

# 55. 最终建议

当前仍然不要：

```text
接 GLM
做强势启动
做 AI Access
重做 Dashboard
增加新策略
```

继续只修 V3。

尤其：

> **R5-01 必须第一个修。**

因为如果时间语义不正确，后面的：

```text
stale
data quality
scanner
entry readiness
stop/target
attention
```

都会建立在错误事实之上。

下一版修完 R5-01～R5-08 后，不建议继续无限做第六、第七轮纯静态审计。

应该直接：

```text
最后代码审计
→ PostgreSQL
→ 部署
→ 真实交易时段
→ ChatGPT 实际低位埋伏选股验收
```

通过后，才真正进入用户最终目标：

> **每天由 gpt-market V3 提供事实底座，ChatGPT 直接完成真实全市场低位埋伏候选筛选、买点判断、持仓 Review，并由 Performance/Replay 持续验证效果。**


---

# 56. 本轮整改执行方式修订：从“问题清单”改为“冻结终验规格”

> 本章节为本报告的**强制执行补充**。  
> 若前文某项表述与本章节的验收方式冲突，以本章节为准。
>
> 本轮目标不是“修改了 R5-01～R5-08”，而是：
>
> **让 V3 低位埋伏 Product Closure 的真实 Runtime 链一次性达到可外部复验状态。**

## 56.1 范围冻结

本轮只允许处理：

```text
R5-01 Current Snapshot 时间语义
R5-02 Active EntryPlan
R5-03 Deep on merged Active Pool
R5-04 Full-market Coverage + Source Failure Isolation
R5-05 EOD Candidate / Current Watchlist / Feature Coverage
R5-06 Evidence + Security-specific Recall/Raw Context
R5-07 Split Cadence + Shared Worker Heartbeat
R5-08 Intraday Evidence + Entry Trigger/Cancel/Structure Attention
```

明确禁止：

```text
不接 GLM-5.3-Flash
不新增强势启动策略
不做 AI Access 页面
不重做 Dashboard
不扩新策略
不改 V1/V2 业务逻辑
不提前激活 V3 Strategy
不顺手重构无关模块
```

Claude Code 不得自行增加新 Phase。

---

# 57. 强制先做 Dependency Map，再改代码

修改任何代码前，必须先输出 R5-01～R5-08 的跨模块依赖图，至少覆盖：

```text
Provider
↓
Quote / Kline
↓
Fact Timestamp / Provenance / Quality
↓
Intraday Overlay
↓
Scanner
↓
EOD Candidate + Watchlist + Portfolio + Intraday Candidate
↓
Active Pool
↓
Deep Market Data
↓
Feature / Regime / Recall / Raw Opportunity
↓
Evidence
↓
Decision Context
↓
EntryPlan / Attention / Trigger
↓
HTTP / MCP
↓
ChatGPT
```

每个整改项必须说明：

```text
上游输入是什么
当前错误是什么
修改发生在哪里
下游哪些模块会受影响
有哪些正向场景
有哪些反向/异常场景
如何证明没有引入回归
```

如果无法写清楚依赖，不允许开始修改。

---

# 58. 状态规则：禁止开发者自行宣布 CLOSED

本轮状态只能使用：

```text
NOT_STARTED
IMPLEMENTED
TESTED
READY_FOR_EXTERNAL_REVIEW
BLOCKED
```

禁止：

```text
CLOSED
100%
全部完成
全部通过
Product Closed
```

`CLOSED` 只能由外部复验确认。

---

# 59. R5-01 Final Acceptance Matrix：Current / PIT 时间语义

这是本轮最高优先级。

## 59.1 Case T1：CURRENT 正常网络延迟

输入：

```text
request_start = 10:00:00.000
provider event_time = 10:00:00.080
provider known_at = 10:00:00.120
provider stale = false
provider quality = GOOD
```

期望：

```text
Quote AVAILABLE
stale = false
quality != UNTRUSTED
stale_reason != FUTURE_KNOWN_AT
context.as_of >= 10:00:00.120
context.known_at >= 所有 component known_at
```

禁止：

```text
仅因为 known_at > request_start 就判 FUTURE
```

## 59.2 Case T2：Historical PIT 真前视

输入：

```text
historical_cutoff = 2026-09-01 10:00
fact.known_at = 2026-09-02 10:00
```

期望：

```text
必须拒绝 / 不进入历史 Context
```

禁止使用“CURRENT 容忍”放宽 Historical PIT。

## 59.3 Case T3：Context 多事实一致

输入：

```text
Quote known_at = T1
Kline known_at = T2
Evidence known_at = T3
Regime known_at = T4
```

期望：

```text
context.known_at >= max(T1,T2,T3,T4)
CURRENT context.as_of >= max(所有当前事实 known_at)
```

## 59.4 Case T4：Fast Lane 批量耗时

输入：

```text
loop_start = T0
5500+ Quote 在 T0～T0+5s 期间完成
```

期望：

```text
正常新鲜 Quote 全部可参与 Scanner
不能因晚于 loop_start 被统一判 stale/future
```

## 59.5 Case T5：Kline known_at

必须区分：

```text
event_time / data_timestamp
known_at / server_timestamp
```

要求：

```text
known_at = 系统实际获得事实的时间
event_time = 市场事实发生时间
```

不得把 `data_timestamp` 冒充 `known_at`。

---

# 60. R5-02 Final Acceptance Matrix：Active EntryPlan

构造同一证券：

```text
Decision A = 历史旧 Decision
Decision B = 当前有效 Decision
Decision C = INVALIDATED/CLOSED
```

每个都有 EntryPlan。

Resident Monitor 期望：

```text
只监控 Decision B 的当前有效 Plan
```

必须保证：

```text
A 不产生 STOP_HIT
A 不产生 TARGET_HIT
A 不产生 ENTRY_TRIGGER_MET
C 不产生任何当前 Attention
```

持仓场景：

```text
quantity > 0
真实 Trade 绑定 EntryPlan B
```

应优先使用：

```text
真实 Trade / 当前 Position 对应的有效 Plan
```

不能退回历史旧 Plan。

必须在代码/Repository 层冻结“Active Plan”正式定义，不能依赖调用方猜。

---

# 61. R5-03 Final Acceptance Matrix：Merged Active Pool → Deep

## Case A：Scanner 为空

输入：

```text
Scanner = []
Portfolio = [A]
Watchlist = [B]
EOD Candidate = [C]
```

运行：

```text
FastLane.run_once()
```

期望：

```text
ActivePool = {A,B,C}
Deep 至少覆盖 {A,B,C}（受明确 deep_limit 时按冻结优先级裁剪）
```

禁止：

```text
Deep = []
```

## Case B：四来源都有

输入：

```text
Portfolio = [A]
Watchlist = [B]
EOD = [C]
Intraday Scanner = [D]
```

期望：

```text
ActivePool = {A,B,C,D}
```

Deep 输入必须来自：

```text
Merged Active Pool
```

不是：

```text
Scanner Candidates
```

## 优先级必须冻结

建议：

```text
1. Portfolio
2. 当前有效 Watchlist / ACTION_READY / WAIT_ENTRY
3. EOD Candidate
4. Intraday Candidate
```

如采用其它优先级，必须在设计中明确并测试。

---

# 62. R5-04 Final Acceptance Matrix：全市场 Coverage + 独立降级

## 62.1 Coverage Case

输入：

```text
expected_count = 5553
actual_quotes = 5500
```

必须返回：

```text
expected_count
actual_count
missing_count
coverage
```

## 62.2 Partial Case

输入：

```text
expected_count = 5553
actual_quotes = 3000
```

必须：

```text
status = PARTIAL 或 UNAVAILABLE_FOR_FULL_MARKET_SCAN
coverage ≈ 54%
full_market_complete = false
```

禁止：

```text
AVAILABLE
全市场扫描完成
```

## 62.3 Provider Page Failure

模拟：

```text
Eastmoney 20 页请求中 3 页失败
```

必须向下游传播：

```text
page/provider error
missing_count
coverage
quality/status
```

不得静默丢行后继续宣称完整市场。

## 62.4 Source Isolation

模拟：

```text
Feature Repo FAILED
Watchlist = [A]
Portfolio = [B]
EOD = [C]
```

期望：

```text
Feature status = FAILED
Overlay = DEGRADED
ActivePool 仍含 A/B/C
```

继续测试：

```text
Evidence failed → Quote/Scanner 仍工作
Recall failed → Portfolio/Watchlist 仍工作
Portfolio failed → 全市场 Scanner 仍工作
```

每个来源必须独立失败、独立标状态。

---

# 63. R5-05 Final Acceptance Matrix：正确的 Active Pool Source of Truth

## EOD Source

Active Pool 的 EOD 来源优先使用：

```text
latest Raw Opportunity / formal Candidate Set
```

不得直接把：

```text
Recall 第一页/前 200 条
```

等同正式 Candidate Pool。

## Watchlist Source

必须读取：

```text
当前 Watchlist current state
```

禁止用：

```text
最近 N 条 watchlist change event
```

代替 current state。

长期稳定 WATCHING 的股票必须仍可进入 Active Pool。

## Feature Coverage

低位埋伏依赖位置/均线/趋势，因此 L1 Overlay 应覆盖全市场近期 Published Feature。

必须返回：

```text
feature_expected
feature_actual
feature_coverage
```

不能默认只加载 2000 只而无覆盖率说明。

---

# 64. R5-06 Final Acceptance Matrix：完整 Decision Context

最终：

```http
GET /api/v3/stocks/{code}/decision-context
```

至少应聚合：

```text
security
current quote
quote source / upstream / known_at / stale / quality

market regime

EOD feature
position / relative position

weekly
daily
60m
15m
5m

relative index
relative industry（可靠数据不存在时 UNKNOWN）

latest recall
latest raw opportunity
latest action / entry assessment（存在时）

latest valid decision
latest valid entry plan

support / resistance

Evidence

Fundamental（可靠源不存在时允许 NOT_AVAILABLE）

Attention Events

Data Quality
```

要求：

```text
Evidence 必须真实调用现有 Evidence Read 能力
```

禁止继续返回：

```text
NO_EVIDENCE_READ_API
```

若项目已有 Evidence API/Service。

Recall/Raw 必须增加 Security-specific 查询，禁止：

```text
只取前 200 条再客户端找
```

然后把“没在前200”解释成“不存在”。

---

# 65. R5-07 Final Acceptance Matrix：Cadence + Shared Heartbeat

一个 Worker Process 可以保留，但内部必须拆不同 due-time：

```text
Entry/Stop/Target Trigger：30–60s
全市场 Quote/Overlay/Scanner：30–120s
Deep Active Pool：60–180s
Intraday Evidence：5–15min
```

不得再用单一：

```text
300s
```

控制所有任务。

## Worker Heartbeat

至少持久化/共享：

```text
last_success_at
last_error_at
last_error_type
consecutive_errors

quote_expected
quote_actual
quote_coverage

provider_health
active_pool_size
candidate_count
deep_count
plan_count
```

API/Dashboard 进程必须能读到。

必须模拟：

```text
连续 3 次 Fast Lane 失败
```

并验证 HTTP 状态接口能看到：

```text
degraded
last_error
consecutive_errors >= 3
```

禁止 heartbeat 只存在 Worker Python 内存。

---

# 66. R5-08 Final Acceptance Matrix：盘中事件链

必须走 Resident Runtime，不只测 Domain Service。

## stale

输入：

```text
quote.stale=true
price < stop
```

期望：

```text
NO STOP_HIT
允许 DATA_QUALITY_DEGRADED
```

## suspended

输入：

```text
quote.suspended=true
price > target
```

期望：

```text
NO TARGET_HIT
```

## Entry Trigger

输入：

```text
state = WAIT_ENTRY
价格/结构满足 typed Trigger
```

期望：

```text
ENTRY_TRIGGER_MET
```

## Cancel

输入：

```text
Cancel Condition 满足
```

期望：

```text
ENTRY_CANCEL_MET
```

## Intraday Evidence

输入：

```text
Portfolio / Watchlist 股票出现新 material Evidence
```

期望：

```text
NEW_EVIDENCE
NEED_AI_REVIEW（若正式设计如此定义）
```

## Structure Change

输入：

```text
60m/15m/5m 结构发生达到阈值的重要变化
```

期望：

```text
STRUCTURE_CHANGED
```

统一安全边界：

```text
AttentionEvent != Trade
TriggerMet != Trade
StopHit != SellTrade
```

---

# 67. Data Provenance 强制验收

Quote/Kline/Deep 每个关键周期都应暴露：

```text
period
source
upstream_source
event_time
known_at
stale
quality
confidence
fallback_used
provisional / bar_status
```

例如：

```text
60m:
source=provider
upstream_source=tencent
fallback_used=true
```

或者：

```text
60m:
source=aggregate
upstream_source=5m:eastmoney
```

禁止最终只输出：

```text
source=legacy-provider
```

而无法判断事实来源。

---

# 68. 真实用户场景 E2E Acceptance

最终必须至少跑 6 条横向场景。

## E2E-1：今天有没有低位埋伏？

必须完整通过：

```text
全市场 Quote + Coverage
→ Feature
→ Regime
→ Recall / Raw Opportunity
→ Candidate
→ Comparison
→ TopK
→ Active Pool / Deep
→ Evidence
→ Decision Context
```

最终输出足够事实供 ChatGPT 做 A/B/C。

## E2E-2：XX 现在能买吗？

必须：

```text
CURRENT Quote
+ Market Regime
+ Feature
+ Recall/Raw 来源
+ Evidence
+ 周/日/60/15/5
+ EntryPlan
+ Data Quality
→ READY / WAIT / NO_BUY / INVALIDATED
```

## E2E-3：持仓 XX 现在卖不卖？

必须：

```text
真实 quantity
真实 average_cost
CURRENT Quote
Trade/Plan
周/日/60/15/5
Evidence
Stop/Target
Data Quality
→ HOLD / ADD / REDUCE / EXIT
```

## E2E-4：Primary Provider 故障

模拟：

```text
Eastmoney fail
```

必须验证：

```text
Tencent fallback（支持处）
source/fallback_used 改变
quality/coverage 正确
无法 fallback 的部分明确 DEGRADED/UNKNOWN
```

## E2E-5：市场部分缺失

```text
expected=5553
actual=3000
```

ChatGPT-facing Context 必须明确：

```text
PARTIAL
coverage
```

不得包装成完整扫描。

## E2E-6：Historical PIT

```text
cutoff = T
```

任何：

```text
known_at > T
```

都不能进入历史事实。

同时 CURRENT 正常网络延迟必须合法。

---

# 69. 每一项必须提交 Evidence Pack

每个 R5 Task 最终必须提供：

```text
1. 修改文件
2. 修改函数/类
3. 原因
4. Dependency Map
5. 上游输入变化
6. 下游输出变化
7. Acceptance Cases
8. 每个 Case 的实际输入/实际输出
9. Negative Cases
10. Regression Cases
11. 测试命令
12. 测试数量
13. PostgreSQL Integration 结果（涉及 DB 时）
14. OpenAPI/Auth 结果（涉及 API 时）
15. 已知限制
16. 当前状态：READY_FOR_EXTERNAL_REVIEW / BLOCKED
```

只写：

```text
“已修复”
“测试通过”
```

不算完成。

---

# 70. Final Test Gate

完成 R5-01～R5-08 后，必须执行：

## A. Static

```text
python -m compileall -q app tests
git diff --check
OpenAPI duplicate operation id = 0
Auth matrix
```

## B. Full V3

```text
完整 tests/v3
```

不能只跑本次新增测试。

任何 skip 必须：

```text
有明确原因
不是把失败改成 skip
```

## C. PostgreSQL Integration

至少覆盖：

```text
Migration Head
Scheduler
Portfolio
Active EntryPlan
Performance
Replay
Attention
Fast Lane health state
ExpectedRun
Shadow/Release
```

测试数据库必须与生产隔离。

## D. Runtime Scenario Matrix

必须完整执行本章所有 Acceptance Cases。

任何 Case FAIL：

```text
状态不得 READY_FOR_EXTERNAL_REVIEW
```

---

# 71. 真实部署后的交易时段验收

外部复验通过后，才允许部署。

真实 A 股交易时段至少观察：

```text
09:30–10:30
或
13:00–14:30
```

必须实际记录：

```text
expected securities
actual quote count
coverage

Quote event_time
Quote known_at
Quote source
fallback rate

Feature coverage

Scanner candidate count
Active Pool size
Portfolio count
Watchlist count
Deep count

5m / 15m / 60m freshness

Attention count
Entry Trigger count
Data Quality events

Worker last_success
Worker last_error
consecutive_errors
```

然后完整走一次 ChatGPT 使用链：

```text
Market Overview
→ Opportunity
→ Candidate Comparison
→ TopK
→ Decision Context
→ Evidence
→ ChatGPT A/B/C
```

以及至少一只股票：

```text
“现在能买吗？”
```

若有真实持仓，再走：

```text
“现在卖不卖？”
```

---

# 72. 给 Claude Code 的最终执行指令

以下指令可直接作为本轮开发 Prompt：

> 本轮不是普通 Bug Fix，而是 **gpt-market V3 Final Product Closure**。  
> 以《gpt-market V3 第五轮代码复验报告（2026-09-04）》全文以及本报告第 56～71 章为本轮强制验收规范。
>
> **第一步不要改代码。** 先完整阅读正式 V3 Baseline、Realtime Integration 设计、本报告，并建立 R5-01～R5-08 的 Dependency Map 与 Final Acceptance Matrix。必须覆盖 Provider → Fact → Quality → FastLane → ActivePool → Deep → Context → Attention → HTTP/MCP 的横向影响链。
>
> 每项必须同时包含：
>
> ```text
> 正常场景
> 边界场景
> 失败场景
> 反向场景
> 跨模块场景
> 回归场景
> ```
>
> 然后逐项实现。
>
> **特别强制：**
>
> 1. CURRENT 网络请求正常延迟不得被判 FUTURE；Historical PIT 仍必须严格拒绝未来事实；
> 2. Context 的 as_of / known_at 必须与内部所有事实保持一致；
> 3. ActivePool 的 Portfolio / Watchlist / EOD Candidate 即使 Scanner 为空也必须进入 Deep；
> 4. 部分市场数据必须输出真实 expected/actual/coverage，并标 PARTIAL；
> 5. 任一来源失败不得清空其它来源；
> 6. Resident Monitor 只能使用当前有效 EntryPlan；
> 7. stale/suspended Quote 不得产生 STOP_HIT / TARGET_HIT / ENTRY_TRIGGER_MET；
> 8. Evidence / Entry Trigger / Cancel / Structure Change 必须有真实 Resident Runtime 调用方；
> 9. Decision Context 必须真实聚合 Evidence，Recall/Raw 必须按 security 精确查询；
> 10. Worker cadence 必须拆分；Heartbeat 必须跨进程可见；
> 11. 每个 Quote/Kline 周期必须暴露实际 source/upstream/known_at/quality/fallback/provisional；
> 12. 必须执行完整 Runtime Scenario Matrix，而不是仅增加单元测试。
>
> **本轮禁止：**
>
> ```text
> GLM
> 强势启动策略
> AI Access
> Dashboard 大改
> 新策略
> V1/V2 业务重构
> 无关重构
> 提前激活 V3
> ```
>
> 开发过程中不得自行标记 `CLOSED / 100% / 全部完成`。  
> 只能使用：
>
> ```text
> NOT_STARTED
> IMPLEMENTED
> TESTED
> READY_FOR_EXTERNAL_REVIEW
> BLOCKED
> ```
>
> 每个 Task 完成时必须提供 Evidence Pack：修改文件、函数、依赖、Acceptance Case 实际输入输出、负例、回归测试、完整 tests/v3、PostgreSQL Integration、OpenAPI/Auth 结果和已知限制。
>
> 所有 R5-01～R5-08 达到 `READY_FOR_EXTERNAL_REVIEW` 后立即停止，不进入其它功能，等待外部复验。

---

# 73. 本报告最终验收口径

从本修订版开始：

```text
“代码提交了”
≠
“问题关闭”
```

```text
“单测通过”
≠
“横向 Runtime 正确”
```

```text
“8/8 Implemented”
≠
“8/8 Closed”
```

最终判断必须遵循：

```text
设计要求
+
跨模块接线
+
Acceptance Matrix
+
完整 Regression
+
PostgreSQL
+
真实交易时段
```

全部通过后，V3 才可以从：

```text
READY_FOR_EXTERNAL_REVIEW
```

进入：

```text
PRODUCTION_DATA_READY
```

最终再进入：

```text
PRODUCT_CLOSED
```

