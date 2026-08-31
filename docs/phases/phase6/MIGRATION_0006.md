# 0006 Context 与 Task Foundation Migration 设计

> 状态：P6-01 冻结设计；P6-02 一次实现完整 `0006_context_task_foundation` DDL，后续 Task 不回改已发布 Migration。
>
> Down Revision：`0005_multi_recall_foundation`。本文是 Migration 实现的精确输入，不代表 Migration 已执行。

## 1. 新增表

### 1.1 `candidate_comparison_packs`

| 字段 | 类型/约束 |
|---|---|
| `comparison_pack_id` | UUID PK |
| `candidate_set_id` | UUID NOT NULL；外部 Candidate Set 的不可变标识，当前无 FK |
| `builder_version` / `schema_version` / `field_profile_version` | VARCHAR(64) NOT NULL |
| `universe_snapshot_id` | UUID NOT NULL FK → `universe_snapshots` |
| `feature_run_id` | UUID NOT NULL FK → `feature_runs` |
| `recall_run_id` | UUID NULL FK → `recall_runs` |
| `regime_snapshot_id` | UUID NULL FK → `market_regime_snapshots` |
| `as_of` / `known_at` | TIMESTAMPTZ NOT NULL；CHECK `known_at >= as_of` |
| `candidate_count` | INTEGER NOT NULL；CHECK 20–100 |
| `coverage` | NUMERIC(8,7) NOT NULL；CHECK 0–1 |
| `missing_summary` / `trim_summary` | JSONB NOT NULL |
| `content_hash` | VARCHAR(64) NOT NULL UNIQUE |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() |

索引：`(as_of, known_at)`、`(feature_run_id, as_of)`、`recall_run_id`。表启用 `prevent_mutation`，发布后禁止 UPDATE/DELETE。

### 1.2 `candidate_comparison_members`

| 字段 | 类型/约束 |
|---|---|
| `comparison_pack_id` | UUID NOT NULL FK → Pack |
| `security_id` | UUID NOT NULL FK → `securities` |
| `candidate_order` | INTEGER NOT NULL；CHECK 1–100 |
| `compact_payload` | JSONB NOT NULL；只保存正式 Member Contract，不得包含 Final Score |
| `coverage` | NUMERIC(8,7) NOT NULL；CHECK 0–1 |
| `stale` | BOOLEAN NOT NULL |
| `missing_fields` | JSONB NOT NULL |

PK `(comparison_pack_id, security_id)`；UNIQUE `(comparison_pack_id, candidate_order)`；索引 `security_id`。表启用 `prevent_mutation`。Pack 的 `candidate_count` 与成员数量、连续顺序由 Repository 在同一短事务发布前校验。

### 1.3 `context_packs`

| 字段 | 类型/约束 |
|---|---|
| `context_pack_id` | UUID PK |
| `context_level` | VARCHAR(16) NOT NULL；CHECK FAST/NORMAL/DEEP |
| `subject_type` / `subject_id` | VARCHAR(32) / VARCHAR(128) NOT NULL |
| `task_profile_id` | UUID NOT NULL FK → `task_profiles` |
| `task_profile_version` | INTEGER NOT NULL；CHECK > 0 |
| `builder_version` / `schema_version` | VARCHAR(64) NOT NULL |
| `as_of` / `known_at` | TIMESTAMPTZ NOT NULL；CHECK `known_at >= as_of` |
| `universe_snapshot_id` | UUID NOT NULL FK → `universe_snapshots` |
| `feature_run_id` | UUID NOT NULL FK → `feature_runs` |
| `recall_run_id` | UUID NULL FK → `recall_runs` |
| `regime_snapshot_id` | UUID NULL FK → `market_regime_snapshots` |
| `comparison_pack_id` | UUID NULL FK → `candidate_comparison_packs` |
| `token_budget` / `actual_tokens` | INTEGER NOT NULL；CHECK `actual_tokens >= 0 AND actual_tokens <= token_budget`，并按 Level 检查预算区间 |
| `coverage` | NUMERIC(8,7) NOT NULL；CHECK 0–1 |
| `missing_fields` / `trim_summary` / `payload` / `references` | JSONB NOT NULL |
| `content_hash` | VARCHAR(64) NOT NULL UNIQUE |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() |

索引：`(subject_type, subject_id, as_of)`、`(task_profile_id, as_of)`、`comparison_pack_id`。表启用 `prevent_mutation`。

### 1.4 `context_evidence_selections`

