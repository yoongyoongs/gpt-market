# gpt-market V3 第四轮代码复验报告（2026-09-04）

> 复验对象：用户上传的最新完整代码包  
> 复验目标：
>
> 1. 复查第三轮报告 `R3-01 → R3-09` 是否真正关闭；
> 2. 继续检查真实交易 Runtime、Point-in-Time、Intraday Fast Lane、ChatGPT Decision Context；
> 3. 判断当前代码是否已经可以进入“部署 → 真实交易时段每日选股/买卖分析”的最终阶段。
>
> 验收原则：以实际代码、运行路径、可复现行为和测试为准；文档中的 `CLOSED` 只作为待核对声明。

---

# 1. 总结结论

这版比上一版又明显前进了一步。

第三轮报告中的多数问题已经**真正关闭**：

- R3-01 Scheduler `database` 初始化顺序：✅ 真修复；
- R3-02 Release Gate 与基础数据链解耦：✅ 技术层面修复；
- R3-03 GET Build 副作用：✅ 真正改成 POST Build / GET Read；
- R3-04 通用 `/context-packs/{id}` 匿名暴露：✅ 收回；
- R3-05 ExpectedRun schedule：✅ 不再统一 00:00；
- R3-07 Position Decision stale quote：✅ 不再给确定性 stop_hit/target_hit；
- R3-08 DeepMarketData 重复抓三次：✅ 改成一次；
- R3-09 OpenAPI Duplicate Operation ID：✅ 已消失。

R3-06（ProviderManager）也已落地 Eastmoney/Tencent fallback，但从“数据事实可追溯、缓存/聚合/降级完整性”角度仍有后续收口空间。

不过继续沿真实使用链检查后，发现以下新的核心问题：

## 本轮最重要

1. **实时 Decision Context 的 `as_of` 存在真实前视泄漏**：
   - 请求历史 `as_of`，Quote 可以来自未来；
   - Entry Decision 也会选择未来 Decision/EntryPlan；
   - Position Context 会混入当前 Position/Trade/Review/Regime。
   - 已在本次审计中直接复现。

2. **盘中常驻 Attention Loop 对 stale/suspended Quote 没有安全 Gate**：
   - on-demand Position Decision 已修；
   - 但 Runtime 仍可能用 stale quote 生成 `STOP_HIT / TARGET_HIT` AttentionEvent。

3. **Intraday Fast Lane 的 Overlay / Scanner / Active Pool 代码存在，但生产 Runtime 没接起来**：
   - MCP 自己明确返回 `INTRADAY = NOT_AVAILABLE`；
   - 当前常驻 Loop 只盯已有 EntryPlan 的 stop/target。

4. **Entry Decision Context 仍远少于正式集成设计要求**：
   - 当前只有 Decision/EntryPlan + Quote + Structure + Readiness；
   - 尚未聚合 Market Regime / Feature / Recall / Raw Opportunity / Action / Evidence / Fundamental / Attention / Data Quality。

因此当前判断是：

> **代码已经非常接近完整 V3，但仍不是 PRODUCT_CLOSED。**
>
> 现在已经不是“大模块没写”，而是最后一批非常关键的 **PIT 数据纪律 + Intraday Runtime 横向接线 + Decision Context 聚合** 问题。

---

# 2. 本次代码包与测试

## 2.1 代码包

ZIP 内约 400+ 文件，完整包含：

```text
app/v3/
app/api/v3.py
app/mcp/v3_tools.py
scripts/v3_scheduler.py
tests/v3/
migrations/
docs/
```

Migration 已到：

```text
0017_regime_stale_reason.py
```

`docs/V3第三轮整改落实记录_2026-09-03.md` 声明：

```text
R3-01 → R3-09 全部 CLOSED
tests/v3 = 368 passed / 89 skipped
```

本报告对这些声明逐项重新验收。

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

## 2.3 Targeted V3 tests

针对本轮关键模块执行：

```bash
pytest -q \
  tests/v3/test_v3_auth.py \
  tests/v3/test_intraday_market_data.py \
  tests/v3/test_entry_decision_context.py \
  tests/v3/test_read_position_context.py \
  tests/v3/test_position_decision_context.py \
  tests/v3/test_phase6_read_api.py \
  tests/v3/test_phase6_architecture_gate.py
```

