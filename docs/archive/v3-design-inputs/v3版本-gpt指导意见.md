# V3 版本 GPT 指导意见（历史设计输入）

> 归档说明：本文是 Baseline 1.0 的第一轮 Review 输入，不是当前独立实施依据。

基于刚刚已经完成的《gpt-market V3 AI投资决策系统总体架构设计》，请继续做一次V3架构补充和收敛，形成“V3设计稿初稿”。

重要：本轮仍然只做设计，不修改代码、不提交、不部署、不自动进入实施。

当前报告总体方向认可，不推翻已有设计。继续保留：
- V1/V2作为baseline和A/B对照；
- 现有MarketDataService、ProviderManager、行情/K线缓存、Phase2A基本面等底座；
- Full Market Features；
- Multi-Recall；
- Evidence；
- Context Pack；
- Watchlist；
- Decision；
- Review；
- Performance；
- Strategy Version；
- Raw / Action / Entry分层；
- known_at防前视；
- PostgreSQL正式状态存储；
- AI Gateway结构化JSON接口。

但结合后续讨论，需要对V3补充以下设计。

====================
一、先明确V3和V4边界
====================

V3目标是先做出一个真正可长期使用的“AI投资决策闭环”，当前主要AI客户端仍然是ChatGPT Plus网页版。

V3不要依赖：
- OpenAI API；
- Claude/Gemini/DeepSeek等API；
- 浏览器Bridge；
- ChatGPT自动POST服务器；
- 多模型自动辩论；
- 自动修改正式策略。

这些属于未来V4或V3之后的扩展能力。

V3必须保证：
即使现在只有ChatGPT网页版，我仍然能够使用服务器数据完成：
全市场发现 → 深度分析 → 自选 → 等买点 → 真实成交录入 → 持仓分析 → 卖点判断 → Review → Performance → 历史复盘。

请在设计稿最前面增加：
V3 Scope
V4 Deferred Scope
明确每一项为什么属于V3或V4。

建议V4再实现：
1. 浏览器Bridge自动同步ChatGPT网页输出；
2. ChatGPT直接WRITE服务器；
3. 真正的LLM Gateway自动调用模型API；
4. OpenAI/Claude/Gemini/DeepSeek/Qwen/GLM Adapter；
5. Model Router；
6. 多模型Second Opinion；
7. Bull/Bear多Agent辩论；
8. 无人值守AI任务；
9. 更高级自动Strategy Proposal/Replay/Shadow流程。

但V3数据结构和Contract必须为这些未来能力预留兼容性。

====================
二、Multi-Recall不能成为新的“420门槛”
====================

当前V2最大问题之一，是全市场经过单一预筛后只有420只进入深度计算。

V3的Multi-Recall虽然方向正确，但必须新增一条硬原则：

“Recall是机会发现加速器，不是GPT访问全市场股票的权限墙。”

必须提供完整的Full Market Feature Query能力，让ChatGPT可以直接查询5000+全市场轻量特征。

例如未来应该支持：
- 250日位置<50%，最近5日量能开始扩张；
- MA20最近由下降转走平/上行；
- 最近5/10日相对行业强度明显提高；
- 20日涨幅不高但日K刚转强；
- 没有进入任何Recall、但多个异常特征同时改善；
- 某行业全部股票横向比较；
- 按trend_freshness / ignition / position / relative_strength等任意组合筛选。

设计：
GET /api/v3/universe/features
GET /api/v3/universe/query
支持分页、排序、过滤、字段选择。

同时增加：
recall_miss

定义：
后来T+3/T+5/T+10表现明显优秀，但当时没有被任何Recall通道命中的股票。

Performance模块必须能统计：
recall_hit_rate
recall_miss_rate
各Recall Channel漏掉了哪些后续优秀机会。

这用于避免未来再次漏掉类似601233历史案例。

====================
三、Full Market Features数据底座必须真正覆盖全市场
====================

当前审计显示，本地SQLite只有约416只股票有day/week历史K线，并不是真正5000+全覆盖。

V3必须把“全市场轻量特征”建立在真正足够完整的历史行情基础上。