| 字段 | 类型/约束 |
|---|---|
| `context_pack_id` | UUID NOT NULL FK → `context_packs` |
| `evidence_id` | UUID NOT NULL FK → `evidence_records` |
| `evidence_known_at` | TIMESTAMPTZ NOT NULL |
| `selection_reason` | TEXT NOT NULL |
| `side` | VARCHAR(16) NOT NULL；CHECK SUPPORT/CONTRARY/NEUTRAL |
| `retrieval_score` / `relevance` | NUMERIC(8,7) NOT NULL；CHECK 0–1 |
| `source_priority` | INTEGER NOT NULL；CHECK >= 0 |
| `final_order` | INTEGER NOT NULL；CHECK >= 1 |

PK `(context_pack_id, evidence_id)`；UNIQUE `(context_pack_id, final_order)`；索引 `evidence_id`。表启用 `prevent_mutation`。`evidence_known_at <= context_packs.as_of` 不能用跨表 CHECK 表达，由 Repository 在同一短事务发布前校验并由 PostgreSQL 集成测试证明。

## 2. 现有 Task 表增量

### 2.1 `task_profiles`

新增 `trading_calendar_source VARCHAR(128)`、`trading_calendar_version VARCHAR(64)`、`comparison_first BOOLEAN`、`candidate_limit INTEGER NULL`、`topk_limit INTEGER NULL`、`topk_context_level VARCHAR(16) NULL`、`strategy_version VARCHAR(64)`。

迁移既有行时：`trading_calendar_source = COALESCE(trading_calendar, 'UNKNOWN')`、Calendar Version/Strategy Version 使用 `UNKNOWN`、`comparison_first=false`，再改为 NOT NULL。保留旧 `trading_calendar` 列到 Phase Gate 后兼容清理，不在 0006 删除。增加 Comparison 字段成组 CHECK、TopK 不超过 Candidate、TopK Level 仅 NORMAL/DEEP、Content Hash UNIQUE。Profile 启用 `prevent_mutation`；启停或字段变化发布新 Version，不原地改行。

### 2.2 `expected_runs`

新增 `task_profile_version INTEGER`、`known_at TIMESTAMPTZ`、`content_hash VARCHAR(64)`、`row_version BIGINT DEFAULT 1`。既有行从关联 Profile Version 和 `created_at` 回填，Hash 使用项目 `canonical_hash` 同语义的迁移回填函数生成，之后全部设 NOT NULL 并为 Content Hash 加 UNIQUE。增加状态 CHECK：`EXPECTED | CANCELLED`。Expected Run 是可取消的 Registry，使用 `row_version` 乐观锁并在状态变更时同步重算 Hash，不安装不可变 Trigger；其状态绝不表示 AI 执行。

### 2.3 `task_runs`

新增 `task_profile_version INTEGER`，从关联 Profile 回填后设 NOT NULL；为现有 `context_pack_id` 增加 FK → `context_packs`。保留 `row_version` 作为乐观锁。增加状态与计数一致性 CHECK：COMPLETED 全部成功；PARTIAL_COMPLETED 至少一组成功但未全部成功；PENDING_IMPORT/MISSED 不得有成功组。Task Run 是 Phase7 Atomic Group 提交时需要更新的 Registry，不启用不可变触发器。

## 3. Upgrade 顺序与原子性

1. 新建 Comparison Pack/Member；
2. 增量并回填 Task Profile；
3. 新建 Context Pack/Selection；
4. 增量并回填 Expected Run/Task Run，最后增加 FK/CHECK/UNIQUE；
5. 为四张新事实表和 Task Profile 安装不可变触发器；Expected Run/Task Run 是带乐观锁的 Registry，不安装该 Trigger；
6. 所有 DDL 在 Alembic 事务内完成，回填不得调用网络或 Builder。

## 4. Downgrade 顺序

先移除 Task Run 的 Context FK 和新增 CHECK/列，再移除 Expected/Profile 新约束与列；按 Selection → Context Pack → Member → Comparison Pack 顺序删除新增表。删除表前先删除其 `prevent_mutation` Trigger。不得触碰 Phase 1–5 数据表和数据。

## 5. 实施验收（后续 Task）

- `0005 → 0006 → 0005 → 0006` 在隔离 PostgreSQL 17 通过；
- Phase 1–5 事实行数与 Hash 在往返迁移后不变；
- 新表 FK、范围、顺序、时点和不可变约束有真实 PostgreSQL 负向测试；
- 相同 Content Hash 幂等，业务键异内容与 Task Run 乐观锁冲突明确；
- Migration 脚本不得导入 Provider、Builder、FastAPI 或 Legacy 业务模块。
