# Phase 7–11 技术验收报告

> 验收日期：2026-09-01（Asia/Shanghai）
> 分支：`codex/phase11-stabilization`
> 结论：Phase 7–11 技术验收通过；生产 V3 保持关闭

## 验收环境

- 应用：Python 3.12 验收镜像；
- 数据库：独立 `postgres:17-bookworm` 容器；
- 网络：数据库只在服务器私有 Docker Network 内可达，未开放公网端口；
- 数据：每次最终套件前删除隔离 Schema 并从空库迁移，不连接生产库；
- 生产：未修改生产容器、Nginx、Feature Flag 或 V1/V2 数据。

## 阶段门禁

| Phase | 关键场景 | 结果 |
|---|---|---|
| 7 | 30 组中 29 成功、1 失败；组间隔离；Hash 漂移、跨组依赖和 AI 成交声明拒绝 | 通过 |
| 8 | 人工 Opening；100 股并发两笔 80 股 SELL；Projection 行锁与超卖拒绝 | 通过，1 成功/1 拒绝，余 20 股 |
| 9 | Action/Entry 无统一总分；无 Decision/Plan 的手工持仓 Review；建议不成交 | 通过，Review 1 条、Trade 0 条 |
| 10 | 七类归因契约；成熟时点；缺失回放输入；Regression 继承阻塞 | 通过，Replay/Case 均为 BLOCKED |
| 11 | AI 权限边界；Shadow 确定分组；Observation 派生容量；人审激活；故障回滚 | 通过，FAILED 健康事件回滚 V2 |

## Migration 与回归

- 空库 `base -> 0011_strategy_stabilization` 成功；
- `0011 -> 0006_context_task_foundation -> 0011` 往返成功；
- P7–P11 五张代表表在 Downgrade 后为 0、Upgrade 后为 5；
- `v3` Schema 检测到 66 个不可变 Trigger；
- P7–P11 专项：`17 passed`；
- 全项目 Python 3.12 + PostgreSQL 17：`243 passed, 5 skipped, 2 warnings`；
- 本地无数据库套件：`219 passed, 28 skipped, 2 warnings`；
- P7–P11 相关 Ruff 检查、`compileall` 和 `git diff --check` 通过。

5 个 Skip 为显式外部/联网条件用例。2 个 Warning 为既有 Starlette/httpx 弃用提示和 Feature Flag 测试中的未 await 警告，不是本轮失败。

## 验收修复

1. Canonical JSON 递归规范化 UUID 字典键和 Decimal，修复 Atomic Group Dependency 与 Position Projection Hash 崩溃；
2. Position Projection 增加输入 Hash 复核，篡改后的投影契约会被拒绝；
3. Release Environment 在 Repository 边界限制为 1–32 字符，超长路径参数不再下沉为 PostgreSQL `varchar` 错误；
4. 历史 P6 门禁收窄到 P6 自有路由，继续保证这些路由只读，同时允许后续 Phase 的显式写接口；
5. Alembic Head 和 Metadata 门禁更新到 Phase 11，并断言 P7–P11 核心表存在。

## 剩余边界

- 本报告是隔离技术验收，不是生产发布验收；
- 未启用生产 `V3_ENABLED`，未执行生产数据库 Migration；
- 未接券商、没有自动下单接口，PositionReview/Action/Entry 只产生建议事实；
- OCR 外部识别准确率、真实流量容量和生产 V3 读写性能需在生产发布计划中单独验收；
- Named Tunnel、稳定域名和企业级鉴权仍属于 Hardening，不纳入本轮 Architecture Gate。
