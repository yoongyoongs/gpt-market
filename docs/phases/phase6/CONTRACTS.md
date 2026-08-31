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

首版 Schema Version 固定为 `candidate-comparison.v1`。Pack 必须绑定：`comparison_pack_id`、Builder/Schema Version、Field Profile Version、Candidate Set、候选原顺序、Universe/Feature/Recall/Regime IDs、`as_of`、`known_at`、Coverage、Missing/Trim Summary 和 Content Hash。

Member 数量为 20–100；只含统一紧凑事实，包括证券标识、Recall 命中、趋势/位置/波动/量价/流动性、基本面摘要、风险/Evidence 摘要和质量。字段不可用时为 `null/UNKNOWN`。Pack 不读取完整单股 Evidence/分钟 K，不产生统一 Final Score。

`candidate_order` 必须从 1 连续递增，并与输入 Candidate Set 顺序一致；同一 Pack 的 `security_id` 不得重复。Content Hash 排除数据库生成的 `comparison_pack_id` 和发布时间 `known_at`，包含规范化输入、候选顺序、Payload、Schema/Builder/Field Profile Version，因此相同规范输入幂等、候选顺序变化会产生新 Hash。

## 3. ContextPack 输出契约

首版 Schema Version 固定为 `context-pack.v1`。Level 为 `FAST | NORMAL | DEEP`，Token Budget 分别限制为 2k–4k、5k–8k、10k–14k，实际 Token 不得超过预算。Pack 绑定 subject、Task Profile ID/Version、`as_of/known_at`、Snapshot/Run/Revision IDs、可选 Comparison Pack、Token Budget、实际大小、Builder Version、裁剪说明、Payload/Reference、Coverage、Missing Fields 和 Content Hash。

每个 Evidence Selection 保存 Evidence ID、Evidence `known_at`、selection_reason、`SUPPORT | CONTRARY | NEUTRAL`、retrieval_score、relevance、source_priority 和从 1 连续递增的最终顺序。同一 Pack 不得重复 Evidence；所有 Evidence 必须满足 `evidence_known_at <= context.as_of`。Untrusted 文本只能进入带数据边界的 Payload。

## 4. Task Profile/Run 契约

Profile 保存 code/version、schedule、timezone、trading_calendar source/version、Context Level、comparison_first、candidate/topk limit、TopK Context Level、输出 Schema、预期组数、宽限期和 Strategy Version。`comparison_first=true` 时 Candidate Limit 必须为 20–100、TopK 不得超过 Candidate Limit 且 TopK Context 只能是 NORMAL/DEEP；为 false 时这三个字段必须为空。Profile 使用 `(profile_code, version)` 和 Content Hash 幂等，修改必须发布新版本。

Expected Run 只表达理论任务时间，状态仅为 `EXPECTED | CANCELLED`，不得出现“AI 已执行”字段或语义；取消是 Registry 状态变更，使用 `row_version` 乐观锁且同步重算 Content Hash。Task Run 状态为 `PENDING_IMPORT | PARTIAL_COMPLETED | COMPLETED | MISSED | CANCELLED`，并保存四项组计数、Task Profile Version、可选 Context Pack ID/Hash 和乐观锁版本；Phase 6 只建立/读取任务事实，不实现 Phase 7 的 AI Import/Atomic Group 写入。

Task Run 必须满足 `expected = successful + failed + pending`。`COMPLETED` 必须全部成功；`PARTIAL_COMPLETED` 必须至少一组成功但未全部成功；成功数为零且未取消/未超过宽限期时为 `PENDING_IMPORT`；成功数为零且超过宽限期才可为 `MISSED`；`CANCELLED` 只表示人工取消。

## 5. Invariants and Errors

- `known_at` 记录可知时间；选择条件必须满足 `known_at <= context_as_of`；
- 仅 Published Feature/Recall 及其不可变输入可以构建 Pack；
- 相同规范输入和 Builder Version 幂等返回同一 Content Hash；
- 不完整输入显式返回 Coverage/Missing/UNKNOWN，不猜测；
- 输入 Run/Snapshot 不存在返回 404；字段/Profile 非法返回 422；Hash/版本冲突返回 409；V3 禁用返回 503；
- Builder 在事务外组装，最终短事务原子发布；已发布 Pack append-only。

Repository Contract 使用以下统一语义：`get*()` 的可选查询未命中返回 `None`；Builder 所需强引用缺失由 Application 转为 404；`publish*()` 返回 `True` 表示首次发布，完全相同 Content Hash 的幂等重放返回 `False`；同一不可变业务键对应不同内容、或 `save_expected_run/save_task_run(... expected_version=...)` 乐观锁失败时抛出 `RepositoryConflictError` 并映射 409。Pydantic/请求字段校验失败映射 422；Feature Flag 门禁在 Repository 调用前映射 503。

## 6. Versioning

首个正式 Pack Schema Version 为 `candidate-comparison.v1` 和 `context-pack.v1`；Builder Version 由实现 Task 独立版本化，不得借 Schema Version 代替。Migration 实施版本固定为 `0006_context_task_foundation`，必须从 `0005_multi_recall_foundation` 向前追加，并严格遵循 `MIGRATION_0006.md`；P6-01 只冻结设计，不创建迁移脚本。