设计：
1. 所有A股逐步补齐至少250~300交易日日K；
2. 日K统一复权口径；
3. 日K以后每日只增量更新；
4. 周K优先由统一日K本地聚合；
5. 月K优先由统一日K本地聚合；
6. 5m/15m/60m不做全市场长期持续采集，只服务：
   - Action候选；
   - AI Watchlist；
   - 实际Portfolio；
7. 冷启动允许分批、断点续跑；
8. Full Feature计算必须版本化和可重放。

基础全市场Feature至少设计：
return_3d
return_5d
return_10d
return_20d
return_60d
return_120d
return_250d
position_60d
position_120d
position_250d
MA5/10/20/60
MA slope
ATR / ATR%
volatility
amount
turnover
volume_ratio
volume_expansion
distance_to_high/low
relative_index_strength
relative_industry_strength
week_state
day_state
trend_freshness
breakout_state
pullback_state
overheat_state
liquidity_quality

明确：
哪些是5000+全市场每天计算；
哪些只为深度候选计算。

====================
四、Universe Provider不能只有Eastmoney一个真实来源
====================

当前报告发现完整Universe主要只有Eastmoney，属于单点风险。

V3设计：
Primary Universe Provider
Secondary Universe Provider
Last-Known-Good Local Universe

Universe Snapshot必须保存：
source
known_at
fetch_time
coverage
diff_from_previous
new_listing
delisted
suspended
risk_flag

如果所有实时Universe源失败：
允许使用最近成功Universe作为基础，
但必须明确：
stale=true
source=LAST_KNOWN_GOOD

不要静默认为是最新Universe。

====================
五、ChatGPT网页版是V3当前主要AI客户端
====================

当前不购买其他模型API，不影响V3使用。

V3当前：
agent_type = CHATGPT_WEB

服务器负责：
- 市场事实；
- Evidence；
- Context Pack；
- Task Profile；
- Watchlist；
- Decision；
- Review；
- Portfolio；
- Trade Ledger；
- Performance。

ChatGPT网页版负责：
- 全市场二次筛选；
- 股票横向比较；
- 正反Evidence分析；
- Action判断；
- Entry判断；
- 持仓分析；
- 卖点判断；
- Review；
- 复盘。

当前不要假设ChatGPT Plus网页版可以稳定携带Bearer直接POST。

因此V3的ChatGPT交互重点先放在：
可靠READ + 标准结构化输出。

服务器提供READ：
GET /api/v3/market-overview
GET /api/v3/universe/features
GET /api/v3/universe/query
GET /api/v3/recalls
GET /api/v3/raw-opportunities
GET /api/v3/stocks/{code}/context-pack
GET /api/v3/stocks/{code}/evidence
GET /api/v3/watchlist
GET /api/v3/watchlist/changes
GET /api/v3/portfolio
GET /api/v3/portfolio/{code}/context
GET /api/v3/decisions
GET /api/v3/reviews
GET /api/v3/performance
GET /api/v3/task-context/{profile}
GET /api/v3/cases/similar

机器接口全部JSON。
HTML页面只做人类查看，不作为主要AI数据接口。

====================
六、V3暂时不用浏览器Bridge
====================

浏览器Bridge明确放到V4。

V3不要设计依赖：
Chrome Extension
ChatGPT DOM监听
自动抓网页回答
自动POST服务器。

但V3需要设计一个简单、可落地的“AI结果导入”机制。

ChatGPT输出统一结构：
DecisionResult
ReviewResult
WatchlistProposal
EntryPlanResult
PositionReviewResult

网站增加：
“导入AI分析”

用户一次粘贴/上传ChatGPT结构化结果后，服务器完成：
Schema校验
Context Pack校验
保存Decision/Review
更新AI Watchlist状态
保存Entry Plan
建立Evidence关联

这些操作必须在一次导入中完成，不要让用户：
先导入分析
再手工去自选页加股票
再手工填Entry Plan。

一次确认完成整套低风险记录。

未来V4接入Bridge或MCP后，直接复用同一个Ingestion API。

