# gpt-market V3 第二轮整改复验报告（Claude Code 修复版）

> 复验日期：2026-09-03  
> 复验对象：`1eb3dc3d-6242-4f50-a46e-335b312ff00d.zip`  
> 验收基线：此前出具的《gpt-market V3 问题详细分析（2026-09-01）》与《gpt-market V3 整改方案建议（2026-09-01）》  
> 判定原则：不以 Claude Code 文档中的“DONE/已部署/已验收”作为完成依据；必须回到实际代码、调用链、调度、API、测试和运行契约逐项确认。

---

## 1. 总结论

本轮整改**效果非常明显，而且大量问题是真修复，不是只补文档或字段**。与 2026-09-01 版本相比，新代码新增了认证、统一错误结构、Orchestrator、指数基准、真实周线、多周期分钟事实、Position Context、Performance Mature、Deterministic Replay、Shadow Executor、Release Resolver、Audit Helper、OCR Pipeline、备份/恢复脚本和 Product E2E 等大量实质能力。

但是，**还没有完全达到此前整改文档的最终要求，也不应宣称 V3 Product Closure 已经 100% 完成**。

本次严格复验建议给出：

| 维度 | 2026-09-01 评估 | 本轮复验 |
|---|---:|---:|
| 技术架构 / 数据基础 | 82%–88% | **90%–93%** |
| 功能语义闭环 | 65%–70% | **78%–83%** |
| 每日真实可用决策闭环 | 50%–55% | **约 70%–75%** |
| 生产 / Release Readiness | 35%–45% | **约 60%–70%** |
| 对此前整改意见的总体落实度 | — | **约 72%–78%** |

最大剩余风险不是“基础模块没写”，而是：

1. **READ 鉴权边界过宽，公开 GET 不仅包含市场事实，还包含 Watchlist/Decision/Performance/Strategy/Task 等私有或控制面信息**；
2. **正式仓库的 Docker Compose/Deployment 仍指向旧 Phase2 Scheduler，和文档声称的生产新调度器不一致**；
3. **Scheduler Catch-up 有两个重要语义 Bug：完成标记仍看 `features` 而不是终端 `full-recall`，历史补跑使用 `as_of=now` 且 market-data handler 没真正按 pending date 回放**；
4. **Position “现在卖不卖” Context 仍使用 FEATURE_LKG 收盘价，而且 API/MCP 实际构造时没有注入 DeepMarketData/Calendar，因此 60m/15m/5m 和 holding sessions 会退化为 UNKNOWN**；
5. **Performance Mature 目前自动生成的正式归因仍主要只有 `SELECTION`，没有把原整改方案要求的七类能力全部自动成熟**；
6. **Replay 已经从“只做 leakage check”升级为真实 Feature 重算，但还没有完整重放 Regime/Recall/Comparison/Context/Entry 等服务器确定性链**；
7. **Shadow Executor 和 Release Resolver 已写出来，但生产 Runtime 没有真正系统性消费它们来选择/执行策略；Resolver 当前主要进入报告**；
8. **Audit 覆盖显著提升，但 Principal 绑定仍没覆盖 `created_by/corrected_by` 等身份字段，API 也基本没有把 request_id 传入各 Application Service**；
9. **GET 构建并持久化 Comparison/Context Pack 的副作用问题仍未关闭**。

因此当前 `mode=V2` 继续保持是正确的。**不建议此时激活正式 V3 Strategy。**

---

## 2. 本轮代码与测试证据

### 2.1 仓库规模变化

旧评审 ZIP 约 270 个源码/文档文件；本轮代码新增了大量实质模块，包括：

- `app/v3/security.py`
- `app/v3/errors.py`
- `app/v3/application/audit_helper.py`
- `app/v3/jobs/orchestrator.py`
- `scripts/v3_scheduler.py`
- `app/v3/application/calculate_index_benchmark_return.py`
- `app/v3/application/ingest_index_benchmarks.py`
- `app/v3/application/deep_market_data.py`
- `app/v3/application/intraday_market_data.py`
- `app/v3/application/intraday_structure_snapshot.py`
- `app/v3/application/read_position_context.py`
- `app/v3/application/read_position_decision_context.py`
- `app/v3/application/mature_performance.py`
- `app/v3/application/deterministic_replay.py`
- `app/v3/application/execute_regression_case.py`
- `app/v3/application/shadow_executor.py`
- `app/v3/application/release_resolver.py`
- `app/v3/application/ocr.py`
- `app/v3/application/ocr_pipeline.py`
- migrations `0012`–`0017`
- 大量新增 V3 测试及 Product E2E。

这证明此次修复不是只改文档。

### 2.2 本审计环境实际测试

