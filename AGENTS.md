# gpt-market 开发导航

本仓库以 `docs/架构设计实施稿.md`（V3 Architecture Baseline 1.0）为最高设计依据。

开始任何开发任务前必须按顺序读取：

1. 本文件及当前路径下更近的 `AGENTS.md`；
2. `docs/ARCHITECTURE_GUARDRAILS.md`；
3. `docs/development/CONTEXT_POLICY.md`；
4. `docs/工作状态.md` 指向的当前 Phase Capsule；
5. 当前 Task 及其直接相关代码、契约和测试。

普通 Task 使用最小必要上下文，禁止无目的扫描整个仓库或全文读取所有大型设计文档。信息不足时按 Context Policy 逐级扩大范围，不得猜测接口、字段、Schema 或历史语义。

每个 Task 必须明确 READ/WRITE Scope、禁止项和验收条件；跨模块 Contract 默认冻结。发现冲突时报告 `DESIGN_CONFLICT`，确需改变正式设计时报告 `DESIGN_CHANGE_REQUIRED`，未经确认不得静默扩大范围。

每个 Task 只完成一个可验证目标：更新对应测试和 Phase `STATUS.md`，使用中文提交并推送，然后停止。不得自动进入下一 Task。

禁止提交 Secret、Token、密码、真实交易敏感数据、运行缓存和机器特定凭据。生产 V3 默认关闭；不得破坏 V1/V2、MCP、行情、评分和现有 API 兼容性。