设计：
POST /api/v3/ai-results/import

输入统一AI Result Envelope。

====================
七、增加Task Run Registry，防止定时分析静默缺失
====================

当前ChatGPT网页版主要靠：
用户手动触发
ChatGPT Scheduled Tasks

但V3暂时无法保证定时任务自动写回。

因此服务器必须知道“理论上应该发生哪些分析”。

设计：
task_profiles
task_runs
expected_runs

例如：
PRE_MARKET
OPEN_1000
MID_MORNING
AFTERNOON
CLOSE
POST_MARKET
WATCHLIST_REVIEW

每次预计任务有：
expected_at
profile_version
strategy_version
status

状态：
EXPECTED
COMPLETED
PENDING_IMPORT
MISSED
CANCELLED

如果ChatGPT已经跑过但结果未导入：
显示“待同步”。

如果超过时限没有结果：
显示“MISSED”。

不能因为某天用户忘记导入就静默形成历史空洞。

====================
八、Context Pack改成三级，而不是默认14K
====================

现有设计14K作为最大深度分析预算可以保留，但不要所有任务都默认14K。

设计三个级别：

FAST：
2k~4k tokens
用于：
盘中快速Review
Watchlist状态检查
持仓异常检查

NORMAL：
5k~8k
用于：
常规股票分析
盘前/盘后重点候选

DEEP：
10k~14k
用于：
首次Decision
重大基本面变化
准备实际交易
持仓重大变化
深度复盘

Task Profile决定Context Level。

历史数据无限保存，但Context永远有限。

====================
九、Evidence继续强化“事实 vs 观点”
====================

Evidence必须支持：
实时行情
K线
财务
公告
业绩
行业
国内政策
海外宏观
地缘局势
商品价格
财经新闻
券商观点
专家观点

严格分类：
FACT
OFFICIAL_DISCLOSURE
VENDOR_DATA
NEWS
OPINION

专家/券商观点不能当事实。

Evidence至少保存：
evidence_id
source
upstream_source
source_type
event_time
publish_time
fetch_time
known_at
confidence
relevance
decay
expire_at
raw_reference
conflict_state

Decision必须保存真正使用过的Evidence ID。

Replay严格：
known_at <= replay_as_of

====================
十、新增真正的Portfolio + Trade Ledger
====================

这是V3必须加入，不放到V4。

因为系统必须知道：
“GPT建议买”
和
“用户真的买了”
完全不同。

设计两个概念：

EntryPlan
= AI建议的交易计划。

ActualTrade
= 用户真实成交。

只有真实成交确认后：
Watchlist/Entry状态才能进入HOLDING。

不能因为GPT说“可以买”就认为用户持仓。

Trade Ledger必须Immutable。

记录每一笔：
trade_id
account_id
code
side BUY/SELL
trade_time
price
quantity
fee
source
source_reference
confidence
decision_id
entry_plan_id
created_at

禁止只维护：
position.quantity
position.avg_cost

当前持仓必须由Trade Ledger计算。

====================
十一、真实成交录入支持两种V3方式
====================

V3先实现：

A. 网站快速手工录入
用户填写：
股票
BUY/SELL
成交价格
数量
时间

系统自动关联：
Decision
Entry Plan
Watchlist
当时Market Snapshot

B. 券商持仓/成交截图导入

设计：
POST /api/v3/trades/import-image

上传持仓截图或成交截图。

识别：
股票代码/名称
持仓数量
成本价
买卖方向
成交价
成交数量
时间
盈亏等可识别字段。

识别后只能生成Trade Draft。

必须显示：
识别结果
confidence
原始图片reference

用户一次确认后才正式写Trade Ledger。

截图OCR/视觉识别不能直接改变真实持仓。

券商API/自动对账单导入放V4或未来。

====================
十二、Portfolio Context必须给GPT做卖点分析
====================

设计：
GET /api/v3/portfolio
GET /api/v3/portfolio/{code}/context

