# V3 Phase 6 上下文与任务实施记录

> 最后更新：2026-08-31（Asia/Shanghai）
>
> 稳定基线：`main@9506983` / `v3-phase5-multi-recall-20260831`
>
> 工作分支：`codex/phase6-context-task`
>
> 当前状态：P6-01～P6-04 已完成；按用户授权继续 Phase 6 后续 Task

本文按 Phase 1–5 的实施记录方式集中保存 Phase 6 的契约、迁移、任务和验收状态，不建立 Phase Capsule、Context Policy 或多层 Task 治理。正式需求与设计仍以需求规格、架构设计、技术架构、数据库设计和详细设计为准。

## 1. 实施边界

Phase 6 建设 Candidate Comparison、FAST/NORMAL/DEEP Context Pack、Task Registry 和只读 JSON 接口。Phase 6 不实现 Phase 7 的 AI Result Import，不修改 Phase 3–5 已冻结的 Feature、Evidence、Recall 语义，也不修改 V1/V2、Provider、Scanner、评分或生产配置。

Task 仅作为 Phase 内可验证的小步开发单位；每个 Task 完成后停止，不自动继续下一项。

## 2. Task 状态

| Task | 内容 | 状态 | 证据/后续 |
|---|---|---|---|
| P6-01 | 冻结 Context、Candidate Comparison、Task Domain/Repository 契约和 0006 设计 | DONE | `044da58`；状态提交 `1585be0` |
| P6-02 | 完整 0006 DDL 与 CandidateComparisonPack/Member 不可变持久化 | DONE | `e669cac` 检查点；PostgreSQL 17.11 完整回归 `212 passed, 5 skipped` |
| P6-03 | Comparison Builder 与 N→TopK READ | DONE | `GET /api/v3/candidates/comparison-pack`；本地全量回归 `195 passed, 25 skipped` |
| P6-04 | FAST/NORMAL/DEEP Context Pack 与 Evidence Selection | DONE | 三级预算、Evidence Selection、不可变 Repository；本地全量 `198 passed, 25 skipped` |
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
- 服务器重启后 SSH 恢复；隔离 PostgreSQL 17.11 完成 `0005 → 0006 → 0005 → 0006`，4 张新表完整，旧 Profile Hash 在 Downgrade 后恢复，Expected Run/Task Run 样本全程保留；
- Candidate Comparison、迁移环境和契约专项测试：`12 passed`；
- 全新 `p6final` 数据库一次性完整回归：`212 passed, 5 skipped, 2 warnings`；两项 Warning 为既有 Starlette/httpx 弃用提示和既有未 await 警告；
- 首次全量尝试因临时镜像 `/data` 不可写失败 1 项；修正测试容器缓存路径后，复用已写入数据的数据库产生 2 项固定时点污染。最终验收改用全新数据库一次运行并全部通过，未修改业务代码掩盖测试环境问题；
- PostgreSQL 容器未发布宿主端口、未挂载生产目录；验收结束后已删除临时容器、网络、镜像、上传包和 `/tmp` 目录；
- 生产 `market-mcp` 全程保持 healthy，服务器本机 `127.0.0.1:8000/health` 与 Nginx `127.0.0.1/health` 均为 HTTP 200；未执行生产 Migration。

## 7. P6-03 实现与验证

- 新增 Comparison Builder，支持按 20–100 个 `CODE` / `MARKET:CODE` 候选一次构建紧凑横向对比包，并保留输入顺序；
- 所有候选绑定同一 Published Feature Run、Universe Snapshot，以及同一时点可用的 Recall Run 和 Market Regime；
- Feature、Recall Hit 与 Evidence 使用批量数据库读取；组装在事务外完成，最终使用短事务原子发布，Content Hash 重放返回既有 Pack；
- Member 输出 Recall、趋势、位置、波动、量价、流动性、基本面、风险、Evidence 和质量摘要；缺失保持 `UNKNOWN`/Missing；
- 明确裁剪分钟 K、深度 Evidence Payload 和统一 Final Score；服务端不计算 TopK，由 ChatGPT 完成 N→TopK；
- `GET /api/v3/candidates/comparison-pack` 支持按候选代码构建，也支持按 `candidate_set_id` 读取已发布 Pack；返回 Pack Hash 和全部版本/时点引用；
- 专项回归：`13 passed, 2 skipped, 2 warnings`；全量本地回归：`195 passed, 25 skipped, 2 warnings`。Skip 均为未配置本地 PostgreSQL/联网条件的既有可选测试，两项 Warning 为既有警告；本 Task 按用户要求不连接服务器。

## 8. P6-04 实现与验证

- 新增 FAST/NORMAL/DEEP 三级 Context Builder，预算分别为 3,000、6,500、12,000 Token，均位于冻结范围内；
- Security/Market Context 绑定同一时点可用的 Universe、Feature、Recall、Regime 和可选 Candidate Comparison；
- Evidence Retrieval 一次最多读取 200 条候选，按时点相关性、来源优先级、置信度、冲突和反方立场排序；保存候选 Evidence IDs、检索配置和最终 Selection 解释；
- FAST/NORMAL/DEEP 最多选择 8/20/40 条 Evidence；保存 Side、Reason、Retrieval Score、Relevance、Source Priority 和连续顺序；
- Raw Payload 不进入 Context；Normalized Payload 位于明确 `UNTRUSTED_DATA` 边界并限制字符串/集合尺寸；
- Portfolio 等 Phase 8 尚不可用事实明确标记 `UNKNOWN`，不伪造；真实持仓/交易/Stop/Cancel 的不可裁剪规则留待相应事实模块接入后执行；
- `SQLAlchemyContextPackRepository` 支持原子发布、ID/Hash 读取、有序 Selection 重建、时点和引用一致性验证；Unit of Work 已接线；
- 专项回归 `14 passed`；全量本地回归 `198 passed, 25 skipped, 2 warnings`。未连接服务器，PostgreSQL 17 集成验收按用户安排留待晚间执行。

## 9. 下一步

按用户授权继续 P6-05 Task Profile、Expected Run 与 Task Run Registry；保持独立提交。