执行：

```text
python -m compileall -q app scripts migrations tests
```

结果：**PASS**。

在排除依赖 `app.main/FastMCP` 的测试后，执行 V3 测试：

```text
301 passed
91 skipped
4 failed
4 warnings
```

4 个失败中：

- 3 个由当前审计环境没有安装 `asyncpg` 引起；
- 1 个由上传 ZIP 在本环境解压后中文文件名表示为 `#Uxxxx`、而测试硬编码中文文件名引起；
- 另有 OpenAPI Duplicate Operation ID Warning，属于真实代码质量问题，见 NEW-API-001。

因此这 4 个失败不直接判定为业务回归，但也不能用本次沙箱测试替代服务器真实 PostgreSQL 验收。

---

# 3. 原问题逐项复验

## 3.1 Security

### SEC-001 `/api/v3` 无认证/权限 —— **部分关闭，仍有高风险缺口**

#### 已修复

`app/main.py:41-49` 已对 V3 安装 `V3AuthMiddleware`。

`app/v3/security.py` 已存在 Scope：

- `MARKET_READ`
- `PORTFOLIO_READ`
- `V3_WRITE`
- `STRATEGY_ADMIN`

非 GET `/api/v3` 默认要求 `V3_WRITE`；Strategy Activate/Rollback 要求 `STRATEGY_ADMIN`。这比旧版明显正确。

#### 未关闭的问题

`V3AuthPolicy.required_scope()` 逻辑为：

```text
GET/HEAD:
  如果 path == /api/v3/portfolio 或 /api/v3/portfolio/* → PORTFOLIO_READ
  其它所有 GET → public_market_read=True 时完全公开
```

见：`app/v3/security.py:57-71`。

这意味着当前“公开 READ”并不等于“公开市场事实 READ”，而是**除了 `/portfolio` 前缀以外几乎所有 GET 都公开**。

当前会公开的非纯市场事实/敏感或控制面 GET 包括但不限于：

- `/api/v3/portfolio-preferences`（别名绕开 `/portfolio/` 前缀）
- `/api/v3/entry-plans/{id}/versions`
- `/api/v3/watchlist/changes`
- `/api/v3/watchlist`
- `/api/v3/decisions`
- `/api/v3/reviews`
- `/api/v3/performance`
- `/api/v3/task-runs`
- `/api/v3/task-context/{profile}`
- `/api/v3/strategies`
- `/api/v3/strategies/experiments/{id}`
- `/api/v3/strategies/releases/{environment}`
- `/api/v3/release/resolution`
- `/api/v3/stocks/{security_id}/decision-pipeline`

这与用户当前明确策略：**“公开 READ 仅市场事实；Portfolio READ 和全部 WRITE 需 Token”** 不一致。

#### 复验结论

WRITE 安全边界大幅改善，但 READ Scope 设计仍需整改。最安全的实现不是继续维护“Portfolio path 黑名单”，而是改成**明确的 Public Market READ Allowlist**。

---

### SEC-002 Human/confirmed_by/actor_id 可伪造 —— **部分关闭**

#### 已修复

`bind_v3_principal()` 已覆盖：

- `actor_id`
- `actor_type`
- `confirmed_by`

见 `app/v3/security.py:27-46`。

Trade Confirm、Opening、Adjustment、Correction、Reconciliation 等多个 API 也已经调用 `_bind_principal()`。

#### 未关闭

绑定函数没有覆盖：

- `created_by`
- `corrected_by`

并且部分关键写接口本身没有 Request/Principal 绑定，例如：

- Decision Correction 的 `corrected_by`；
- Strategy Version/Guardrail/Experiment 等部分 `created_by`；
- 部分 Action/Entry/Performance 写接口也没有把 authenticated principal 传入业务层。

因此已认证客户端仍可能伪造“是谁创建/修正”的审计身份字段。

#### 结论

**部分关闭。** 服务端 Principal 必须成为所有 actor/creator/corrector/confirmer 的唯一可信来源。

---

## 3.2 Operations / Scheduler

### OPS-001 只有 Phase2 Worker —— **部分关闭，代码已有正式 Orchestrator，但仓库部署仍可能回退**

#### 已修复

新增：

- `app/v3/jobs/orchestrator.py`
- `scripts/v3_scheduler.py`
- Orchestrator Job Run 数据模型/迁移；
- main + maintenance 链；
- Corporate Action Match / Projection Verify / Performance Mature / Recall Observation Mature 等维护任务；
- Evidence + Full Recall 已进入新的 Scheduler 主链。

这是实质修复。

#### 关键未关闭 1：Docker Compose 仍启动旧 Worker

`docker-compose.yml:61` 仍然是：

