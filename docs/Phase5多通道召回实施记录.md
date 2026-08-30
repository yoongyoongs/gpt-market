# Phase 5 多通道召回实施记录

> 日期：2026-08-31（Asia/Shanghai）
> 分支：`codex/phase5-multi-recall`
> 状态：数据基础、首批 Feature Channel、Run/Raw/Observation 和 READ 已完成；Evidence Channel 与全市场验收开发中

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
- `scripts/v3_phase5_recall.py` 最新/指定 Feature Run 执行与幂等重放。

## 2. 不变量

- Recall Result 只保存通道内 `strength/rank/reasons/matched_features/coverage`，不保存跨通道 Final Score。
- Raw Opportunity 是 Recall 命中并集，不是 Action Candidate，不产生买卖建议。
- Recall 未命中证券仍由 Full Universe Query 直接读取。
- `PENDING` 观察禁止出现未来价格、未来收益或超额收益。
- `MATURED` 观察必须到达成熟时点并包含可验证未来结果。
- 只有“未来表现优秀且当时未被召回”才是 Recall Miss，阈值必须绑定版本，窗口未成熟不得生成最终评价。
- 六张 Phase 5 表均为 append-only，修改或删除由 PostgreSQL Trigger 拒绝。

## 3. 当前验收

- PostgreSQL 17 `0004 -> 0005 -> 0004 -> 0005` 双往返通过；全新数据库从 `base -> 0005` 迁移通过；
- Domain/Channel/Run/Metadata 覆盖 Hash、完整通道计数、Raw 无 Final Score、未来泄漏拒绝、Miss 类型、失败隔离、全 Universe 观察和幂等；
- 真实 PostgreSQL 验证 2 只证券原子发布 2 Result、2 Raw、6 PENDING Observation，重复执行零新增，UPDATE 被 Trigger 拒绝；
- 成熟 Feature Job 探针 9 个通道中 6 个成功，周线/指数/行业输入缺失的 3 个通道明确 `UNAVAILABLE`；同输入连续执行返回同一 Recall Run；
- 未来 `as_of` Feature 被 Job 在写 Channel 前拒绝，不能伪造已知未来；
- 全新真实 PostgreSQL 的完整回归 `197 passed, 5 skipped`；2 条既有依赖/异步资源警告未由本阶段引入；
- 相关文件 Ruff 和 `git diff --check` 通过。

## 4. 待完成

1. 财务改善、业绩拐点和催化事件 Evidence Channel；
2. 未命中证券回归、真实全市场覆盖和性能验收；
3. Recall Miss 成熟 Job 的 Phase 5 基础接口与 Phase 10 边界复核；
4. 文档收口、合并与 Phase 5 稳定标签。

本记录不表示 Phase 5 或整个 V3 已完成。生产 V3 仍关闭，生产数据库尚未执行 `0005`。
