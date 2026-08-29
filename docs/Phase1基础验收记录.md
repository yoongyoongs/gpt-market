# V3 Phase 1 基础验收记录

> 验收日期：2026-08-29（Asia/Shanghai）
> 验收分支：`codex/phase1-foundation`
> 结论：技术验收通过，待用户确认后再决定是否合并和部署

## 1. 验收范围

本轮只验收《架构设计实施稿》Phase 1：PostgreSQL、Migration、正式时点、Agent Contract、Task、Audit 和 Repository 基础。未修改 V1/V2 评分规则、Scanner、Provider、MCP 和 Web 路由，也未进入 Phase 2 数据工程。

## 2. 运行环境

| 项目 | 实际环境 |
|---|---|
| 应用运行时 | Python 3.12 Docker 镜像 |
| 数据库 | PostgreSQL 17 bookworm |
| ORM/Driver | SQLAlchemy 2.0 async + asyncpg |
| Migration | Alembic async |
| 隔离方式 | 独立目录、独立 Docker Network、无宿主端口 |
| 生产影响 | `market-mcp` 未重启、持续 healthy |

## 3. Phase 1 输出核对

| 输出要求 | 状态 | 证据 |
|---|---|---|
| 持久化选型 ADR | 通过 | `docs/adr/0001-phase1-persistence.md` |
| PostgreSQL Compose | 通过 | `docker-compose.yml` 的 `v3` Profile |
| 初始 Migration | 通过 | `0001_phase1_foundation`，包含 9 张基础表 |
| Down/Restore | 通过 | Downgrade 到 `base` 后 schema 清空；`pg_dump` 恢复后表、版本、数据和触发器正常 |
| 时间与 Hash | 通过 | 时区感知契约、Canonical JSON/Hash、`known_at` 数据库约束 |
| Agent/Task 契约 | 通过 | AgentTask、AIResultEnvelope、组计数状态机及数据库约束 |
| Audit/Repository | 通过 | Repository Protocol、SQLAlchemy Adapter、Unit of Work、Task + Audit 同事务用例 |
| 不可变事实 | 通过 | Raw/Evidence/Task/Envelope/Audit 的 UPDATE/DELETE 触发器 |
| Legacy 回归 | 通过 | 完整测试套件通过；V3 默认关闭 |

## 4. 真实 PostgreSQL 结果

1. Upgrade 到 `0001_phase1_foundation` 成功，`v3` schema 下 9 张表齐全。
2. `known_at < fetch_time` 被数据库约束拒绝。
3. `expected_group_count != successful + failed + pending` 被数据库约束拒绝。
4. Raw Document UPDATE 被不可变触发器拒绝。
5. AgentTask 与 Audit 在同一 Unit of Work 提交，结果为 1 Task + 1 Audit。
6. 同一内容顺序重试不重复写 Task 或 Audit。
7. Audit 契约失败后 Task 未落库，事务整体回滚。
8. 相同 Task ID 携带不同内容被完整性约束拒绝，不冒充幂等成功。
9. 8 个并发事务提交相同任务，最终只有 1 Task + 1 Audit。
10. Downgrade、空库重建和 Restore 后，9 张表、Migration 版本、数据及不可变触发器均恢复正常。

可重复集成测试位于 `tests/v3/test_postgres_integration.py`，必须通过 `V3_TEST_DATABASE_URL` 显式连接真实 PostgreSQL；未配置时测试明确跳过。

## 5. 后续 Migration 计划

Phase 1 不提前创建尚未进入实施阶段的业务表。后续按 Phase 单独增加版本，禁止修改已发布的 `0001`：

| 计划版本 | 对应 Phase | 主要对象 |
|---|---:|---|
| `0002_market_data_foundation` | 2 | Universe、Raw Bar、Corporate Action、Factor Revision |
| `0003_feature_and_regime` | 3 | Feature Run、Published Snapshot、Market Regime |
| `0004_evidence_pipeline` | 4 | Entity Link、Conflict、Decay、Parser Run |
| `0005_recall_and_context` | 5–6 | Recall、Comparison、Context Pack、Expected Run 扩展 |
| `0006_ai_import_and_decision` | 7 | Import、Atomic Group、Decision、Review、EntryPlan |
| `0007_portfolio_ledger` | 8–9 | Trade Ledger、Adjustment、Portfolio、Position Review |
| `0008_performance_replay` | 10 | Performance、Replay、Regression、Recall Miss |

具体编号可因实际依赖拆分，但每个版本必须独立执行 Upgrade/Downgrade，并在合并前验证 Restore。

## 6. 已知边界

- 当前不是 V3 可用产品版本，没有新增 V3 API 或页面。
- PostgreSQL 尚未部署到生产，V3 Feature Flag 保持关闭。
- Secondary Universe Provider、严格交易日历和历史 Point-in-Time 精度仍按文档标记为 `UNKNOWN`，由后续 Phase 实测。
- Phase 7 的 Atomic Group Import、Phase 8 的 Trade Ledger 和后续业务审计尚未实现；本轮只提供可复用事务基础。

## 7. 回滚与进入下一阶段

当前生产回滚基线仍为 `origin/main@68884db`。用户验收前不合并 `main`、不部署 V3；用户确认 Phase 1 后，才创建 Phase 1 合并检查点并进入 Phase 2。