结果：

```text
57 passed
1 skipped
```

---

## 2.4 大范围 V3 tests

当前审计环境缺 `asyncpg`，同时 FastMCP 完整依赖不可复现，因此排除两个依赖 `app.main/FastMCP` 的收集项后执行：

```bash
pytest -q tests/v3 \
  --ignore=tests/v3/test_error_envelope.py \
  --ignore=tests/v3/test_feature_api.py
```

结果：

```text
346 passed
92 skipped
9 failed
```

9 个失败均为当前审计环境：

```text
ModuleNotFoundError: asyncpg
```

没有从这 9 个失败中发现新的业务断言失败。

因此：

> 本环境无法独立证明开发者机器的 `368 passed / 89 skipped`，
> 但当前可运行的关键测试没有出现直接业务回归。

---

## 2.5 OpenAPI Operation ID

直接从 `app.api.v3.router` 构建 OpenAPI：

```text
paths = 81
operations = 84
duplicate operation ids = []
```

所以 R3-09 **真实关闭**。

---

# 3. 第三轮 R3-01 → R3-09 复验结果

| ID | 第四轮结论 | 说明 |
|---|---|---|
| R3-01 Scheduler boot | ✅ CLOSED | `database = build_database()` 已发生在 ReleaseResolver 之前 |
| R3-02 Release Gate | 🟢 CODE CLOSED | market/index/features/evidence 在 V2 下继续；full-recall 仍由 V3 Strategy Gate 控制 |
| R3-03 GET Write | ✅ CLOSED | GET comparison/context 只查已有结果；POST 才 Build/Publish |
| R3-04 ContextPack Public | ✅ CLOSED | `/context-packs/{id}` 已不在 Public Prefix |
| R3-05 ExpectedRun schedule | ✅ CLOSED | 支持 HH:MM 列表/受限 cron；无 schedule 不伪造 00:00 |
| R3-06 ProviderManager | 🟡 MOSTLY CLOSED | Eastmoney/Tencent fallback 已接；但仍绕过更完整 MarketDataService，且事实 provenance 仍不足 |
| R3-07 stale sell facts | ✅ CLOSED（on-demand） | Position Decision Context stale/LKG 返回 UNKNOWN |
| R3-08 Deep duplicate fetch | ✅ CLOSED | Position Context 一次获取 structure 后拆 60/15/5 |
| R3-09 operation id | ✅ CLOSED | OpenAPI duplicates = none |

---

# 4. R3-01：Scheduler boot bug 已真正修复

`scripts/v3_scheduler.py` 当前顺序：

```python
database = build_database(database_url)

report["release_resolution"] = await ReleaseResolver(
    lambda: SQLAlchemyUnitOfWork(database.sessions),
    ...
).resolve("production")

main, maintenance, database = build_orchestrators(
    database_url,
    release=report["release_resolution"],
    database=database,
)
```

这一点与第三轮 P0 要求一致。

之前：

```text
database 未赋值
→ ReleaseResolver 使用 database.sessions
```

的问题不存在了。

判定：

```text
R3-01 = CLOSED
```

---

# 5. R3-02：Release Gate 基础数据链冻结问题已修，但还有产品语义需要确认

当前定义：

```python
DATA_CHAIN_JOB_IDS = (
    "market-data",
    "index-benchmarks",
    "features",
    "evidence-increment",
)
```

当：

```text
effective_mode = V2
```

仍执行：

```text
market-data
index-benchmarks
features
evidence-increment
```

只跳过：

```text
full-recall
```

这修掉了第三轮最危险的问题：

> mode=V2 时整个 V3 数据事实停止更新。

因此代码层：

```text
R3-02 = CLOSED
```

---

## 5.1 但要确认你真正想要的产品行为

你目前仍明确：

```text
正式 Release mode = V2
```

而用户当前真实目标是：

> 先让 V3 每天真实选股、观察一段时间效果，再决定后续正式策略切换。

现在 V2 模式下：

```text
基础 V3 数据会更新
但 full-recall 不会自动执行
```

