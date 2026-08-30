# Phase 4 证据管道实施记录

> 日期：2026-08-30（Asia/Shanghai）  
> 分支：`codex/phase4-evidence-ingestion`  
> 状态：Evidence 基础模型与 Raw→Parse 最小闭环完成；Core Provider、Conflict/Dedup 和 READ API 待完成

## 1. 当前范围

本里程碑按《架构设计实施稿》启动 Phase 4，不修改 V1/V2 评分，不增加自动交易：

- `0004_evidence_ingestion` Migration；
- Evidence Source 能力、优先级、限流、解析版本和可靠性配置；
- Fetch Run、Raw Document、不可变 Parse Attempt、Entity Link、Evidence Relation、Conflict Set/Member 数据结构；
- Evidence 来源类型、衰减、失效、时点和 Untrusted Data 领域约束；
- Raw 先提交、后解析的批次服务；
- 精确 Raw 去重、解析幂等、按 `known_at` Retrieval 和跨来源独立保留。

## 2. 关键不变量

- Raw 原文写入并提交成功前不得发布解析 Evidence；解析失败仍保留 Raw 和不可变失败 Attempt。
- Raw、Parse Attempt、Evidence、Entity Link、Relation 和 Conflict 事实均 append-only；解析状态不通过 UPDATE Raw 冒充。
- 相同内容来自不同来源时保留各自 Raw 与 Evidence，不用全局 Content Hash 唯一约束抹掉来源链。
- Opinion Source 只能产生 `OPINION`，不能升级为 FACT；`OFFICIAL_DISCLOSURE` 必须来自 OFFICIAL Source。
- 外部文本默认 `untrusted=true`；本阶段只作为数据保存，不能进入 Agent Instruction。
- Retrieval 必须满足 `known_at <= as_of`，并排除已失效、撤回或被替代的 Evidence。
- Linear/Exponential Decay 必须有明确 Rate；Fixed Expiry 必须有 `expire_at`，衰减在读取时计算，不回写历史 Evidence。
- 低置信实体关联可以保存为 Candidate，不能自动冒充 Confirmed Link。

## 3. 数据库变更

新增表：

- `evidence_fetch_runs`；
- `raw_document_parse_attempts`；
- `evidence_entity_links`；
- `evidence_relations`；
- `evidence_conflicts`；
- `evidence_conflict_members`。

扩展 `evidence_sources/raw_documents/evidence_records`，增加来源能力、Raw Payload、规范引用、Claim Key、Normalized Payload、Source Type、Decay、Availability、Supersedes 和检索索引。原有 Raw/Evidence 全局 Content Hash 唯一约束改为带来源/Raw/Parser 身份的约束，既支持精确去重，也保留多源事实。

## 4. 当前验收

- 全新 PostgreSQL 17 从 base 升级至 `0004_evidence_ingestion` 成功，共 29 张 V3 表；新增 5 张不可变事实表的 UPDATE/DELETE 触发器共 10 个事件绑定。
- `0004 -> 0003 -> 0004` Downgrade/Upgrade 通过；降级后 6 张新增表和 Raw 扩展列均清除。
- 真实 PostgreSQL 集成 `2 passed`：Raw 去重、跨来源同内容保留、Parse/Link 原子发布、时点 Retrieval、解析失败保留 Raw、重复跑批幂等和不可变触发器通过。
- Phase 4 Domain/Migration 专项 `8 passed`；本地完整回归 `150 passed, 19 skipped`。
- 本轮文件 Ruff 和 `git diff --check` 通过；全仓仍保留既有 Legacy Ruff 告警，未扩大 V1/V2 修改范围。

## 5. 待完成

1. 增加 Fetch Run checkpoint、重试、限流和 Provider 级失败隔离；
2. 接入 Core 公告/交易所/巨潮、财务与业绩、已有 Vendor、基础新闻和重要国内政策 Provider；
3. 实现 Parser Registry、规范化策略、精确/近似 Dedup Relation；
4. 实现 Security/Industry/Market Entity Link 与低置信 Candidate 工作流；
5. 实现同 Claim 多源 Conflict Detection、来源优先级和不可变 Conflict Set；
6. 实现 Decay-aware Retrieval、Coverage/UNKNOWN、READ API 和 Job；
7. 真实多源样本验收、性能测试、文档收口、Phase 4 标签。

本记录不表示 Phase 4 或整个 V3 已完成。生产 V3 仍关闭，生产数据库尚未执行 `0004`。
