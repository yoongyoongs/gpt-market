# Phase 4 证据管道实施记录

> 日期：2026-08-30（Asia/Shanghai）  
> 分支：`codex/phase4-evidence-ingestion`  
> 状态：Evidence 基础闭环、Fetch Run 恢复及 Conflict/Dedup 已完成；Core Provider、实体匹配和 READ API 待完成

## 1. 当前范围

本里程碑按《架构设计实施稿》启动 Phase 4，不修改 V1/V2 评分，不增加自动交易：

- `0004_evidence_ingestion` Migration；
- Evidence Source 能力、优先级、限流、解析版本和可靠性配置；
- Fetch Run、Raw Document、不可变 Parse Attempt、Entity Link、Evidence Relation、Conflict Set/Member 数据结构；
- Evidence 来源类型、衰减、失效、时点和 Untrusted Data 领域约束；
- Raw 先提交、后解析的批次服务；
- Fetch Run 游标 checkpoint、分段恢复、终态重放和 `row_version` 乐观锁；
- 精确 Raw 去重、解析幂等、按 `known_at` Retrieval 和跨来源独立保留；
- 规范 Payload 精确/近似重复关系、同 Claim 多值冲突集合和来源优先级临时选择。

## 2. 关键不变量

- Raw 原文写入并提交成功前不得发布解析 Evidence；解析失败仍保留 Raw 和不可变失败 Attempt。
- Raw、Parse Attempt、Evidence、Entity Link、Relation 和 Conflict 事实均 append-only；解析状态不通过 UPDATE Raw 冒充。
- 相同内容来自不同来源时保留各自 Raw 与 Evidence，不用全局 Content Hash 唯一约束抹掉来源链。
- Opinion Source 只能产生 `OPINION`，不能升级为 FACT；`OFFICIAL_DISCLOSURE` 必须来自 OFFICIAL Source。
- 外部文本默认 `untrusted=true`；本阶段只作为数据保存，不能进入 Agent Instruction。
- Retrieval 必须满足 `known_at <= as_of`，并排除已失效、撤回或被替代的 Evidence。
- Linear/Exponential Decay 必须有明确 Rate；Fixed Expiry 必须有 `expire_at`，衰减在读取时计算，不回写历史 Evidence。
- 低置信实体关联可以保存为 Candidate，不能自动冒充 Confirmed Link。
- Fetch Run 只有 `RUNNING` 可写 checkpoint；上游异常必须落为 `FAILED`，未耗尽批次必须返回可恢复 Cursor。
- 重复和冲突 Evidence 均不得被覆盖或删除；临时选择只记录解释，不把低优先级来源从证据链中移除。

## 3. 数据库变更

新增表：

- `evidence_fetch_runs`；
- `raw_document_parse_attempts`；
- `evidence_entity_links`；
- `evidence_relations`；
- `evidence_conflicts`；
- `evidence_conflict_members`。

扩展 `evidence_sources/raw_documents/evidence_records`，增加来源能力、Raw Payload、规范引用、Claim Key、Normalized Payload、Source Type、Source Priority、Decay、Availability、Supersedes 和检索索引。Fetch Run 同时保存 Raw、Parse 和 Evidence 数量及失败摘要。原有 Raw/Evidence 全局 Content Hash 唯一约束改为带来源/Raw/Parser 身份的约束，既支持精确去重，也保留多源事实。

## 4. 当前验收

- PostgreSQL 17 `0003 -> 0004 -> 0003 -> 0004` 双往返通过；Migration 回填期间临时移除旧 Raw/Evidence 不可变触发器并在同一事务恢复，运行期约束不放松。
- Phase 4 Domain/PostgreSQL 专项 `13 passed`：Raw 去重、跨来源同内容保留、Parse/Link/Relation/Conflict 原子发布、时点 Retrieval、解析失败保留 Raw、重复跑批幂等和不可变触发器通过。
- 两页真实数据库分段任务验证首段 `RUNNING + cursor`、恢复后 `COMPLETED`、终态重放不请求 Provider，陈旧 `row_version` checkpoint 被拒绝。
- 多源真实数据库样例验证相同值建立 `EXACT_DUPLICATE`，不同值形成保留三方成员的 `OPEN` Conflict，并按来源优先级、置信度和 `known_at` 记录临时选择。
- 配置真实 PostgreSQL 的完整回归 `169 passed, 5 skipped`；本轮 Ruff、`git diff --check` 通过。
- 本轮文件 Ruff 和 `git diff --check` 通过；全仓仍保留既有 Legacy Ruff 告警，未扩大 V1/V2 修改范围。

## 5. 待完成

1. 接入 Core 公告/交易所/巨潮、财务与业绩、已有 Vendor、基础新闻和重要国内政策 Provider；
2. 实现 Provider/Parser Registry、限流、重试和 Provider 级失败隔离；
3. 完成 Security/Industry/Market Entity Link 与低置信 Candidate 工作流；
4. 完成 Decay-aware Retrieval、Coverage/UNKNOWN、READ API 和 Job；
5. 真实多源样本验收、性能测试、文档收口、Phase 4 标签。

本记录不表示 Phase 4 或整个 V3 已完成。生产 V3 仍关闭，生产数据库尚未执行 `0004`。
