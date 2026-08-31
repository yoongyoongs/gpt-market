# V3 Phase 6 上下文与任务实施记录

> 最后更新：2026-08-31（Asia/Shanghai）
>
> 稳定基线：`main@9506983` / `v3-phase5-multi-recall-20260831`
>
> 工作分支：`codex/phase6-context-task`
>
> 当前状态：P6-01 已完成；P6-02 已完成本地实现，服务器 SSH 服务异常，等待隔离 PostgreSQL 17 验收

本文按 Phase 1–5 的实施记录方式集中保存 Phase 6 的契约、迁移、任务和验收状态，不建立 Phase Capsule、Context Policy 或多层 Task 治理。正式需求与设计仍以需求规格、架构设计、技术架构、数据库设计和详细设计为准。

## 1. 实施边界

Phase 6 建设 Candidate Comparison、FAST/NORMAL/DEEP Context Pack、Task Registry 和只读 JSON 接口。Phase 6 不实现 Phase 7 的 AI Result Import，不修改 Phase 3–5 已冻结的 Feature、Evidence、Recall 语义，也不修改 V1/V2、Provider、Scanner、评分或生产配置。

Task 仅作为 Phase 内可验证的小步开发单位；每个 Task 完成后停止，不自动继续下一项。

## 2. Task 状态

| Task | 内容 | 状态 | 证据/后续 |
|---|---|---|---|
| P6-01 | 冻结 Context、Candidate Comparison、Task Domain/Repository 契约和 0006 设计 | DONE | `044da58`；状态提交 `1585be0` |
| P6-02 | 完整 0006 DDL 与 CandidateComparisonPack/Member 不可变持久化 | PARTIAL | 本地 PostgreSQL 16 完整回归通过；等待 PostgreSQL 17 验收后提交 |
| P6-03 | Comparison Builder 与 N→TopK READ | TODO | 依赖 P6-02；本轮不启动 |
| P6-04 | FAST/NORMAL/DEEP Context Pack 与 Evidence Selection | TODO | 依赖 P6-01/P6-02 |
| P6-05 | Task Profile、Expected Run 与 Task Run Registry | TODO | 不实现 AI Import |
| P6-06 | ChatGPT READ JSON 接线与契约测试 | TODO | 依赖 P6-03–P6-05 |
| P6-07 | 全市场性能验收与 Architecture Gate | TODO | Phase 6 最终验收 |

## 3. P6-01 已冻结契约

### 3.1 CandidateComparisonPack

- Schema Version：`candidate-comparison.v1`；
- 每包包含 20–100 个候选，保存候选原顺序、Universe/Feature/Recall/Regime 引用、`as_of`、`known_at`、Coverage、缺失/裁剪摘要和 Content Hash；
- Member 只保存统一紧凑事实，不读取完整单股 Evidence/分钟 K，不产生统一 Final Score；
- `candidate_order` 从 1 连续递增，同一 Pack 不得重复证券；
- Content Hash 包含规范输入、候选顺序、Payload 和版本，不包含数据库生成 ID 与发布时间；相同输入幂等，顺序变化产生新 Hash。

### 3.2 ContextPack

- Schema Version：`context-pack.v1`；Level 为 `FAST | NORMAL | DEEP`；
- Token Budget 分别为 2k–4k、5k–8k、10k–14k，实际 Token 不得超过预算；
- 保存 subject、Task Profile ID/Version、时点、Snapshot/Run/Revision、可选 Comparison Pack、版本、裁剪、Payload/Reference、Coverage、Missing Fields 和 Content Hash；
- Evidence Selection 保存 Evidence ID/known_at、选择原因、立场、检索分、相关度、来源优先级和连续最终顺序；
- 所有 Evidence 必须满足 `evidence_known_at <= context.as_of`，不可信文本只能进入有明确数据边界的 Payload。

### 3.3 Task Profile、Expected Run 与 Task Run

- Profile 保存 schedule、timezone、交易日历来源/版本、Context Level、comparison-first 参数、输出 Schema、预期组数、宽限期和 Strategy Version；修改必须发布新版本；
- Expected Run 只表示理论任务时间，状态为 `EXPECTED | CANCELLED`，不表示服务器执行过 AI；
- Task Run 状态为 `PENDING_IMPORT | PARTIAL_COMPLETED | COMPLETED | MISSED | CANCELLED`，并保存逻辑组计数、Profile Version、可选 Context 引用和乐观锁版本；
- `expected = successful + failed + pending`，状态必须与组计数一致；
- Phase 6 只建立和读取任务事实，不实现 Phase 7 的按 `atomic_group` 导入。

### 3.4 统一不变量

