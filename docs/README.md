# gpt-market 文档索引

## 接手必读

按以下顺序阅读：

1. [项目 README](../README.md)
2. [当前工作状态](工作状态.md)
3. [V3 需求规格说明](需求规格说明.md)
4. [P0/P1 需求追踪矩阵](需求追踪矩阵.md)
5. [V3 系统功能架构](系统功能架构.md)
6. [V3 技术架构设计](技术架构设计.md)
7. [V3 数据库设计](数据库设计.md)
8. [V3 详细设计](详细设计.md)
9. [V3 架构设计实施稿](架构设计实施稿.md)
10. [功能清单与开发状态](功能清单与开发状态.md)
11. [开发工作规范](开发规范.md)
12. 当前任务相关代码与测试

换电脑、换模型或新会话时，以这套 Baseline 文档和当前代码恢复上下文。归档材料不是必读项；数据库/详细设计可以在进入相应 Phase 时重点阅读，但不得绕过需求和功能架构。

## 当前参考文档

- [API 与 MCP 工具参考](api-reference.md)
- [测试与验收规范](testing-acceptance.md)
- [Baseline 一致性修补摘要](Baseline一致性修补摘要.md)
- [V3 Phase 1 基础验收记录](Phase1基础验收记录.md)
- [V3 Phase 2 行情底座实施记录](Phase2行情底座实施记录.md)
- [服务器部署记录](deployment.md)
- [东方财富字段实测记录](eastmoney_fields.md)
- `eastmoney_probe.json`：带时间戳的字段探针样例；
- `acceptance_results.json`：带时间戳的验收样例。

这些文档用于处理现有 V1/V2 服务、部署或排障，按任务需要读取，不属于 V3 架构接手的默认阅读集合。

## 双格式规则

根 README、CONTRIBUTING 和 `docs` 下每份 Markdown 都必须有同目录、同名 HTML：Markdown 供大模型和版本审查，HTML 供人阅读。执行：

```bash
python scripts/build_docs_html.py
python scripts/build_docs_html.py --check
```

HTML 由 Markdown 自动生成，Markdown 是内容源；禁止只手工修改 HTML。

## 历史归档

### `archive/v2/`

保存 V1/V2、Multi-Provider、K 线缓存、Opportunity Scanner、Phase2A 等历史设计和验收材料。当前代码仍可能包含这些能力，但新开发应以代码、测试和 V3 架构基线为准；只有追溯历史行为时才读取。

### `archive/v3-design-inputs/`

保存 V3 蓝图、两轮 GPT 指导意见和最终文档审查原文。它们用于审计 Baseline 1.0 的形成过程，不是独立实施依据。若其内容与架构实施稿冲突，以架构实施稿为准。

## 文档维护规则

- 当前架构只有 `架构设计实施稿.md` 是有效 Baseline；
- 当前进度只更新 `工作状态.md`，不要复制日期版状态文件；
- 每完成一个开发步骤，必须同步状态、中文提交并推送 GitHub；
- 历史文档只移动归档，不随意删除；
- 探针和验收 JSON 是历史样例，不能作为当前行情或投资事实。