所以：

```text
V3 Raw Opportunity / V3 Candidate
```

是否能每天自动刷新，取决于你如何定义 `full-recall`：

### 如果 full-recall 是“V3 策略 live 执行”

现在 Gate 合理。

### 如果 full-recall 只是“研究候选生成”

那么在 mode=V2 阶段完全停掉它，会妨碍：

> V3 Shadow / Observation / 真实候选效果验证。

建议后续冻结一个语义：

```text
Live Strategy Release
!=
Research / Shadow Recall Generation
```

可以允许 V3 Recall 以：

```text
SHADOW / OBSERVATION
```

运行，但不成为 live strategy、不影响真实资金。

这不是当前必须立即重构的 P0 Bug，但正式上线前应明确。

---

# 6. R3-03：GET Build 副作用已经真正关闭

这是本轮比较满意的一项。

现在：

```http
GET /api/v3/candidates/comparison-pack
```

只按：

```text
comparison_pack_id
或 candidate_set_id
```

读取已存在 pack。

而：

```http
POST /api/v3/candidates/comparison-pack
```

才 Build + Publish。

股票 Context 也是：

```http
GET /stocks/{code}/context-pack
→ read latest existing

POST /stocks/{code}/context-pack
→ build/publish
```

因此：

```text
GET
→ 不再隐式写库
```

判定：

```text
API-002 / R3-03 = CLOSED
```

---

# 7. R3-04：通用 ContextPack 公共泄漏已修

当前：

```python
PUBLIC_MARKET_READ_PREFIXES = (
    "/evidence/",
)
```

`/context-packs/` 已经移除。

公开的股票 Context 只通过明确模板：

```text
/stocks/{code}/context-pack
```

所以之前：

```text
/context-packs/{uuid}
可能命中 POSITION Context
```

的问题已经关闭。

判定：

```text
R3-04 = CLOSED
```

---

# 8. R3-05：ExpectedRun schedule 已修

目前 TaskProfile schedule 支持：

```text
10:00,14:30
```

以及受限 5-field cron。

无 schedule 时：

```text
不自动创建 slot
```

无法解析 schedule 时：

```text
显式 UNSUPPORTED_SCHEDULE / error
```

而不是统一伪造：

```text
00:00
```

因此：

```text
R3-05 = CLOSED
```

---

# 9. R3-06：ProviderManager fallback 已接，但数据事实层仍应继续收口

当前 API/MCP/Scheduler 已从：

```python
IntradayMarketDataService(container.eastmoney)
```

改为：

```python
IntradayMarketDataService(container.provider_manager)
```

以及：

```python
DeepMarketDataService(container.provider_manager)
```

Scheduler resident loop 也构造：

```text
ProviderManager(
  Eastmoney,
  Tencent
)
```

Targeted tests 中 Provider fallback 通过。

因此第三轮“绑死 Eastmoney”问题已经实质改善。

---

## 9.1 但还不是最终理想状态

项目已有更上层：

```text
MarketDataService
```

它在 ProviderManager 外还提供：

```text
Kline cache
stale cache fallback
bounded concurrency
15/30/60 from 5m aggregation
week/month from day aggregation
metrics
provisional handling
```

当前 V3 Realtime 直接依赖：

```text
ProviderManager
```

因此这些更高层的降级能力并没有全部进入 V3。

尤其：

```text
week
```

ProviderManager 本身的 Tencent fallback 能力有限，而 `MarketDataService` 可以从 day 做 week 聚合。

---

## 9.2 provenance 仍然不够完整

`IntradayMarketDataService.get_intraday_bars()` 现在返回：

```python
source="legacy-provider"
```

`IntradayBarSeries` 也没有明确输出该 period 实际来自：

```text
eastmoney
tencent
aggregate:5m:tencent
cache
stale-cache
```

所以当发生 fallback 时，ChatGPT 只能知道：

```text
有数据
```

但不知道：

```text
这个 60m 到底从哪个源来的？
有没有 fallback？
质量如何？
known_at 是什么？
```

对你“最真实、最准确、可核验”的目标而言，这仍是数据事实层缺口。

因此：

```text
R3-06 = MOSTLY CLOSED
```