- 只读取 `known_at <= as_of` 的记录；只允许已发布的 Feature/Recall 及不可变输入构建 Pack；
- 缺失保持 `null/UNKNOWN` 并携带 Coverage/Missing，不允许猜测；
- Repository 可选读取未命中返回 `None`；首次发布返回 `True`，同 Hash 幂等重放返回 `False`；
- 不可变业务键异内容及乐观锁冲突使用 `RepositoryConflictError`；API 映射保持 404/409/422/503；
- Builder 在事务外组装，最终使用短事务原子发布；已发布 Pack append-only。

## 4. `0006_context_task_foundation` 迁移设计与实现

Down Revision 为 `0005_multi_recall_foundation`。P6-02 一次实现完整 DDL，后续 Task 不回改已发布 Migration。

### 4.1 新表

- `candidate_comparison_packs`：Pack 标识、版本、输入 Snapshot/Run、时点、候选数、Coverage、摘要和唯一 Content Hash；
- `candidate_comparison_members`：Pack/Security 复合主键、唯一连续顺序、紧凑 Payload、Coverage、stale 和 Missing；
- `context_packs`：Level、Subject、Task Profile、输入引用、Token Budget、Coverage、Payload/References 和唯一 Content Hash；
- `context_evidence_selections`：Context/Evidence 复合主键、唯一顺序、Evidence 时点、立场与选择评分。

四张新增事实表均安装 `prevent_mutation`，发布后禁止 UPDATE/DELETE。范围、外键、唯一性和 Level/Token 约束由 PostgreSQL 强制；跨表时点、成员数量与连续顺序由 Repository 在同一短事务发布前校验。

### 4.2 现有 Task 表增量

- `task_profiles` 新增 `trading_calendar_source`、`trading_calendar_version`、`comparison_first`、`candidate_limit`、`topk_limit`、`topk_context_level` 和 `strategy_version`，并启用不可变触发器；
- `expected_runs` 新增 Profile Version、known_at、Content Hash 和 row_version，作为可取消 Registry，不安装不可变触发器；
- `task_runs` 新增 Profile Version、Context 外键和状态/组计数一致性约束，保留乐观锁，不安装不可变触发器；
- 迁移使用应用层忽略的 `pre_phase6_content_hash` 保存既有 Profile Hash，Downgrade 恢复原值后删除兼容列，确保 Phase 1 历史 Hash 可逆。

### 4.3 顺序与回滚

Upgrade 按 Comparison → Profile 增量 → Context → Expected/Task Run 增量 → Trigger 执行；Downgrade 逆序移除新增约束、列、Trigger 和表。不得触碰 Phase 1–5 事实表和数据，迁移不得导入 Provider、Builder、FastAPI 或 Legacy 业务模块。

## 5. P6-02 当前实现

- 新增完整 `migrations/versions/0006_context_task_foundation.py`；
- ORM Metadata 与 0006 的四张新表、Task 表增量保持一致；
- `SQLAlchemyCandidateComparisonRepository` 支持首次发布、Content Hash 幂等、按 ID/Hash 读取和有序重建；
- 发布前校验 Feature/Universe/Recall/Regime 已发布且时点合法，所有成员属于绑定 Universe；
- 网络行情采集和外部计算不在 Repository 事务内；Pack/Member 在短事务中原子发布；
- Unit of Work 已接入 Candidate Comparison Repository；
- 修复 Coverage 输入为整数 `1` 时验证前后 Hash 不一致的问题，统一规范为 7 位小数；
- 未实现 Builder、READ API、Context/Task Repository 行为，未进入 P6-03。

## 6. 验证记录

- 本地隔离 PostgreSQL 16.13 已完成 `0005 → 0006 → 0005 → 0006`；历史 Profile Hash、Expected Run 和 Task Run 均保留；
- 全新 PostgreSQL 16 数据库完整回归：`217 passed, 5 skipped, 2 warnings`；
- 两项 Warning 为既有 Starlette/httpx 弃用提示和既有未 await 警告；
- 本机未安装 Ruff，未宣称 Ruff 通过；
- 尚需在服务器隔离 PostgreSQL 17 执行同等迁移往返、Repository 集成和完整回归；不对生产数据库执行 Migration。
- 2026-08-31 服务器复验：公网 TCP 22 可以建立连接，但 SSH 在返回协议 Banner 前持续超时或被远端重置；公网 `/health` 同时返回 HTTP 502。尚未登录服务器，未执行任何远端命令或数据库操作。

## 7. 下一步

服务器恢复 SSH Banner 和健康状态后，仅完成 P6-02 的隔离 PostgreSQL 17 验收。验收通过后更新本实施记录和工作状态，使用中文提交并推送，然后停止；不自动进入 P6-03。
