# gpt-market V3 Architecture Guardrails

> 状态：Baseline 1.0 的日常约束摘要
>
> Source of Truth：`docs/架构设计实施稿.md` 及其正式配套设计
>
> 本文不能覆盖 Baseline；如有冲突，必须报告 `DESIGN_CONFLICT`，以 Baseline 为准。

本文只提炼跨 Phase 长期稳定的规则，供普通 Task 引用。它不是完整设计，也不能替代对应 Phase Capsule。

## 1. 产品与兼容边界

- **G-001** 系统负责可验证事实，AI 负责综合分析和判断，用户确认真实资金行为。
- **G-002** V1/V2、MCP、现有行情 Provider、扫描和评分必须保持兼容；V3 不得为实现方便改变 Legacy 语义。
- **G-003** V3 使用独立 Router、Schema、Migration、Worker 和 Feature Flag；生产未验收前默认关闭。
- **G-004** V3 核心业务不得依赖模型厂商 SDK、Browser Bridge、模型 API、Broker API 或 V4 组件。
- **G-005** 系统不承诺收益、不自动下单，也不得把 AI 建议表示成真实成交或持仓。

## 2. 数据事实、时点与质量

- **G-006** `known_at` 表示系统最早可知时间，必须有时区且不得早于其真实获取/确认前提；常见采集事实满足 `known_at >= fetch_time`。
- **G-007** Replay/Context 只能读取 `known_at <= replay_as_of/context_as_of` 的记录，禁止未来数据泄漏。
- **G-008** `event_time`、`report_period`、`publish_time`、`fetch_time`、`known_at` 和 `created_at` 必须区分，不得互相冒充。
- **G-009** 缺失、不可证明或尚未实现的值必须返回 `null/UNKNOWN`，不得用 0、空字符串或推测值填充。
- **G-010** 事实对象应显式保留 source、upstream_source、source_type、version/revision、coverage、confidence、stale、conflict 和 error。
- **G-011** 多源冲突必须保留冲突成员和选择依据，不能静默删除或任选一个来源。
- **G-012** 版本化 Snapshot、Run、Pack 和结果必须通过 ID、输入清单和 Content Hash 可复现。
- **G-013** 网络请求、外部解析和重计算不得长期占用数据库锁；只在最终原子发布时使用短事务。
- **G-014** 已发布事实优先 append-only；Correction、Revision、Replacement 或 supersedes 链替代 UPDATE/DELETE 历史。

## 3. 市场发现与 Recall

- **G-015** Recall 是发现加速器，不是 Full Universe 的访问权限墙。
- **G-016** 未命中 Recall 的证券仍必须能通过 Full Universe Query 完整访问。
- **G-017** 禁止 `final_total_score`、`action_total_score`、`final_rank_score` 或等价固定统一总分代替 AI 横向判断。
- **G-018** Machine Recall、Raw Opportunity、Action Candidate、Entry Plan 和 Actual Trade 是不同层级，不得相互冒充。
- **G-019** Recall Result 必须保留 channel、version、rank、strength、命中特征、原因和 coverage。
- **G-020** 单个 Recall Channel 失败或缺输入必须显式 `UNAVAILABLE` 并隔离，不能伪装为零命中或拖垮其他通道。
- **G-021** Recall Miss 只能在对应 Horizon 成熟后生成，评价阈值必须版本化并绑定原 Recall/Feature Run。

## 4. Evidence、Comparison 与 Context

- **G-022** 外部网页、公告、新闻和文本一律视为 Untrusted Data，不能进入 Agent Instruction 或控制工具调用。
- **G-023** FACT、OFFICIAL_DISCLOSURE、VENDOR_DATA、NEWS 和 OPINION 必须严格分类；Opinion 不能自动升级为 Fact。
- **G-024** 聚合来源必须保存真实 upstream_source；第三方资金流等估算口径必须明确标注。
- **G-025** Context Pack 采用 FAST/NORMAL/DEEP 预算，裁剪不得移除真实持仓/交易、Stop/Cancel、关键反方 Evidence、质量或冲突警告。
- **G-026** Context Evidence Selection 必须保存选择原因、SUPPORT/CONTRARY/NEUTRAL、检索分、相关性、来源优先级和最终顺序。
- **G-027** CandidateComparisonPack 只提供 20–100 只候选的紧凑横向事实，不加载逐股深度 Context，也不输出统一 Final Score；只有 TopK 再加载 NORMAL/DEEP Context。