```text
python -m scripts.v3_phase2_scheduler
```

而不是：

```text
python -m scripts.v3_scheduler
```

文档声称生产已经手工切到新 Scheduler，但仓库 IaC 不是当前真实部署事实。

结果是：

> 新机器 `docker compose up` / 灾备恢复 / 换服务器，可能重新部署成旧 Phase2 Worker。

这违反“仓库应成为可恢复 Source of Truth”的要求。

#### 关键未关闭 2：Expected Run Update 仍未进入正式调度

`RegisterExpectedTaskService` 仍存在，但 Scheduler/Job Graph 中未看到正式 Expected Run Registry/Update Job。

原详细设计后台任务要求该链路；目前仍需手工/其它入口完成。

---

### NEW-OPS-001：Scheduler Catch-up 终端完成标记错误 —— **P1 新问题**

`scripts/v3_scheduler.py:375-378`：

```text
_latest_main_success_key()
→ latest_succeeded_idempotency_key("features")
```

注释也仍写“主链终端 Job（features）”。

但当前主链已经扩展为：

```text
market-data
→ index-benchmarks
→ features
→ evidence-increment
→ full-recall
```

因此如果 `features` 成功，而 evidence/full-recall 失败，下一次 Scheduler 可能把该交易日视为已经追平，不再补跑后半链。

正确完成标记应该基于真实终端 Job（当前至少是 `full-recall`），或者基于 Orchestrator Run 的完整 Main-chain 状态。

---

### NEW-OPS-002：历史 Catch-up 使用 `as_of=now`，且 market-data handler 不真正按 pending date 运行 —— **P1 新问题**

`scripts/v3_scheduler.py:423-425`：

```text
main.execute(trade_date=pending_date, as_of=now)
```

多个历史 pending date 都共用当前 `now`。

同时 Market Data Job 仍主要调用“当前市场增量/当前 Provider”路径，并没有证明使用 `pending_date` 精确重建历史点时输入。

这会导致：

- 旧交易日 Catch-up 的 Job ID 是旧日期；
- 但输入事实可能已经是今天能看到的最新数据；
- 在 Point-in-Time 语义上存在“补跑日期”和“事实 known_as_of”错位。

必须明确两种模式：

1. **Operational catch-up**：只保证现在把缺失事实补齐，不声称历史 Point-in-Time；
2. **Historical point-in-time backfill/replay**：必须使用对应交易日的 revision/known_at 边界。

不能混用。

---

## 3.3 Market Data / Multi-timeframe

### DAT-001 5m/15m/60m 深度周期 —— **能力层基本关闭，但在 Position 主路径没有真正接通**

#### 已修复能力

新增：

- `IntradayMarketDataService`
- `IntradayStructureSnapshotService`
- `DeepMarketDataService`

能够返回：

- 60m
- 15m
- 5m

并明确 source/known_at/stale/precision LIMITED 等语义。

因此从“有没有 V3 深度分钟事实能力”来看，**已基本关闭**。

#### 但对最终产品仍存在 CTX 接线问题

真正 Position Context 的 API/MCP 构造时没有注入 Deep Service，见 CTX-001。

---

### DAT-002 Weekly 不计算/字段不一致 —— **关闭**

已经统一 `weekly_trend_state`，Feature Service 使用真实 Weekly Revision 计算周趋势。

并实现：

```text
weekly=DOWN + daily=UP
→ WEEKLY_DOWN_DAILY_BOUNCE
→ “下降趋势中的反弹”
```

这是用户明确要求的多周期核心规则，修复符合整改要求。

---

### DAT-003 相对指数/相对行业 —— **部分关闭**

#### 指数部分：关闭

已新增 Index Benchmark Fact Chain，并在 `RunFullMarketFeaturesService` 注入指数 20 日收益，`relative_index_strength` 可真实计算。

#### 行业部分：未关闭

`industry_return_20d` 仍没有生产输入，`relative_industry_strength` 仍会是 null。

当前文档说明因为没有可靠行业分类源而选择“不猜”，这是正确的诚实策略；但从原整改目标看，只能判“指数完成、行业未完成”。

---

### DAT-004 Market Regime 完整度 —— **部分关闭**

已修复：

- Index state；
- Breadth；
- Turnover coverage；
- stale ratio/freshness 语义。

仍固定 UNKNOWN：

- limit_structure；
- size_style；
- growth_value_style；
- industry_rotation。

见 `app/v3/application/calculate_market_regime.py:93-96`。

因此不能判完全完成。

---

## 3.4 Context / Position

### CTX-002 Phase6 Portfolio 永久 UNKNOWN —— **关闭**