建议最终让 V3 realtime 依赖 `MarketDataService`，或者至少完整透传每周期 provider/source/quality/known_at。

---

# 10. R4-P0-001：实时 Decision Context 存在真实 Point-in-Time 前视泄漏

这是本轮最重要的新问题。

V3 的核心纪律一直是：

```text
known_at <= as_of
```

历史分析/Replay 绝不能看到未来事实。

但当前 realtime Decision Context 的 `as_of` 语义并不满足这个约束。

---

## 10.1 Quote 可以“把未来行情贴到过去 as_of”

当前：

```python
async def get_quote_snapshot(code, as_of):
    quote = await provider.get_quote(code)

    return IntradayQuoteSnapshot(
        event_time=quote.data_timestamp,
        known_at=quote.server_timestamp,
        as_of=as_of,
        stale=quote.stale,
        ...
    )
```

它只把调用方 `as_of` 原样写回。

没有检查：

```text
quote.known_at <= as_of
quote.event_time <= as_of
```

---

## 10.2 本次审计实际复现

我构造：

```text
requested as_of:
2026-09-01 10:00

Provider 返回 Quote:
event_time = 2026-09-03 10:00
known_at   = 2026-09-03 10:00
stale      = false
```

实际 V3 返回：

```text
as_of      = 2026-09-01
event_time = 2026-09-03
known_at   = 2026-09-03
stale      = false

known_at > as_of = TRUE
```

也就是说：

> 请求 9 月 1 日的 Decision Context，可以拿到 9 月 3 日的 Quote，而且仍然被标记为新鲜。

这是明确的未来数据泄漏。

---

# 11. Entry Decision 也会选中“未来 Decision”

`ReadEntryDecisionContextService.execute()`：

```python
bundle = await self._decision_bundle(code, market)
```

`_decision_bundle()` 没有接收 `as_of`。

随后：

```python
decision = max(decisions, key=lambda row: row["as_of"])
```

没有：

```text
decision.as_of <= requested_as_of
```

EntryPlan 同样：

```text
取 version 最大
```

没有真正做：

```text
effective_from <= as_of
known_at <= as_of
```

---

## 11.1 本次审计实际复现

构造：

```text
requested as_of = 2026-09-01

Decision A:
as_of = 2026-09-01

Decision B:
as_of = 2026-09-03
```

实际返回：

```text
Decision B（未来）
```

所以这是第二个独立可复现的前视泄漏。

---

# 12. Position Decision Context 也有同类问题

`PortfolioRepository.position_context()` 当前读取：

```text
当前 Position Projection
全部 Trade
最新 Position Review
最新 Decision
全部相关 EntryPlan
```

查询没有 `as_of` 过滤。

但上层：

```http
GET /portfolio/{code}/decision-context?as_of=...
```

允许调用者传历史 as_of。

同时 Position Context：

```python
regime = await uow.features.latest_regime()
```

也没有按 `as_of` 限制。

所以历史 `as_of` 时可能混合：

```text
历史 Feature / Daily Bar
+
当前 Position
+
未来 Trade
+
未来 Decision
+
未来 Review
+
当前 Market Regime
+
当前 Realtime Quote
```

这是非常危险的“混合时点 Context”。

---

# 13. R4-P0-001 推荐修复方案

这里不要做半套。

有两个可选方案。

---

## 方案 A：Realtime Decision Context 明确 CURRENT_ONLY（推荐先做）

`/stocks/{code}/decision-context`

和：

`/portfolio/{code}/decision-context`

本来就是回答：

```text
现在能买吗？
现在卖不卖？
```

那么最简单、最安全：

```text
它只支持 CURRENT / NOW
```

如果用户传：

```text
显著早于当前时间的 as_of
```

直接：

```text
400 / CURRENT_ONLY_CONTEXT
```

历史判断必须走：

```text
immutable ContextPack
Deterministic Replay
Historical Review
```

这样边界最干净。

---

## 方案 B：真正实现 PIT Context

若要保留历史 `as_of`：

必须全部重建：

