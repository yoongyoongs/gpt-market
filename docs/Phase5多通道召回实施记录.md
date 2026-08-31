# Phase 5 多通道召回实施记录

> 日期：2026-08-31（Asia/Shanghai）
> 分支：`codex/phase5-multi-recall`
> 状态：Phase 5 技术验收完成；生产 V3 仍保持关闭

## 1. 范围

Phase 5 按《架构设计实施稿》实现 Multi-Recall、Raw Opportunity、未来表现观察和 Recall Miss 基础，不修改 V1/V2 评分，不增加自动交易，也不建立统一 Final Score 或 Recall 权限墙。

首个检查点新增：

- `0005_multi_recall_foundation`；
- 不可变 `recall_channels/recall_runs/recall_results`；
- 只表达多通道命中事实的 `raw_opportunities`；
- T+3/T+5/T+10 `performance_observations`；
- 绑定阈值版本的 `recall_miss_evaluations`。
- 9 个 Feature Channel：低位转强、趋势启动、首次突破、首次回踩、周底日强、相对指数、相对行业、量能扩张和异常组合；
- 单通道失败/缺字段隔离，`UNAVAILABLE` 不伪装为零命中；
- 原子发布、确定性通道内排名、Raw 命中并集和全 Universe T+3/5/10 PENDING Observation；
- `GET /api/v3/recalls` 与 `GET /api/v3/raw-opportunities` 稳定 Cursor READ；
- PENDING Observation 保持不可变，MATURED/UNAVAILABLE 通过 `supersedes_observation_id` 追加形成修订链；
- `MatureRecallObservationsService` 在事务外读取 Outcome Provider，事务内只追加终态 Observation 和绑定阈值版本的 Recall Miss Evaluation；
- `GET /api/v3/recalls/misses` 支持阈值版本、仅漏召回过滤和稳定 Cursor READ；
- `scripts/v3_phase5_recall.py` 最新/指定 Feature Run 执行与幂等重放。
- 财务改善、业绩拐点和催化事件 3 个 Evidence Channel：分别只比较同报告期跨年财务、明确同比字段和近 30 日官方公告事件词，并返回 Evidence ID。

## 2. 不变量

- Recall Result 只保存通道内 `strength/rank/reasons/matched_features/coverage`，不保存跨通道 Final Score。
- Raw Opportunity 是 Recall 命中并集，不是 Action Candidate，不产生买卖建议。
- Recall 未命中证券仍由 Full Universe Query 直接读取。
- `PENDING` 观察禁止出现未来价格、未来收益或超额收益。
- `MATURED` 观察必须到达成熟时点并包含可验证未来结果。
- PENDING 不得 UPDATE 为终态；一个 PENDING 最多被一个 MATURED 或 UNAVAILABLE 记录替代，终态必须明确引用原 PENDING。
- 只有“未来表现优秀且当时未被召回”才是 Recall Miss，阈值必须绑定版本，窗口未成熟不得生成最终评价。
- 六张 Phase 5 表均为 append-only，修改或删除由 PostgreSQL Trigger 拒绝。

## 3. 当前验收

- PostgreSQL 17 `0004 -> 0005 -> 0004 -> 0005` 双往返通过；全新数据库从 `base -> 0005` 迁移通过；
- Domain/Channel/Run/Metadata 覆盖 Hash、完整通道计数、Raw 无 Final Score、未来泄漏拒绝、Miss 类型、失败隔离、全 Universe 观察和幂等；
- 真实 PostgreSQL 验证 2 只证券原子发布 2 Result、2 Raw、6 PENDING Observation，重复执行零新增，UPDATE 被 Trigger 拒绝；
- 成熟 Feature Job 探针 9 个通道中 6 个成功，周线/指数/行业输入缺失的 3 个通道明确 `UNAVAILABLE`；同输入连续执行返回同一 Recall Run；
- 未来 `as_of` Feature 被 Job 在写 Channel 前拒绝，不能伪造已知未来；
- 全新真实 PostgreSQL 的完整回归 `197 passed, 5 skipped`；2 条既有依赖/异步资源警告未由本阶段引入；
- 12 通道真实 Job 探针成功发布：2 股夹具中 6 个 Feature 通道成功，周线/指数/行业及三类 Evidence 因确实缺输入明确 `UNAVAILABLE`，Coverage `0.5`；
- Evidence Channel 专项验证跨年同报告期、预告/快报同比、官方催化词和普通公告不命中；第三个全新 PostgreSQL 库完整回归 `199 passed, 5 skipped`；
- 相关文件 Ruff 和 `git diff --check` 通过。
- 本地新增成熟链、Outcome 完整性、收益一致性、幂等和漏召回分类测试；不配置真实 PostgreSQL 的完整回归为 `184 passed, 23 skipped`。
- 修改后的 `0005` 已在服务器隔离 PostgreSQL 17 完成 `0003 -> 0005 -> 0003 -> 0005` 双向迁移；5,551 条 Feature 全程保留，六张 Phase 5 表创建完整。
- 独立空库从 `base -> 0005` 后执行完整回归：`202 passed, 5 skipped`；两条既有依赖/异步资源警告保留。真实 PostgreSQL 专项用例同时通过追加终态、Recall Miss READ、未召回证券仍由 Full Universe Query 返回和成熟重放零新增。
- 真实 5,551 证券 Feature Run 执行 12 通道：6 个成功、6 个因输入确实缺失明确 `UNAVAILABLE`，发布状态 `PUBLISHED`，通道 Coverage `0.5`，命中 3,492 只。
- 全市场 Run 生成 5,726 条 Recall Result、3,492 条 Raw Opportunity 和 16,653 条全 Universe PENDING Observation；首次耗时 14.971 秒，第二次耗时 3.539 秒并返回同一 `recall_run_id=8866cd57-f68e-4ffd-9275-83f1e55ce4f1`，数据库只保留一个 Run。
- 2,059 只未命中证券仍存在于 5,551 条 Full Universe Query；抽样 `BJ 920001 纬达光电` 可读取，证明 Recall 不是 Universe 权限墙。
- READ 20 次服务器实测 P95：Feature 12.273ms、Raw 11.858ms、Recall Result 10.600ms、Recall Miss 1.081ms；所有接口均为数据库读取，没有触发行情采集。
- 验收期间生产 `market-mcp`、Nginx 本机入口和公网 `106.13.171.166:80` 健康检查均为 HTTP 200；隔离数据库未挂载生产，V3 未启用。
- Phase 5 只定义 Outcome Provider 契约和基础漏召回分类；严格同口径未来收益、MFE/MAE、Target/Stop 与七类归因仍属于 Phase 10，禁止用不一致复权价格冒充严格评价。

## 4. 验收边界与后续

1. Phase 6 开始实现 Candidate Comparison Context、Context Pack、Task 和 READ JSON；
2. Phase 10 再选择并验证 point-in-time-safe Outcome Provider；若同口径收益无法证明则追加 `UNAVAILABLE`，不得伪造；
3. 生产 PostgreSQL Migration 和 V3 启用仍须等待后续 Phase 与最终部署验收，本轮不部署 V3。

Phase 5 技术验收通过，但不表示整个 V3 已完成。生产 V3 仍关闭，生产数据库尚未执行 `0005`。