Context Builder 现在能够对 POSITION Subject 读取真实 Position Projection，不再固定 `not_available_in_phase6`。

SECURITY Context 无账户时明确 NOT_APPLICABLE，方向合理。

---

### CTX-001 Full Position Context —— **部分关闭，结构大幅补齐，但主 API/MCP 仍没有真正接 Deep/Calendar/Realtime**

#### 已补齐

`ReadPositionContextService` 已包含：

- quantity/cost_basis/average_cost/realized_pnl；
- market price / unrealized pnl；
- holding；
- trades；
- original/current/trade-bound EntryPlan；
- support/resistance/stop/target/invalidation；
- weekly/daily/60m/15m/5m；
- Evidence；
- Market Regime；
- risk；
- time_efficiency；
- Position Review；
- data_quality。

这比旧版本进步很大。

#### 关键接线问题 1：Decision Context 实际没注入 Deep/Calendar

`app/api/v3.py:694`：

```text
ReadPositionContextService(_uow)
```

没有：

- `calendar`
- `deep_market_data`

因此 `read_position_context.py` 会返回：

```text
holding_sessions = UNKNOWN(CALENDAR_NOT_BOUND)
60m/15m/5m = UNKNOWN(DEEP_MARKET_DATA_NOT_BOUND)
```

`PortfolioWriteService.read_position_context()` 虽然增加了可选 deep/calendar 参数，但 API `/portfolio/{code}/context` 也没有传。

MCP 的 Position Context 调用也存在同样问题。

因此“一个 Position Context 就足够 ChatGPT 完成持仓 Review”的整改目标仍未真正闭环。

#### 关键接线问题 2：‘现在卖不卖’使用 FEATURE_LKG 收盘价，不是当前实时价

`ReadPositionContextService._price_section()` 明确：

```text
price_source = FEATURE_LKG
latest_price = feature.close
```

`ReadPositionDecisionContextService` 又直接用这个 `latest_price` 判断：

```text
stop_hit = last_price <= stop
target_hit = last_price >= target
```

所以 `/portfolio/{code}/decision-context` 虽然文档声称回答“现在卖不卖”，其 Stop/Target 客观判断可能是基于上一轮 Feature 的收盘/LKG 价格，而不是当前 Quote。

这对盘中持仓决策是 P1 正确性风险。

**建议：Position 决策 Context 的 market.latest_price 必须绑定实时 Quote/RT Overlay；EOD Feature close 只能作为 EOD/LKG 辅助事实，并同时返回两个时间戳。**

---

## 3.5 Performance / Replay

### PF-001 Performance Mature Engine —— **部分关闭**

#### 已修复

新增 `MaturePerformanceService`，能够从真实 Decision + Bars + Benchmark + EntryPlan + Trade 事实自动计算：

- T+1/3/5/10/20；
- raw return；
- excess return；
- MFE/MAE；
- Target/Stop hit + first hit；
- direction correctness；
- actual_trade；
- entry_triggered。

这是实质性的重大修复。

#### 未完成

代码文件自身明确写：

```text
“七类能力分开，本引擎产出 SELECTION（决策级）事实”
```

当前自动 Mature 仍主要只生成 `PerformanceAbility.SELECTION`。

原整改要求的七类自动归因：

- Selection
- Initial Entry
- User Execution
- Add
- Reduce
- Final Exit
- Risk Control

并没有全部通过系统自动成熟引擎生成。

因此 PF-001 只能判部分关闭。

#### 新鲁棒性问题

`mature_performance.py:73-76`：

```python
baseline_index = max(index for ... if bar.bar_time <= decision["as_of"])
```

没有 default/空集合处理。

如果某个 Decision 找得到 Revision，但 Revision 中没有任何 bar <= decision.as_of，该候选会直接抛 `ValueError`，可能让整批 Maintenance Job 失败，而不是将该项标为 PENDING/SKIPPED。

建议改成安全查找并记录 reason。

---

### PF-002 Replay —— **部分关闭，完成度大幅提升**

旧版本确实只是 Reference/Leakage Check。

新 `DeterministicReplayService` 已经能够：

- 先做 point-in-time gate；
- 读取 pinned Bar Revision；
- 重新计算 Feature；
- 与保存的 Feature 做字段级比较；
- 对 Immutable AI Result 做结果回放；
- 明确 AI Boundary：`SERVER_HAS_NO_MODEL_API`。

这是正确方向。

但 `_deterministic_layer()` 当前主要重算 Feature，明确排除了：

```text
relative_index_strength
relative_industry_strength
weekly_trend_state
stale
coverage
```

也没有完整重放：

```text
Market Regime
→ Recall Channels
→ Raw Opportunity
→ Comparison Pack
→ Context Pack
→ Entry Trigger/Cancel
```

