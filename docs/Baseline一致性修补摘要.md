# V3 Architecture Baseline 1.0 一致性修补摘要

> 本文只记录 `gpt-文档审查` 要求的最后一轮文档修补。没有修改业务代码、数据库 Migration、V1/V2 接口、评分、行情或部署。

## 1. 修补结论

V3 总体架构和 Phase 顺序保持不变，V4 边界保持不变；继续坚持“系统负责事实、AI 负责判断、用户确认真实资金行为”，不引入统一 Final Score。本轮消除了五处跨文档歧义，并补齐需求追踪基线。

## 2. 五项一致性修补

### 2.1 Independent PositionReview

- 新增独立 Append-only `position_reviews` 持久化设计；
- `decision_id/entry_plan_id` 可空，支持手工建仓且无 AI Decision 的真实持仓；
- 补齐 AI Import 映射、Position Context/Review READ API、数据库约束和测试场景；
- Review 建议不产生真实 SELL，资金变化仍须用户走 Trade 确认流程。

### 2.2 Portfolio Adjustment

- 市场级 `corporate_actions` 与账户级 `portfolio_adjustments` 明确分离；
- Adjustment 以不可变追加记录数量、现金和成本基础变化；
- 无法证明来源或账户影响时保持 `UNKNOWN/PENDING_RECONCILIATION`；
- Position Projection 由 Opening Position + 已确认 Adjustment + Trade/Correction 重建。

### 2.3 Task Run Partial Completion

- 增加 `PARTIAL_COMPLETED`；
- 增加 expected/successful/failed/pending group count；
- 全部必需组成功才是 `COMPLETED`，部分成功不得冒充全部完成，也不得被 MISSED 覆盖；
- Import Confirm 返回成功组、失败组和更新后的 Task Run，测试覆盖 29/30 成功情形。

### 2.4 Canonical EntryPlan Version

- EntryPlan 改为不可变 Canonical Version Chain；
- Decision 固定原始 Plan ID、Snapshot 和 Hash；
- Review/PositionReview 的修改通过新版本和 `supersedes` 关系表达；
- Execution Deviation 绑定成交实际采用版本；Performance 分开评价原始、修订和成交绑定版本。

### 2.5 Requirements Traceability Matrix

- 新增《需求追踪矩阵》；
- 覆盖《需求规格说明》全部 49 项 P0/P1；
- 每项映射到 Phase、模块、表/存储、API/Job、测试、当前状态和证据；
- 明确只有代码、测试、状态文档和提交证据同时具备时才能标记完成。

## 3. 已同步文档

- 《需求规格说明》：新增需求编号和验收条件；
- 《系统功能架构》：补齐领域职责、读取关系和资金事实流；
- 《技术架构设计》：补齐流水线、并发、不变量和可观测性；
- 《数据库设计》：补齐表、字段、关联、状态和事务约束；
- 《详细设计》：补齐导入映射、服务流程、API、Job 和测试；
- 《架构设计实施稿》：同步 Baseline 主正文、Phase 和验收标准；
- 《需求追踪矩阵》：建立实施和验收的唯一逐项索引；
- 《功能清单与开发状态》《工作状态》：只登记文档修补，所有 V3 功能仍按真实实现状态标记。

## 4. 未执行事项

- 未进入 Phase 1；
- 未创建数据库表或 Migration；
- 未实现 V3 API、Job、Provider 或 Domain；
- 未修改、部署或重启现有 V1/V2/MCP；
- 未把设计完成误标为功能完成。