```text
Decision.as_of <= requested as_of

EntryPlan.effective_from <= requested as_of

Trade.trade_time <= requested as_of

Opening / Correction / Adjustment
按 requested as_of 重放 Position

PositionReview.as_of <= requested as_of

MarketRegime.as_of/known_at <= requested as_of

Evidence.known_at <= requested as_of

Feature/Bar known_at <= requested as_of
```

并且：

```text
历史 as_of 禁止调用“当前实时 Quote”
```

这明显工作量大于方案 A。

因此 V3 当前阶段推荐：

> **Realtime Context = CURRENT_ONLY**
>
> **Historical = ContextPack / Replay**

---

# 14. R4-P1-002：盘中常驻 Attention Loop 仍会用 stale/suspended Quote 触发 Stop/Target

第三轮 R3-07 已经修好了：

```text
用户主动 GET Position Decision Context
```

时：

```text
stale realtime quote
→ stop_hit=None
→ target_hit=None
```

这很好。

但 Runtime 另一条路径没有同步。

---

## 14.1 当前 resident loop

`IntradayTriggerLoop.evaluate_once()`：

```python
quote = await quote_service.get_quote_snapshot(...)

evaluation = await engine.evaluate_entry_plan_levels(
    quote=quote,
    ...
)
```

没有检查：

```text
quote.stale
quote.suspended
quote.quality
```

---

## 14.2 Attention Engine

`evaluate_entry_plan_levels()`：

```python
last_price = quote.last_price

if last_price <= stop:
    STOP_HIT

if last_price >= target:
    TARGET_HIT
```

同样完全没有 stale/suspended Gate。

所以：

```text
stale quote + old price <= stop
```

仍可能创建：

```text
CRITICAL STOP_HIT
```

---

## 14.3 修复建议

进入 plan price trigger 前：

```text
if quote.suspended:
    skip price trigger

if quote.stale:
    skip price trigger

if quote.quality not reliable:
    skip price trigger
```

同时可以生成：

```text
DATA_QUALITY_DEGRADED
```

但绝不能生成确定性：

```text
STOP_HIT
TARGET_HIT
ENTRY_TRIGGER_MET
```

---

# 15. R4-P1-003：Intraday Fast Lane Scanner/Overlay 尚未真正接生产 Runtime

这点非常重要，因为它决定你的系统究竟是：

> “有实时行情接口”

还是：

> “真正的实时选股系统”。

当前代码里已经存在：

```text
IntradayOverlayService
IntradayScannerService
ActiveIntradayUniverseService
```

Attention Engine 也已经有：

```text
record_intraday_anomalies()
record_new_evidence()
record_data_quality()
```

但代码搜索没有发现生产 Runtime 调用这些服务。

---

## 15.1 MCP 甚至明确写着尚未接线

当前：

```python
v3_scan_opportunities(mode="INTRADAY")
```

返回：

```json
{
  "status": "NOT_AVAILABLE",
  "detail": "intraday scan pipeline is not wired yet"
}
```

这是代码自己对当前能力的诚实说明。

---

## 15.2 当前常驻盘中 Loop 实际只做什么？

只做：

```text
读取已有带 stop/target 的 EntryPlan
↓
拉一只股票 Quote
↓
STOP / TARGET 触发
↓
AttentionEvent
```

没有：

```text
全市场 Quote
↓
Intraday Overlay
↓
Lightweight Scanner
↓
IntradayAttentionCandidate
↓
Active Intraday Universe
↓
Deep Pool
↓
Intraday Opportunity
```

因此设计中的：

```text
盘中新异常机会
```

尚未形成完整生产链。

---

# 16. R4-P1-004：Entry Decision Context 尚不够完整

正式实时集成设计期望：

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

当前 `ReadEntryDecisionContextService` 实际只返回：

```text
decision
entry_plan
plan_payload
quote
structure
readiness
evaluated_triggers
evaluated_cancels
```

缺少：

```text
Market Regime
EOD Feature
Recall
Raw Opportunity
ActionCandidate
EntryAssessment
Fundamental
Evidence
Attention
完整 Data Quality
```

---

## 16.1 为什么这会影响我以后“现在能买吗”的判断

如果我只拿：

```text
Quote
+ 周/日/60/15/5
+ EntryPlan
```