因此它已经不是“只有 leakage check”，但距离原整改方案中的“Server Deterministic Replay”仍有明显缺口。

建议判：**约 65%–70% 完成**。

---

## 3.6 Strategy / Shadow / Release

### STR-001 Shadow/A-B Runtime —— **部分关闭**

`ShadowExecutorService` 已真实执行：

```text
same subject/as_of
→ control executor
→ treatment executor
→ hash/diff/latency/error
→ ShadowObservation
```

并真正消费 A/B assignment。这是实质修复。

但是生产代码中没有看到 Scheduler/API/正式 Pipeline 自动调用 `ShadowExecutorService`，目前主要出现在：

- 单元测试；
- PostgreSQL 测试；
- Product E2E；
- 人工生产验证脚本/文档。

也没有仓库级正式 Executor Registry 将具体 Strategy Version 持续映射到真正执行器。

所以当前是：

> Shadow Engine 已有；Production Shadow Runtime 尚未自动接入日常链路。

不能判完全关闭。

---

### STR-002 Release State 控制实际 Runtime —— **仍为部分关闭**

`ReleaseResolver` 已正确实现：

- V3 flag emergency fallback；
- DB release state 读取；
- 缺版本时诚实回 V2；
- 每次 resolve，不缓存。

但在 `scripts/v3_scheduler.py` 中，当前行为是：

```text
resolve release
→ 写进 report["release_resolution"]
→ 后续仍照常执行同一套 main/maintenance orchestrator
```

没有根据 `effective_mode/strategy_version/configuration` 真正选择：

- Feature/Recall version；
- Strategy executor；
- Control/Treatment；
- V2/V3 真实业务执行路径。

因此目前更多是“Control Plane Resolver + 运行报告记录”，还不是完整的 Runtime Strategy Resolver。

**不要因为 GET `/release/resolution` 返回 V3 就认为 V3 Runtime 真切过去。**

---

## 3.7 Portfolio / Ledger

### POR-001 多次 Correction 链 —— **关闭**

新 Migration/Repository 已改成：

```text
Original Trade
→ correction #1 patch
→ correction #2 cumulative patch
→ ...
```

并增加 sequence、pre/effective hash、REVERSE terminal、并发保护和回归测试。

达到原整改目标。

### POR-002 Opening 基准边界 —— **关闭**

已按 baseline time/sequence 过滤前序 Trade，并有 pre/same/post/multiple Opening 测试。

达到整改要求。

### POR-003 cost_method 假配置 —— **关闭**

当前收紧为仅支持 `WEIGHTED_AVERAGE`，与真实算法一致。合理。

### POR-004 Execution Deviation —— **基本关闭**

已补 Price/Time/Quantity/Trigger/Cancel 等结构化偏差与 UNKNOWN 语义。

### POR-005 Corporate Action Match + Projection Verify —— **代码关闭，但部署可恢复性仍受 OPS-001 影响**

两个正式 Job 已实现，也进入新 Maintenance Orchestrator。

但仓库 `docker-compose.yml` 仍默认启动旧 Phase2 Scheduler，因此从“代码能力”看完成，从“仓库可重复部署”看仍部分关闭。

---

## 3.8 Audit

### AUD-001 统一 Audit Chain —— **部分关闭**

新增 `AuditRecorder` 并接入：

- Portfolio 多个关键写；
- Decision；
- Strategy 多个写；
- AI Import。

比旧版明显改善。

仍有三个问题：

1. `created_by/corrected_by` 身份仍可能来自请求体（见 SEC-002）；
2. Strategy Guardrail、Shadow Observation、Capacity、Health Event 等并非全部统一 Audit；
3. Application Service 已支持 `request_id` 的地方，`app/api/v3.py` 基本没有把 `request.state.v3_principal.request_id` 继续传下去，因此 AuditEvent 的 request_id 可能大量为 null。

所以仍不能判“统一审计链完全闭环”。

---

## 3.9 API

### API-001 缺 READ Contract —— **基本关闭**

已新增此前主要缺失的：

- Portfolio summary；
- Position reviews；
- Adjustments；
- Preferences；
- EntryPlan versions；
- Watchlist changes；
- Decisions；
- Reviews；
- Market Reviews；
- Performance；
- Data Quality。

`cases/similar` 未实现，但原整改方案允许在没有可靠相似度事实时进入 Product Backlog，因此不作为阻断。

### API-002 GET 有写副作用 —— **未关闭**

仍然存在：

- `GET /api/v3/candidates/comparison-pack` → Build + Publish Pack；
- `GET /api/v3/stocks/{code}/context-pack` → Build + Publish/Commit Context Pack。

