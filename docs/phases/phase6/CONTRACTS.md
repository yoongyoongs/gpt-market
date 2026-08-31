# Phase 6 Contracts

> Derived from V3 Architecture Baseline; this document cannot override it. Conflicts require `DESIGN_CONFLICT`.

## 1. Frozen Inputs

普通 Phase 6 Task 优先只读取以下现有契约，默认不得修改：

- `FeatureQuery/FeaturePage/FeatureRun/MarketRegimeSnapshot`：`app/v3/domain/features.py`；
- `EvidenceReadQuery/EvidenceReadPage/NormalizedEvidence`：`app/v3/domain/evidence.py`；
- `RecallRun/RecallReadPage/RawOpportunityReadPage`：`app/v3/domain/recall.py`；
- `FeatureRepository/EvidenceRepository/RecallRepository` Protocol：`app/v3/repositories/protocols.py`；
- `AgentTask/AIResultEnvelope` Phase 1 基础契约，仅作为 Task/Context 引用，不进入 Import 实现；
- 当前 `/api/v3/universe/features`、`/market-regime`、`/evidence/...`、`/recalls`、`/raw-opportunities` READ。

若这些输入不能满足正式 Phase 6 需求，先输出 `DESIGN_CHANGE_REQUIRED`，不得顺手改 Phase 3–5 语义。

## 2. CandidateComparisonPack 输出契约

Pack 必须绑定：`comparison_pack_id`、Builder/Schema Version、Candidate Set、候选原顺序、Universe/Feature/Recall/Regime IDs、`as_of`、`known_at`、Coverage、Missing/Trim Summary 和 Content Hash。

Member 数量为 20–100；只含统一紧凑事实，包括证券标识、Recall 命中、趋势/位置/波动/量价/流动性、基本面摘要、风险/Evidence 摘要和质量。字段不可用时为 `null/UNKNOWN`。Pack 不读取完整单股 Evidence/分钟 K，不产生统一 Final Score。

## 3. ContextPack 输出契约

Level 为 `FAST | NORMAL | DEEP`，绑定 subject、Task Profile、`as_of/known_at`、Snapshot/Run/Revision IDs、Token Budget、实际大小、Builder Version、裁剪说明、Payload/Reference 和 Content Hash。

每个 Evidence Selection 保存 Evidence ID、selection_reason、`SUPPORT | CONTRARY | NEUTRAL`、retrieval_score、relevance、source_priority 和最终顺序。Untrusted 文本只能进入带数据边界的 Payload。

## 4. Task Profile/Run 契约

Profile 保存 code/version、schedule、timezone、trading_calendar source/version、Context Level、comparison_first、candidate/topk limit、输出 Schema、宽限期和 Strategy Version。

Expected Run 只表达理论任务时间。Task Run 状态为 `PENDING_IMPORT | PARTIAL_COMPLETED | COMPLETED | MISSED | CANCELLED`，并保存四项组计数；Phase 6 只建立/读取任务事实，不实现 Phase 7 的 AI Import/Atomic Group 写入。

## 5. Invariants and Errors

- `known_at` 记录可知时间；选择条件必须满足 `known_at <= context_as_of`；
- 仅 Published Feature/Recall 及其不可变输入可以构建 Pack；
- 相同规范输入和 Builder Version 幂等返回同一 Content Hash；
- 不完整输入显式返回 Coverage/Missing/UNKNOWN，不猜测；
- 输入 Run/Snapshot 不存在返回 404；字段/Profile 非法返回 422；Hash/版本冲突返回 409；V3 禁用返回 503；
- Builder 在事务外组装，最终短事务原子发布；已发布 Pack append-only。

## 6. Versioning

首个正式版本由对应 Task 确定并写入 Domain/Migration；在此之前不得把示例 `v1` 当作已发布事实。Migration 下一版本必须从 `0005_multi_recall_foundation` 向前追加。
