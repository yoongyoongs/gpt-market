# Phase 6 Scope：Context、Task 与 READ JSON

> This document is derived from V3 Architecture Baseline.
>
> This document MUST NOT override the V3 Architecture Baseline.
>
> If conflict exists, V3 Architecture Baseline wins and the conflict must be reported.

## Why

Phase 1–5 已提供版本化全市场事实、Evidence 和 Multi-Recall。Phase 6 将这些事实组织成 ChatGPT Web 可高效读取、可重放的紧凑比较与分级 Context，并建立只表达任务预期和执行状态的 Task Profile/Run。

## In Scope

- 20–100 只 CandidateComparisonPack 与 Candidate Comparison Context；
- Universe/Recall/Raw N→TopK 工作流所需紧凑 READ；
- TopK 单股 FAST/NORMAL/DEEP Context Pack；
- Evidence Selection 原因、正反面、排序、Coverage 和冲突说明；
- Task Profile、Expected Run、Task Run Registry 的 READ 侧与调度语义；
- Schedule、Timezone、Trading Calendar 元数据；
- ChatGPT Web 所需 Phase 6 机器可读 JSON API；
- Pack/Task 的版本、`as_of`、`known_at`、输入 Run/Revision 和 Content Hash。

## Out of Scope

- AI Result Single/Bundle Import、Atomic Group 提交和决策状态（Phase 7）；
- Watchlist、Decision、EntryPlan、Review/MarketReview 持久化（Phase 7）；
- Trade、OCR、Portfolio 和 Position Context（Phase 8）；
- Action/Entry 业务判断、PositionReview（Phase 9）；
- 严格未来收益、Replay、Regression 和完整 Recall Miss 归因（Phase 10）；
- 模型 API、Browser Bridge、自动 POST、Broker 和企业级鉴权（V4/Hardening）；
- 任何 V1/V2、行情 Provider、扫描和评分改造。

## Inputs

- Phase 3 Published Feature Run、FeaturePage、MarketRegimeSnapshot；
- Phase 4 Evidence READ、Conflict/Coverage 和 Untrusted 标记；
- Phase 5 RecallRun/Result、RawOpportunity 和 Full Universe 可访问性；
- Phase 1 AgentTask/Task 基础与审计契约；
- 已验证的交易日历 Provider 元数据。

## Outputs

- `candidate_comparison_packs/members`、`context_packs/context_evidence_selections` 及必要 Task 表的版本化 Migration；
- Comparison/Context/Task Domain、Repository、Application Service 和 READ API；
- 独立 Job/Builder、契约测试、真实 PostgreSQL 与性能证据；
- Phase 6 Capsule 和验收记录。

## Boundaries

遵守 G-001–G-014、G-015–G-027、G-028–G-030、G-042–G-048。Pack 不产生 Final Score；Context 不执行 AI；Expected Run 不代表 AI 已运行；READ Handler 不触发现场 Provider 采集。