没有完成原建议中的：

```text
POST build/create
GET read existing
```

该问题仍然保留。

### API-003 V3 Error Contract —— **关闭**

`app/v3/errors.py` 已建立统一：

```json
{
  "code": "...",
  "message": "...",
  "request_id": "...",
  "details": {},
  "retryable": false
}
```

并覆盖 Validation/404/409/Provider/Internal/Auth 等主要路径。

### API-004 Idempotency —— **基本关闭**

已新增较完整 Idempotency 测试和对象级去重机制，覆盖 Opening、Adjustment、Trade Confirm、Correction、Reconciliation、Strategy Version、Experiment/Health 等。

---

## 3.10 OCR / UI / Docs / Ops

### OCR-001 OCR Pipeline —— **MVP 级关闭**

已新增：

```text
base64 image
→ sha256/image store
→ Tesseract Adapter
→ field/confidence/region
→ Draft Preview
→ human correction/confirmation
```

且缺 Provider/字段时保持失败或 INCOMPLETE，不伪造。符合原整改方向。

#### 部署可恢复性问题

API 默认：

```text
V3_IMAGE_STORE_DIR=data/v3-images
```

Docker 容器为 `read_only: true`，而可写挂载是 `/data`。

文档声称生产手工设置了：

```text
V3_IMAGE_STORE_DIR=/data/v3-images
```

但 `.env.example/docker-compose.yml` 没有把这个写成标准部署配置。

所以新部署可能因为默认路径不可写导致 OCR 回归。应修复 IaC/default。

---

### DOC-001 文档状态漂移 —— **仍未完全关闭**

`docs/V3最终收口实施记录.md` 顶部任务表仍有：

- RC-03 TODO
- RC-05 TODO
- RC-06 TODO
- RC-07 TODO
- RC-08 TODO

但同一文件后半部分又记录这些已经开发/部署/生产验证。

`docs/工作状态.md` 顶部也记录多项已部署，但文件后部仍存在“下一步进入 RC-03”“未开始生产数据/备份”等旧段落。

这是典型的“Append-only 历史记录 + 当前状态没有明确分层”。

建议不要删历史，而是增加一个唯一的 **Current Truth / Current Status** 区块，并明确：

```text
历史段落只作为历史记录，不代表当前状态。
```

否则 Codex 以后还会读错。

---

### OPS-002 Production Data —— **部分关闭**

文档显示生产已经有：

- Universe；
- Bars；
- Feature；
- Regime；
- Index Benchmark。

但收口记录也曾明确：

```text
evidence/recall/comparison/context/attributions = 0
```

并解释为按需/AI 日程。

新 Scheduler 已加入 Evidence/Full Recall，但仅从 ZIP 无法独立证明当前生产已经形成每天稳定可消费的 Evidence/Recall/Context/Attribution 数据链。

因此代码准备度很高，但实际生产数据就绪仍需要以服务器实测为准。

---

### OPS-003 Backup / Restore Drill —— **代码和文档层基本关闭**

已有：

- backup script；
- restore drill；
- 保留策略；
- 文档记录生产 cron + PostgreSQL 17 restore 验证。

ZIP 无法独立验证服务器 cron 当前还在运行，但从仓库和记录看，这一项已达到整改要求。

---

### TEST-001 Product Closure E2E —— **基本关闭**

新增 `tests/v3/test_product_e2e_postgres.py`，覆盖约 20 步：

```text
Universe
→ Bars
→ Feature/Regime
→ Evidence
→ Recall
→ Comparison
→ Context
→ AI Result
→ Trade
→ Position
→ Review
→ Future Bars
→ Performance
→ Replay
→ Shadow
→ Audit
→ Idempotent rerun
```

这是很重要的实质提升。

但测试内 Shadow Executor 仍由 fixture 注入，并不证明生产 Scheduler 正常自动 Shadow；所以 Product E2E 不能覆盖 STR-001/STR-002 的剩余 Runtime 缺口。

---

### TEST-002 新机器/第三方复现 —— **基本关闭，但仍有小问题**

`scripts/test_suite.py`、requirements 和测试分层明显改善。

本审计环境不能安装/联网补 `fastmcp/asyncpg`，这是环境限制。

仍建议：

- 不要让测试依赖中文文件名在 ZIP 中的具体编码表达；
- OpenAPI alias 的 Duplicate Operation ID 修掉。

---

# 4. 新发现问题汇总