至少返回：
真实持仓数量
平均成本
已实现收益
未实现收益
真实成交历史
持有交易日
原始Decision
原Entry Plan
最新价格
周K
日K
60m
15m
5m
行业状态
最新基本面
最新公告/新闻
最新风险
原stop/target
当前support/resistance
time_efficiency
最新Review

GPT以后分析：
继续持有
减仓
止盈
止损
上移止损
退出
不能只看股票本身，要结合用户真实成本和持仓历史。

====================
十三、Performance进一步拆分能力
====================

原来的：
选股能力
择时能力
持仓管理

继续细化成：

1. Stock Selection Quality
选中的股票后来表现如何。

2. Initial Entry Quality
首次买点是否合理。

3. Add Position Quality
加仓是否合理。

4. Reduce / Take Profit Quality
减仓/止盈质量。

5. Final Exit Quality
最终退出是否合理。

6. Risk Control Quality
止损/取消条件是否有效。

统计：
T+1/3/5/10/20
benchmark excess return
MFE
MAE
time_to_target
direction_correct
entry_triggered
actual_trade
target_hit
stop_hit
exit_efficiency

股票后来上涨但用户没买：
只算Selection，不算Trade PnL。

Entry满足但用户没成交：
不能变成HOLDING。

====================
十四、V3必须Model-Agnostic，但现在不调用模型API
====================

这是架构设计要求，但不要为了未来API把V3做复杂。

现在定义统一Agent Contract：

AgentTask
ContextPack
ToolContract
DecisionResult
ReviewResult
PositionReviewResult
AIResultEnvelope

业务层只依赖这些Schema。

不能把：
OpenAI SDK
Claude SDK
Gemini SDK

写进核心业务。

Decision/Review保存：

agent_type
provider
model
model_version
prompt_version
strategy_version
task_profile
context_pack_id
context_pack_hash
trigger_type
analysis_profile

当前：
agent_type=CHATGPT_WEB
provider=OPENAI
如果网页版无法确认具体模型：
model=UNKNOWN
禁止猜测。

未来V4再实现：

LLMGateway
LLMAdapter

例如：
OpenAIAdapter
AnthropicAdapter
GeminiAdapter
DeepSeekAdapter
QwenAdapter
GLMAdapter
LocalModelAdapter

但V3只需要定义Contract和扩展接口，不需要真正调用这些模型。

====================
十五、Raw / Action / Entry继续严格分层
====================

V3必须彻底避免V2的问题：

“结构安全 + RR漂亮”
不等于
“未来几天最值得买”。

Machine Recall：
哪些值得进一步研究。

Raw Opportunity：
哪些股票整体值得研究。

Action Candidate：
未来约3~15交易日哪些最值得占用资金机会槽。

Entry Plan：
当前是否到了可以执行的位置。

Action必须重点考虑：
trend_freshness
ignition_quality
flow_confirmation
time_efficiency
relative_strength
industry_confirmation
fundamental_quality
catalyst
risk

避免：
机场
银行
港口
低波动慢股
仅仅因为位置低和RR漂亮长期霸榜。

====================
十六、expected_horizon和time_efficiency作为V3核心字段
====================

至少支持：
1~5 trading days
3~10
10~20
20~60

Raw可以保留慢机会。

但Action优先级必须考虑资金占用效率。

例如：
深圳机场这种结构安全但长期无动作的股票，可以：
Raw较高
Action较低
expected_horizon较长。

而类似历史601233：
刚启动
趋势新鲜
量价改善
存在第一次回踩机会

应明显提高Action优先级。

====================
十七、601233作为回归案例，但禁止假回测
====================

保留当前报告正确处理：

601233：
zone 22.45~22.70
stop 21.65
target1 24.90
高开>3%不追
化纤板块不得明显转弱

但是由于原始as_of和Evidence缺失：
当前Replay仍标：
BLOCKED_BY_MISSING_POINT_IN_TIME_DATA

不能利用后来上涨证明当时模型应该选中。

后面如果补齐资料，再启用严格时点Replay。

同时V3从上线第一天开始自动积累新的Regression Cases，
以后不再出现历史案例缺乏当时时点数据的问题。

