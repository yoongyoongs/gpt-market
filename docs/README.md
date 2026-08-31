# gpt-market 文档索引

## 接手必读

按以下顺序读取最小上下文：

1. [根 AGENTS](../AGENTS.md)及当前路径的局部 `AGENTS.md`；
2. [Architecture Guardrails](ARCHITECTURE_GUARDRAILS.md)；
3. [最小必要上下文规范](development/CONTEXT_POLICY.md)；
4. [当前工作状态](工作状态.md)指向的 Phase Capsule；
5. 当前 Task、直接相关代码和测试。

[V3 架构设计实施稿](架构设计实施稿.md)仍是最高 Source of Truth，但普通 Task 不再默认全文读取。需求、功能架构、技术架构、数据库和详细设计只在 Context Policy 的 Level 3 或 Phase Gate 中精确使用。

## 开发治理与 Phase Capsule

- [开发工作规范](开发规范.md)
- [Task 模板](development/TASK_TEMPLATE.md)
- [Phase Capsule 模板](phases/_template/SCOPE.md)
- [当前 Phase 6 Scope](phases/phase6/SCOPE.md)
- [Phase 6 Contracts](phases/phase6/CONTRACTS.md)
- [Phase 6 Acceptance](phases/phase6/ACCEPTANCE.md)
- [Phase 6 Status](phases/phase6/STATUS.md)

## 当前参考文档

- [API 与 MCP 工具参考](api-reference.md)
- [测试与验收规范](testing-acceptance.md)
- [Baseline 一致性修补摘要](Baseline一致性修补摘要.md)
- [V3 Phase 1 基础验收记录](Phase1基础验收记录.md)
- [V3 Phase 2 行情底座实施记录](Phase2行情底座实施记录.md)
- [V3 Phase 3 全市场特征实施记录](Phase3全市场特征实施记录.md)
- [V3 Phase 4 证据管道实施记录](Phase4证据管道实施记录.md)
- [V3 Phase 5 多通道召回实施记录](Phase5多通道召回实施记录.md)
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

保存 V3 蓝图、两轮 GPT 指导意见、最终文档审查和开发上下文治理输入原文。它们用于审计 Baseline 1.0 与治理规则的形成过程，不是独立实施依据。若其内容与架构实施稿冲突，以架构实施稿为准。

## 文档维护规则

- 当前架构只有 `架构设计实施稿.md` 是有效 Baseline；
- 项目级当前进度更新 `工作状态.md`；普通 Task 细节只更新当前 Phase `STATUS.md`；
- 每完成一个开发 Task，必须同步 Phase STATUS、中文提交并推送 GitHub，然后停止；
- 历史文档只移动归档，不随意删除；
- 探针和验收 JSON 是历史样例，不能作为当前行情或投资事实。