| ID | 等级 | 问题 | 影响 |
|---|---|---|---|
| NEW-SEC-001 | **P0/P1** | Public READ 规则过宽，除 `/portfolio/*` 外几乎所有 GET 都公开 | Watchlist/Decision/EntryPlan/Performance/Strategy/Task/Release 等私有或控制面数据可匿名读取 |
| NEW-SEC-002 | P1 | `created_by/corrected_by` 等没有绑定 Principal | 审计身份仍可被认证客户端伪造 |
| NEW-OPS-001 | **P1** | `docker-compose.yml` 仍启动 `v3_phase2_scheduler` | 新部署/灾备可能回退旧 Worker，生产与 Git 不一致 |
| NEW-OPS-002 | **P1** | Catch-up 完成标记仍取 `features`，不是 `full-recall` | 后半链失败后可能被误认为交易日已完成 |
| NEW-OPS-003 | **P1** | Historical catch-up 用 `as_of=now`，market-data 未真实按 pending date 回放 | 交易日标签与事实时点可能错位，破坏 Point-in-Time 语义 |
| NEW-CTX-001 | **P1** | Position Decision Context 用 FEATURE_LKG close 判断 stop/target | “现在卖不卖”可能基于旧收盘价，而非实时价格 |
| NEW-CTX-002 | **P1** | Position API/MCP 未注入 DeepMarketData/Calendar | 60m/15m/5m 和 holding sessions 在主路径退化 UNKNOWN |
| NEW-PF-001 | P2 | Mature Engine baseline `max()` 无空集合兜底 | 单个异常 Decision 可能使整批成熟 Job 失败 |
| NEW-AUD-001 | P2 | API 不向 Audited Service 传播 authenticated request_id | 审计链请求关联弱 |
| NEW-API-001 | P3 | Portfolio preferences 两个 Alias 共享同一 operation id | OpenAPI Warning/客户端代码生成冲突风险 |
| NEW-OCR-001 | P2 | OCR 可写目录依赖生产手工 env，Compose/Example 未固化 | 新机器 read-only 容器可能无法写图片 |
| REMAIN-OPS-EXPECTED | P1/P2 | Expected Run Registry/Update 仍未进 Scheduler | AI 日程/任务闭环仍依赖其它入口 |
| REMAIN-STR-RUNTIME | P1 | ReleaseResolver 只记录报告，未真正选择业务 Executor/Version | Release State 与实际 Strategy Runtime 仍未完全闭环 |
| REMAIN-API-GETWRITE | P2 | GET Comparison/Context Pack 仍产生 DB 写入 | 缓存/预取/匿名请求可制造不可变 Pack |

---

# 5. 原问题状态总表

| 原问题 | 本轮判定 | 说明 |
|---|---|---|
| SEC-001 | **部分关闭** | WRITE/Portfolio 前缀保护已完成；Public GET Allowlist 仍错误 |
| SEC-002 | **部分关闭** | actor/confirmed_by 已绑定；created_by/corrected_by 等未统一 |
| OPS-001 | **部分关闭** | 新 Scheduler/Orchestrator 已有；Compose 旧 Worker + Expected Run 缺 + Catchup Bug |
| DAT-001 | **基本关闭** | 5m/15m/60m Deep Service 已有；主 Position Context 接线见 CTX |
| DAT-002 | **关闭** | 周线真实计算 + 字段统一 + 下降趋势中反弹规则 |
| DAT-003 | **部分关闭** | Index 完成，Industry 未完成 |
| DAT-004 | **部分关闭** | Index/Breadth/Turnover 提升；limit/style/industry rotation 仍 UNKNOWN |
| CTX-001 | **部分关闭** | Payload 大幅补齐；实时价/Deep/Calendar 主路径仍未接好 |
| CTX-002 | **关闭** | Portfolio 不再永久 `not_available_in_phase6` |
| PF-001 | **部分关闭** | 自动 Mature 已有，但只自动做 Selection，七类未闭环 |
| PF-002 | **部分关闭** | 已真实重算 Feature；未重放完整 deterministic pipeline |
| STR-001 | **部分关闭** | Shadow Engine 存在；未日常自动接入生产 Runtime |
| STR-002 | **部分关闭** | Resolver 存在；实际业务执行路径没有真正由它选择 |
| POR-001 | **关闭** | Correction 累计 patch/sequence/hash/reverse 已修 |
| POR-002 | **关闭** | Opening baseline replay boundary 已修 |
| POR-003 | **关闭** | 收紧为 WEIGHTED_AVERAGE |
| POR-004 | **基本关闭** | Price/Time/Qty/Trigger/Cancel 已补 |
| POR-005 | **基本关闭** | Match/Verify Job 已有；受 Compose Scheduler 可恢复性影响 |
| AUD-001 | **部分关闭** | AuditRecorder 大量接入；Principal/request_id/覆盖仍不完整 |
| API-001 | **基本关闭** | 主要 READ Contract 已补 |
| API-002 | **未关闭** | GET 仍 Build/Publish Pack |
| API-003 | **关闭** | V3 Error Envelope 已统一 |
| API-004 | **基本关闭** | Idempotency matrix/测试明显补齐 |
| OCR-001 | **基本关闭** | Tesseract + Draft + Confirm 已成 MVP |
| DOC-001 | **部分关闭** | 当前状态和历史段落仍相互矛盾 |
| OPS-002 | **部分关闭** | 市场数据生产已提升；Evidence/Recall/Context/Attribution 是否日常产出仍需生产证据 |
| OPS-003 | **基本关闭** | backup/restore script + 生产演练记录已存在 |
| TEST-001 | **基本关闭** | 20 步 Product E2E 已新增，但不等于生产 Runtime 全自动 |
| TEST-002 | **基本关闭** | 测试入口改善，仍有 Unicode/OpenAPI 小问题 |
| PG-001 | **非缺陷** | V3 Baseline 本来就不要求服务器主动调用 ChatGPT |
| PG-002 | **大幅改善/部分关闭** | OCR+人工确认流程已有；完整日常 UI 仍可继续增强 |