====================
十八、Decision必须保存完整AI身份和上下文
====================

Decision新增：
assistant_model / model
agent_type
provider
analysis_profile
task_profile
trigger_type
task_run_id
prompt_version
strategy_version
context_pack_id
context_pack_hash
feature_version
recall_run_id
created_at

Decision保持Immutable。

Review只能追加。

====================
十九、Strategy Guardrail仍然保留，但标记initial
====================

现有设计中的：
Recall≥200
Entry≥60
不同市场环境
20/60/120滚动窗口
Shadow周期
单次参数变化限制

继续保留作为：
initial_guardrail

必须配置化和版本化。

不能硬编码为永久科学标准。

V3仍然禁止AI自动激活正式策略。

====================
二十、V3低风险状态和真实资金事实严格分开
====================

AI可以产生：
WATCHING
WAIT_ENTRY
SLOW
DOWNGRADED
INVALIDATED
Analysis Draft
Review
Strategy Proposal

但以下事实必须用户确认：
实际BUY
实际SELL
实际持仓
实际加仓
实际减仓
正式Strategy Activation

AI永远不能根据自己的建议自动生成真实持仓。

====================
二十一、请重新整理V3实施Phase
====================

不要机械沿用旧Phase顺序，请结合上述补充重新规划。

建议你评估类似：

Phase 0：
冻结V2/Phase2A baseline

Phase 1：
PostgreSQL + Observation/Evidence/known_at + Agent Contract + Audit基础

Phase 2：
全市场日K补齐 + Full Market Features

Phase 3：
Multi-Recall + Full Universe Query + Raw Opportunity

Phase 4：
Context Pack + Task Profile + ChatGPT READ JSON API

Phase 5：
AI Result Import + Watchlist + Decision + Review

Phase 6：
Trade Ledger + Portfolio + 截图导入

Phase 7：
Action / Entry / Portfolio Review

Phase 8：
Performance / Replay / Regression

Phase 9：
Strategy Shadow/A-B与V3稳定化

但请根据当前真实代码评估后给出最终建议，不必照抄。

====================
二十二、本轮设计稿必须回答
====================

请在现有V3报告基础上形成新的“V3设计稿初稿”，至少包含：

1. V3 Scope / V4 Deferred Scope；
2. 更新后的总体架构图；
3. ChatGPT Web当前真实交互模式；
4. 未来模型扩展边界；
5. Agent Contract；
6. Full Market Feature设计；
7. Multi-Recall及Recall Miss；
8. Full Universe Query；
9. Evidence模型；
10. Context Pack三级预算；
11. Task Profile和Task Run Registry；
12. AI Result Import；
13. Watchlist状态机；
14. Immutable Decision；
15. Append-only Review；
16. Portfolio / Trade Ledger；
17. 截图持仓/成交导入流程；
18. Position Context；
19. Selection/Entry/Add/Reduce/Exit分别评价的Performance模型；
20. Raw / Action / Entry分层；
21. expected_horizon / time_efficiency；
22. 601233 Regression设计；
23. Model-Agnostic扩展方案；
24. 数据库表变化；
25. READ API；
26. 当前可落地WRITE/Import API；
27. V4未来WRITE/MCP/Bridge扩展点；
28. V2→V3迁移；
29. 新Phase实施顺序；
30. 每个Phase验收标准；
31. 涉及当前项目的真实文件；
32. 性能/存储/数据量估算；
33. 风险和回滚方案。

要求：
- 必须基于当前真实代码和刚才的V3架构报告继续设计；
- 不要重新从零写一个完全不同的架构；
- 发现与当前代码不一致的地方必须指出；
- UNKNOWN保持UNKNOWN；
- 不允许编造ChatGPT Plus当前不存在的直接WRITE能力；
- 当前不使用浏览器Bridge；
- 当前不使用任何模型API；
- 不要为了未来V4过度设计V3；
- V3优先目标是让CHATGPT_WEB完整跑通投资决策闭环；
- 本轮绝对不要修改代码；
- 输出设计稿初稿后停止，等待我和GPT一起Review。
