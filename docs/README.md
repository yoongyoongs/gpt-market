# gpt-market 文档索引

> 2026-09-10 文档归档整理：历史实施记录全部移入 `archive/`，
> 本索引标注每份文档**什么时候需要读**。接手新会话先看"当前状态层"。

## 一、当前状态层（每次接手/验收先读这些）

| 文档 | 什么时候读 |
|---|---|
| [工作状态](../工作状态.md)（仓库根目录） | **每次接手第一个读**——活账本，记录唯一当前事实 |
| [v3_r11_production_handoff_20260910](v3_r11_production_handoff_20260910.md) | **当前交接文档**——r11 生产部署状态、run_once 验收命令、§C~H 补验顺序、入口地址 |
| [v3_run_once_resource_optimization_report](v3_run_once_resource_optimization_report.md) | run_once 生产验收后**回填** §50 Production 实测值；了解资源治理设计 |
| [v3_candidate_engine_current_state](v3_candidate_engine_current_state.md) | 了解候选引擎现状审计结论（2026-09-09） |
| [功能清单与开发状态](功能清单与开发状态.md) | 查某功能开发到什么程度 |

## 二、开发参考层（写代码/部署时查）

| 文档 | 什么时候读 |
|---|---|
| [开发规范](开发规范.md) | 写代码前 |
| [deployment](deployment.md) | 部署/重建容器/迁移时 |
| [api-reference](api-reference.md) | 对接 API 时 |
| [testing-acceptance](testing-acceptance.md) | 组织测试与验收时 |
| [eastmoney_fields](eastmoney_fields.md) | 处理东财数据源字段时 |

## 三、设计基准层（新会话恢复上下文 / 设计回溯）

阅读顺序即编号：需求规格说明 → 需求追踪矩阵 → 系统功能架构 → 技术架构设计 → 数据库设计 → 详细设计 → 架构设计实施稿。

[需求规格说明](需求规格说明.md) · [需求追踪矩阵](需求追踪矩阵.md) · [系统功能架构](系统功能架构.md) · [技术架构设计](技术架构设计.md) · [数据库设计](数据库设计.md) · [详细设计](详细设计.md) · [架构设计实施稿](架构设计实施稿.md)

## 四、archive/（历史归档，只在回溯历史时读）

**什么时候需要**：查某轮改动为什么这么改、复盘历史决策、追溯 Phase N 或第 X 轮整改的具体落实内容。日常开发与验收**不需要**读。

| 分组 | 内容 |
|---|---|
| `archive/Phase1~11*.md`（12 份） | Phase 1~11 各阶段实施记录与技术验收（2026-08~09） |
| `archive/RT00/RT04-10` | 实时双流水线设计冻结与实施记录 |
| `archive/V3第X轮整改落实记录*.md`（5 份）+ 最终收口 + 外部验收阻断点修复 | V3 一~六轮整改与收口的历史落实记录 |
| `archive/gpt-market_V3_*.md`（4 份） | V3 总体修改方案设计稿 + 第二/四/五轮复验报告 |
| `archive/v3_candidate_engine_{implementation_report,round2_baseline,round2_1_hotfix_report}.md` | 候选引擎首轮实施报告、round2 基线、round2.1 hotfix 报告 |
| `archive/Baseline一致性修补摘要.md` | Baseline 修补历史 |
| `archive/v2/` | V2 时代文档 |
| `archive/v3-design-inputs/` | V3 设计输入材料 |
| `adr/0001` | 架构决策记录（Phase1 持久化） |