---

# 6. 我建议 Claude Code 下一轮只修这些，不要继续扩功能

按风险顺序：

1. **收紧 V3 Public READ**：建立明确 Market READ Allowlist，所有 Watchlist/Decision/EntryPlan/Task/Strategy/Performance/Portfolio/Release 控制面默认 Token；补匿名 401/200 矩阵测试。
2. **Principal 全绑定**：覆盖 `created_by/corrected_by/confirmed_by/actor_*`；所有 audited API 传 `request_id`。
3. **修正式部署 Source of Truth**：`docker-compose.yml`、deployment docs 统一切 `scripts.v3_scheduler`；把 OCR store path、worker healthcheck 固化进 Git，而非只在服务器手工设置。
4. **修 Scheduler Catch-up**：终端完成标记用 `full-recall`/run completion；区分 operational catchup 和 point-in-time historical backfill，禁止 `pending_date + as_of=now` 被解释为历史事实。
5. **把 Position Context 真接通**：API/MCP 注入 Calendar + DeepMarketData + 实时 Quote；“现在卖不卖” Stop/Target 用当前 quote，FEATURE_LKG 作为独立 EOD 事实。
6. **完成 Performance 七类归因 + Mature robustness**：至少 Initial Entry/User Execution/Risk Control 等从真实 Trade/Plan 自动产生；修空 baseline bar 单候选隔离。
7. **扩 Deterministic Replay**：继续重放 Regime/Recall/Comparison/Context/Entry deterministic 层；AI 层保持 immutable-result boundary，不接固定评分。
8. **把 Shadow/Release 真接 Runtime**：建立 Production Executor Registry/Resolver consumer；Scheduler/业务请求按照 effective release configuration 真选择版本，并自动产 Shadow Observation。
9. **关闭 API-002**：Build 走 POST，Read 走 GET；旧 GET Alias 可 deprecated 兼容。
10. **补 Expected Run Job + 状态文档 Current Truth**：不要再让历史旧段落被 Codex 当成当前事实。

---

# 7. Release 建议

当前仍建议：

```text
Release State = V2
V3 API = 可继续测试/读取
V3 WRITE = 仅认证环境
V3 Strategy = 不激活
```

在以下阻断项关闭前，不建议切 V3：

```text
NEW-SEC-001
NEW-SEC-002
NEW-OPS-001
NEW-OPS-002
NEW-OPS-003
NEW-CTX-001
NEW-CTX-002
PF-001 七类自动归因关键部分
STR-001/STR-002 Production Runtime
```

---

# 8. 最终评价

Claude Code 这轮不是“修了个表面”。**大约七成以上的整改意见已经有真实代码落地，其中 Portfolio Ledger、Weekly、多周期能力、Index Benchmark、Error Contract、Idempotency、OCR、Product E2E 等提升很明显。**

但它也出现了典型的“服务已经写了，主路径没真正注入；Resolver 已经写了，Runtime 没真正消费；生产手工部署正确，但 Git/IaC 还是旧配置”的问题。

所以本轮最准确的结论是：

> **V3 已从“架构骨架 + 分散模块”提升到“基本成型的产品系统”，但还没有达到此前整改文档定义的最终 Product Closure。剩余工作已经从大规模建设转为少量但非常关键的安全、调度时点、实时持仓上下文、绩效/回放、策略 Runtime 收口。**

下一轮不应该再扩展新业务模块，而应只处理上述剩余 P0/P1 和 Runtime 接线问题，然后进行一次真正的 Final Release Gate。