我可以做技术执行判断。

但你要求的是：

```text
位置
趋势
资金
行业
基本面
催化
Evidence
市场环境
相对强度
反方证据
失效条件
```

这些仍要我额外调用多个 API 拼起来。

聚合 Decision Context 的目标本来就是：

> **一次给 AI 足够完整、同一 as_of 的事实包。**

所以这里仍然是 Product Closure 的重要缺口。

---

# 17. R4-P1-005：Public Evidence / Market Review 权限语义仍偏宽

当前 public allowlist 包含：

```python
"/evidence/"
```

作为任意 prefix：

```text
/evidence/{subject_type}/{subject_id}
```

而 `subject_type` 不是 Public Security-only 固定模板。

因此今天可能没泄漏，但将来如果增加：

```text
POSITION
ACCOUNT
PRIVATE_CASE
```

等 Evidence subject，默认也会进入 Public Prefix。

---

## 17.1 建议

不要：

```text
/evidence/* 全公开
```

改为：

```text
明确 Public Subject Type Allowlist
```

例如：

```text
SECURITY
PUBLIC_INDUSTRY
PUBLIC_POLICY
```

其它全部要求认证。

---

## 17.2 `/market-reviews` 也值得重新判断

它当前在 Public Market READ Exact。

但 MarketReview 属于 AI Result/Review 语义，可能包含：

```text
agent_identity
context_pack_id/hash
evidence_ids
AI payload
```

它不是单纯：

```text
上证指数今天涨了多少
```

建议明确：

### 若希望公开
写进设计：

```text
MarketReview = Public AI Research Output
```

### 若只供你/ChatGPT
改为：

```text
MARKET_READ token
```

不要模糊。

---

# 18. R4-P1-006：实时 Bar 的来源/质量信息仍不足

Quote 现在能返回：

```text
source
upstream_source
quality
stale
confidence
```

这很好。

但 Kline/structure：

```python
IntradayBarsResult.source = "legacy-provider"
```

并没有告诉消费方：

```text
60m 实际 Eastmoney
15m 实际 Tencent fallback
week 实际 aggregate:day:tencent
```

---

## 18.1 建议每周期返回

```text
period
status
source
upstream_source
known_at
stale
quality
confidence
fallback_used
provisional
```

这样我以后做盘中分析时才知道：

> 数据是正常主源，还是备用源，还是缓存/聚合。

这会直接提升“数据能不能信”的判断能力。

---

# 19. R4-P1/P2-007：mode=V2 下是否应继续生成 V3 Recall，需要产品裁决

这一项不直接判 Bug。

现在：

```text
V2:
data pipeline RUN
full-recall SKIP
```

如果你的目标是：

> V3 尚未成为正式 live strategy，所以 V3 Recall 不运行

成立。

但你的实际计划是：

> 把 V3 全做完，然后我每天用 V3 选股，看它到底怎么样。

那么很可能希望：

```text
Live mode 仍为 V2
但 V3 Recall 作为 Shadow/Research 每天照跑
```

建议正式增加区分：

```text
RELEASE_MODE
vs
RESEARCH_SHADOW_ENABLED
```

不要为了“看 V3 效果”被迫把正式 Strategy Release 切 V3。

---

# 20. R4-P2-008：Intraday Worker 缺运行状态与优雅收口

`run_resident()`：

```python
intraday_task = create_task(...)

finally:
    intraday_task.cancel()
```

没有：

```text
await cancelled task
database.close()
provider_manager.close()
```

通常进程退出 OS 会回收，但不够干净。

---

## 20.1 更重要：循环失败被静默吞掉

```python
try:
    await evaluate_once()
except Exception:
    pass
```

如果盘中 Worker 连续失败 2 小时：

```text
没有 heartbeat
没有 last_success
没有 last_error
没有健康事件
```

Dashboard 可能还看起来“系统在线”。

建议至少保存：

```text
last_success_at
last_error_at
last_error_type
plan_count
evaluated_count
quote_failed
engine_failed
provider_health
```

这样真正交易时才知道 Fast Lane 是否活着。

---

# 21. R4-P2-009：holding_sessions 使用服务器本地 timezone

当前：

