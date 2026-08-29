# ADR-0001：Phase 1 PostgreSQL 持久化与迁移选型

> 状态：Accepted
> 日期：2026-08-29
> 范围：V3 Phase 1 正式状态与时点基础

## 背景

现有 V1/V2 使用进程内缓存、SQLite Kline 和 JSONL Scan History。它们继续作为 Legacy 运行底座，但不能满足 V3 对不可变 Revision、`known_at`、事务、外键、幂等、审计和严格 Replay 的要求。V3 Baseline 要求 PostgreSQL 正式状态、显式 Migration、可回滚发布，以及与现有行情链隔离的 Repository 边界。

仓库审计确认：当前没有 PostgreSQL Driver、ORM、Migration、V3 Repository 或 Worker 基础；`Container` 只装配现有行情和基本面服务；Compose 只有一个只读根文件系统的 API 服务。

## 决策

1. PostgreSQL 使用 `postgres:17-bookworm`。生产和测试固定大版本，补丁版本由镜像更新管理；数据库不暴露公网。
2. Python 持久化采用 SQLAlchemy 2.0 async API，Driver 使用 `asyncpg`。
3. Migration 使用 Alembic async 模板。Migration 由发布步骤显式执行，API/Worker 启动时禁止自动抢跑 Migration。
4. Application 每个事务创建独立 `AsyncSession`；禁止跨并发任务共享 Session，也不使用全局 `async_scoped_session`。
5. Domain Entity、Value Object 和 Policy 不依赖 SQLAlchemy。Repository Protocol 位于内层，SQLAlchemy Model/Repository 位于 `app/v3/infrastructure/db`。
6. V3 使用独立 `app/v3`、独立配置和 `V3_ENABLED` Feature Flag。默认关闭时不创建 Engine、不连接 PostgreSQL，不改变 V1/V2 生命周期。
7. 数据库 URL 只来自环境变量；Alembic 配置文件不保存密码。Runtime 与 Migration 账户的权限分离在生产部署前落实。
8. Migration 必须支持空库 `upgrade head`、逐版本 downgrade 或明确恢复脚本，并在发布前执行备份与恢复验证。

## 依赖范围

```text
SQLAlchemy>=2.0,<2.2
asyncpg>=0.30,<1
alembic>=1.16,<2
```

范围允许兼容补丁升级，锁定前由测试验证。选择 SQLAlchemy 2.0 系列是为了使用稳定的 `create_async_engine`、`async_sessionmaker` 和类型化映射；Alembic 通过 Async Engine 的 `run_sync` 执行同步迁移核心。

## 目录边界

```text
app/v3/
├── contracts/
├── domain/
├── repositories/
└── infrastructure/db/
    ├── models/
    ├── repositories/
    └── session.py
migrations/
├── env.py
└── versions/
tests/v3/
```

首个 Migration 只承载 Phase 1 基础：Schema Version、Audit、Task/Agent Contract、不可变时点记录所需的最小表和约束。Universe、Bar、Feature、Recall、Trade 与 Portfolio 表按后续 Phase 增量加入，不提前创建空壳大表。

## 事务与生命周期

- Application Use Case 拥有事务边界；Repository 不自行提交。
- 一个 Atomic Group 对应一个数据库事务；Phase 1 先建立可复用事务基础，不提前实现 Phase 7 Import。
- Engine 在 V3 Container 启动时创建、关闭时 `dispose()`；`V3_ENABLED=false` 时整个 V3 Container 为禁用状态。
- Migration 使用独立命令执行；健康检查区分 Legacy 健康与 V3 数据库状态。

## 测试和验收

1. 默认测试不需要 PostgreSQL，V1/V2 全套测试必须继续通过；
2. Domain 时间、Hash、Contract 和状态不变量使用纯单元测试；
3. PostgreSQL 集成测试通过 Compose 启动隔离数据库；
4. 验证空库 Upgrade、Schema 约束、Downgrade/Restore 和重复执行；
5. `V3_ENABLED=false` 时数据库不可达也不影响现有 `/health` 和 V1/V2；
6. 只有集成证据齐全后，Phase 1 数据库能力才可标记为已实现。

## 回滚

- 代码回滚：关闭 `V3_ENABLED`，Legacy Runtime 不读取 V3 表；
- Schema 回滚：发布前备份，执行已验证的 Alembic downgrade；破坏性变更优先使用恢复而非猜测修复；
- 数据迁移：Legacy SQLite/JSONL 只复制到 V3，不原地修改；
- Phase 1 未验收前不合并 `main`、不部署生产服务器。

## 未决项

- Worker Scheduler 继续保持 `UNKNOWN`，Phase 1 只建立 Run Registry/接口边界；
- Runtime/Migration 数据库账户的具体权限 DDL 在服务器部署前验证；
- 备份工具和保留周期结合服务器环境形成单独运维 ADR。