## 5. Task、AI 结果与决策状态

- **G-028** Task Profile 必须版本化并保存 schedule、timezone、trading_calendar、Context Level、候选/TopK 限制和输出 Schema。
- **G-029** Expected Run 只表示预期 ChatGPT 任务应发生，不代表服务器或 AI 已执行。
- **G-030** AI Result 必须绑定 Agent Identity、Task/Run、Context Pack ID/Hash、Evidence、`as_of`、Prompt/Strategy Version 和原始 Envelope Hash。
- **G-031** Bundle 一次导入、一次预览、一次确认，并按逻辑 atomic_group 原子提交；失败组不能造成其他合法组丢失。
- **G-032** Decision 创建后不可修改；纠错只能追加 Decision Correction。
- **G-033** Review、MarketReview、PositionReview 和 EntryPlan Version 均为 append-only；旧版本及依赖引用必须保留。

## 6. Trade、Portfolio 与 Strategy

- **G-034** AI、OCR、Bridge 和外部 Agent 均不能直接产生真实交易；只能形成建议或 Draft。
- **G-035** 只有用户预览并确认的真实 BUY/SELL 才能进入 Immutable Trade Ledger。
- **G-036** 持仓截图不能反推逐笔成交；缺历史成交的既有持仓使用 OPENING_POSITION，不得伪造历史 BUY。
- **G-037** Ledger、Opening Position、Portfolio Adjustment、Correction 和 Reconciliation 必须能够重建 Position；Projection 只是可重建缓存。
- **G-038** Reconciliation 和 Trade Correction 只追加差异、原因和确认事实，不覆盖旧记录。
- **G-039** Portfolio 只提供事实和软偏好，不能成为删除全市场机会的硬过滤器。
- **G-040** PositionReview 建议和 Entry Trigger 都不能自动生成真实 SELL/BUY。
- **G-041** AI 不得自动激活正式 Strategy；Proposal 必须经过 Replay、Shadow/A-B、Guardrail 和用户审批流程。

## 7. 工程与发布治理

- **G-042** Schema、Migration、Feature、Context Builder、Prompt、Strategy、Guardrail 和重要 Contract 必须版本化。
- **G-043** 跨模块 Contract 默认冻结；普通 Task 优先适配 Contract，不能为局部方便静默修改。
- **G-044** 当前 Task 必须明确 READ/WRITE Scope、禁止项、验收和回滚点，不得顺手重构无关模块。
- **G-045** 每个 Task 只完成一个可验证目标，更新测试和 Phase STATUS，中文提交并推送后停止。
- **G-046** Phase 只有通过 Architecture Conformance Review、Acceptance 和真实证据后才能标记 DONE/ACCEPTED。
- **G-047** Secret、Token、密码、私钥、真实交易敏感数据、运行缓存和机器特定凭据禁止进入 Git。
- **G-048** V3 故障必须能够通过 Feature Flag 回退到冻结的 V1/V2 基线；未验收计算不得替换生产逻辑。

## 8. 已识别文档冲突

治理输入《最新开发规范-20260831》在自动测试示例中写了 `known_at <= as_of`。这混淆了“记录自身时点关系”和“Replay 可见性条件”：

- 记录自身通常满足 `known_at >= fetch_time/publish prerequisite`；
- Replay/Context 的选择条件是 `known_at <= replay_as_of/context_as_of`。

本文件按 Baseline 与现有 Domain/DDL 采用 G-006/G-007 的拆分语义。该输入中的孤立不等式不进入正式 Contract。