```python
end = as_of.astimezone().date()
```

它用服务器本地时区，而不是明确：

```text
Asia/Shanghai
```

如果服务器是 UTC，在北京时间凌晨附近可能出现交易日计算偏一天。

建议：

```python
end = as_of.astimezone(SHANGHAI).date()
```

这是小 Bug，但持仓天数/Time Efficiency 都依赖它。

---

# 22. R4-P2-010：Remote MCP 尚未形成完整“扫描 → 比较 → 深度”链

当前 MCP 已有：

```text
market_overview
market_intraday_status
pipeline_eod_latest
scan_opportunities
stock_decision_context
stock_intraday_structure
watchlist
attention_events
position_context
position_decision_context
```

但：

```text
candidate_comparison
```

没有明确工具。

并且：

```text
scan_opportunities(INTRADAY)
```

明确 NOT_AVAILABLE。

所以按正式设计期望：

```text
市场扫描
→ Candidate Comparison
→ TopK Deep Context
→ Entry/Position Review
```

MCP 目前还没完整闭环。

HTTP API 可以补救，但正式“ChatGPT 工具入口”仍差最后几块。

---

# 23. Dashboard 目前仍是“特征看板”，还不是最终选股看板

当前 Dashboard 主要展示：

```text
市场状态
Regime
EOD Pipeline
Attention
全市场 Feature
```

但还没有把：

```text
Raw Opportunities
Recall Channel
Candidate Comparison
为什么入选
多周期摘要
Data Quality
AI Review Status
```

做成首页主选股视图。

这一项先不要求现在开发，因为用户已经明确：

> 先修完 V3 核心问题，再做 AI Access/看板增强。

所以本报告将它归为：

```text
PRODUCT CLOSURE / UI
```

而不是阻塞当前代码正确性的 P0。

---

# 24. 仍然存在的历史未完全关闭项

## Performance

目前自动 Mature：

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

所以：

```text
4 / 7
```

---

## Replay

当前已真正重算：

```text
Feature
Regime
Context Evidence Selection
```

仍显式排除：

```text
Recall Channels
Raw Opportunity / Comparison
Entry Trigger
```

因此仍是：

```text
Partial Deterministic Replay
```

而不是完整 Strategy Replay。

---

## Industry

可靠版本化行业事实仍未完成：

```text
relative_industry_strength
industry classification history
industry rotation
```

保持 UNKNOWN 是正确的，不能猜。

---

## Audit

request_id / Principal 已大幅补齐。

但完整：

```text
所有关键写动作 Audit Matrix
```

仍建议 Final Gate 再跑一次。

---

# 25. 第四轮建议整改顺序

不要再开新功能。

按下面顺序收：

```text
R4-01  Point-in-Time / CURRENT_ONLY Decision Context
↓
R4-02  stale/suspended Quote 不得触发 Attention Hit
↓
R4-03  Intraday Overlay / Scanner / Active Pool 真接 Runtime
↓
R4-04  Entry Decision Context 聚合完整
↓
R4-05  Public Evidence / MarketReview 权限语义收口
↓
R4-06  Realtime Kline provenance + MarketDataService 降级能力
↓
R4-07  MCP candidate comparison + INTRADAY scan
↓
R4-08  Intraday Worker heartbeat / graceful shutdown / Shanghai timezone
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

# 26. R4-01 必须新增的测试

至少新增：

## 26.1 Future Quote Guard

```text
requested as_of = T1
quote.known_at = T2
T2 > T1
```

必须：

```text
reject / UNKNOWN / CURRENT_ONLY
```

绝不能：

```text
stale=false + use price
```

---

## 26.2 Future Decision Guard

```text
requested as_of = T1

Decision A.as_of = T0
Decision B.as_of = T2
T2 > T1
```

返回必须是：

```text
Decision A
```

或 CURRENT_ONLY 直接拒绝历史请求。

---

## 26.3 Future Trade Guard

历史 Position Context：

```text
Trade.trade_time > requested as_of
```

绝不能影响：

```text
quantity
average_cost
PnL
Position Review
```

---

## 26.4 Regime Guard

```text
regime.known_at > requested as_of
```

绝不能进入历史 Context。

---

# 27. R4-02 必须新增的测试

构造：

```text
quote.stale = true
last_price < stop
```

Runtime：

```text
IntradayTriggerLoop.evaluate_once()
```

必须：

```text
STOP_HIT not created
```

另测：

```text
suspended = true
last_price >= target
```

必须：

```text
TARGET_HIT not created
```

允许：

```text
DATA_QUALITY_DEGRADED
```

---

# 28. R4-03 Intraday Fast Lane 的最小验收

交易时段必须真实出现：

```text
全市场 Quote
→ Overlay
→ Lightweight Scanner
→ IntradayAttentionCandidate
→ Active Intraday Universe
→ 重点池 DeepMarketData
→ Attention
```

MCP：

```text
scan_opportunities(mode=INTRADAY)
```

不能再返回：

```text
NOT_AVAILABLE
```

---

# 29. R4-04 Entry Context 最低完整度

最终：

```http
GET /api/v3/stocks/{code}/decision-context
```

至少应一次返回：

```text
security
market_regime
latest_quote
feature_eod
intraday_overlay
weekly
daily
60m
15m
5m
latest_recall
latest_raw_opportunity
latest_action
latest_entry_assessment
latest_decision
latest_entry_plan
levels
evidence
fundamental（可靠时）
attention_events
data_quality
```

缺什么可以：

```text
UNKNOWN / NOT_AVAILABLE
```

但字段和 reason 要明确。

---

# 30. 部署前 Final Runtime Gate

修完以后不要只跑 unit test。

需要：

## A. 静态/单元

```text
compileall
tests/v3
OpenAPI duplicate
Auth matrix
```

## B. PostgreSQL

```text
Migration Head
Repository
Correction/Openings
Performance
Replay
Scheduler
ExpectedRun
Shadow
```

## C. 实际交易时间

至少验证一个真实交易时段：

```text
09:30–10:30 或 13:00–14:30
```

检查：

```text
Quote freshness
Quote provider
fallback
5m/15m/60m
provisional day/week
Attention
Intraday scanner
Candidate
Entry Context
Position Context
```

## D. PIT Gate

随机抽历史 as_of：

```text
任何返回事实必须满足 known_at <= as_of
```

当前 realtime CURRENT_ONLY API 则必须拒绝历史 as_of。

## E. AI 使用链

完整走一次：

```text
Market Overview
→ Raw Opportunity
→ Candidate Comparison
→ TopK Decision Context
→ ChatGPT 横向比较
→ WAIT / READY / NO_BUY
```

有持仓再走：

```text
Position Context
→ HOLD / ADD / REDUCE / EXIT
```

---

# 31. 当前完成度判断

与最早代码相比，现在已经完成大量基础与治理能力：

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
API Contract
ExpectedRun
```

所以已经不是“V3 还没做”。

但是用户真正要的是：

> **每天把系统打开，ChatGPT 能读到真实、完整、新鲜、同一时点的数据，然后可靠地选股与判断买卖。**

按这个 Product Closure 标准，目前仍有：

```text
PIT
Intraday scanner runtime
Decision Context aggregation
data provenance
MCP chain
```

几个关键缺口。

因此我的状态判断是：

```text
TECHNICAL IMPLEMENTATION：很高
PRODUCT CLOSURE：尚未完成
LIVE TRADING ANALYSIS READY：暂不建议最终放行
```

粗略理解：

> **代码层已经进入约最后 15%～20% 的“横向接线和真实性验收”阶段。**

这个百分比只是工程阶段感知，不代表投资策略胜率。

---

# 32. 最终建议

现在不要：

```text
接 GLM
做强势启动
做 AI Access
大改 Dashboard
扩新策略
```

继续只做 V3 收口。

下一版只处理：

```text
R4-01 → R4-08
```

然后再给完整 ZIP。

第五轮我建议不再继续无限审代码，而是：

```text
代码终验
+
部署验收
+
真实交易时段事实验收
```

如果这三项通过，就正式进入你真正想做的阶段：

> **每天我直接使用你的行情底座，跑 V3 低位埋伏真实选股，持续记录结果，用 Performance / Replay 检验到底有没有效果。**
