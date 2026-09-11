# V3 候选生成主链路现状审计（v3_candidate_engine_current_state）

- 审计日期：2026-09-07
- 审计基线：`main@7f71b11`（FC-05 收口版）
- 审计方式：三路并行只读审计（主链路/数据层/基建），关键结论已抽查源码核实
- 用途：候选生成主链路整改（8 路专家召回 + Pareto + 双层 Rank）实施前的事实基线。**所有类名、行号、数量、阈值均来自源码核实，非文档推断。**
- 设计基线：《V3_低位埋伏候选生成与排序_详细设计.md》；实施约束：《Claude_Code_V3候选生成主链路整改_执行任务书.md》

---

## 1. 当前系统全景

仓库存在**两套并行系统**，新引擎（候选生成整改）属于 V3 线：

| 线 | 位置 | 存储 | 状态 |
|---|---|---|---|
| V1/V2 实时扫描 | `app/services/scanner.py`、`app/services/opportunity_scoring.py` | JSONL 文件（`app/history.py`） | 生产在用，Top30 来源 |
| V3 事件溯源管线 | `app/v3/**` | PostgreSQL schema `v3`，content_hash 幂等 | 生产在用（multi-recall + 特征库），**无最终 Top30 排序引擎** |

V3 当前主链（调度器 `scripts/v3_scheduler.py:497-521`，advisory lock `v3-scheduler-main`）：

```
market-data → index-benchmarks → features → evidence-increment → full-recall
```

`full-recall` 之后**就结束了**——V3 只产出 `raw_opportunities`（多通道命中并集），没有 Pareto、没有打分排序、没有 Top30。设计的 8 路专家引擎 = 在现有 `full-recall` 之后/之上重建 L2-L7，最终落 `candidate_snapshot` 等新表并暴露 API。

## 2. 现有候选生成主链路（逐层事实）

### 2.1 全市场 Universe

| 环节 | 位置 | 事实 |
|---|---|---|
| 实时全市场清单 | `app/providers/eastmoney.py:449-514` `get_all_a_shares()` | 东财 clist 分页（pz=100，f12 稳定排序防重漏），去重后 dict，3s 缓存 `market:all-a-shares`；`fs` 覆盖沪深京全部板块 |
| Provider 抽象 | `app/providers/base.py:70`、`manager.py:190-191` | Manager 仅委派 eastmoney（`supported=("eastmoney",)`），tencent 为 fallback |
| V3 UniverseSnapshot | `app/v3/domain/market_data.py:294-350` | content-hash 校验的快照域对象 |
| V3 刷新与 sanity gate | `app/v3/application/refresh_universe.py:33-70` | `minimum_members=4500, maximum_members=5700, minimum_coverage=0.9, minimum_retention=0.9, maximum_growth=1.05`；Provider 失败 → LKG |
| V3 持久化 | `app/v3/infrastructure/db/models.py:720-785`；`repositories.py:822-905` | `universe_sources` + `universe_snapshots` + `universe_members` |
| 特征驱动入口 | `app/v3/application/run_full_market_features.py:38-47` | 由 `universe_snapshot_id` → 全量 security 逐个算 QFQ DAY 特征 |

### 2.2 硬过滤清单（前置误杀的根，全在 V1/V2 扫描器）

**V1**：`app/services/scanner.py:187-224`；**V2**：`:417-451`（近重复拷贝，V2 缺 pct_change 过滤）。逐条：

| # | excluded key | 条件 | 阈值 |
|---|---|---|---|
| 1 | `chinext` | code 以 300/301 开头 | — |
| 2 | `star` | code 以 688/689 开头 | — |
| 3 | `bse` | market==BJ 或 code 以 4/8/92 开头 | — |
| 4 | `other` | 非 `is_mainboard(code)`；`MAINBOARD_PREFIXES = ("000","001","002","003","600","601","603","605")`（scanner.py:21） | — |
| 5 | `suspended` | `quote.suspended` | — |
| 6 | `invalid_quote` | price/pct_change 为 None | — |
| 7 | `st` | `is_st(name)`：ST/*ST/含"退"（scanner.py:25-27），`exclude_st` 默认 True | — |
| 8 | `illiquid` | amount < min_amount | 默认 5000 万 |
| 9 | `pct_change`（仅 V1） | pct_change > max_pct_change | 默认 5.0% |
| 10 | `limit_untradable` | `at_price_limit(up/down)`（±10%/ST ±5%，scanner.py:34-39）+ `is_one_price_board`（high==low 且 volume>0，scanner.py:42-43） | — |

**误杀规模量化**：主板白名单仅 8 个前缀 ≈ **2200 / ~5700** 只股票能进 V1/V2；创业板/科创板/北交所 + 全部非主板前缀在评分之前就出局。这是"机会型单条件硬过滤"的最大单一误杀点（设计基线 L1 只允许交易资格/数据质量类过滤）。

**V1 K线门槛（第二重误杀）**：`scanner.py:226-232` `preliminary()` 用 pct_fit/volume_ratio/liquidity 三个**机会型**启发式对 eligible 排序，截 `min(max(top_n*3,60),120)`（默认 90，`kline_required` scanner.py:342）只给这 90 只拉 K线，其余直接没机会进 Top30。K线拉取失败回退为无趋势因子打分（scanner.py:255-260）。

**V2 候选池（预召回雏形）**：`app/services/opportunity_scoring.py:130-155` `build_candidate_pool(eligible, target_size=420)`，5 通道各 `max(80, ceil(420/3))` 上限：`trend_improvement_proxy`（最接近 pct=1.0）/`low_position_proxy`（pct+volume_ratio 最低）/`flow_activity`（volume_ratio 降序）/`relative_strength`（pct 降序）/`liquidity_floor`（amount 降序），余量按 amount 补；目标 clamp [300,500]。**通道阈值同样是机会型指标单条件过滤。**

**V3 盘中硬过滤**：`app/v3/application/intraday_overlay.py:154-180`——stale quote 直接跳过（:155），**无主板过滤**，全市场扫描，未触发信号即跳过（:179）。fast-lane coverage 门：≥0.9 AVAILABLE、0.5-0.9 PARTIAL、<0.5 `UNAVAILABLE_FOR_FULL_MARKET_SCAN`（`intraday_fast_lane.py:62-63,212-219`）；`full_market_complete = (unique_count == expected)`（:211）。指数质量门 :485-503。

**V3 召回通道可用性门**：`app/v3/application/evaluate_recall_channels.py:58-76`——required_fields 任一 None/UNKNOWN/stale 该行跳过（计 `unavailable`）；整通道 0 可评行 → `RecallChannelUnavailable` 进 RecallRun.errors。

### 2.3 评分维度与 Top30 方式

**V1 `score_candidate`（scanner.py:83-134，总分 100，权重硬编码）**：trend 25（MA20 上下/MA5>MA20/MA20>MA60）+ volume 20（volume_ratio/2 + log10(amount)）+ relative 20（pct-基准）+ position 20（pct 区间 + 距 MA20/20d 高点 + 连续 3 日 ≥4% 扣 4）+ liquidity 15。排序 `scanner.py:263`，top_n 默认 30（clamp 1..100）。

**V2 `build_opportunity_candidate`（opportunity_scoring.py:528-668，公式 :21-25）**：
`position(15) + fundamental(15) + trend(20) + flow(15) + catalyst(10,缺源恒缺) + risk_reward(20) + liquidity(5) + risk_penalty(0..-20)`，clamp 0..100。分项阈值全部内联魔法数：
- position :158-226：250d 锚点 [0.25,0.65]→+5；ret60 [−12,18]→+3；距 MA20 [−4,8]→+3 等
- 周线分类 `classify_week_trend` :229-247（需 ≥30 周bar）：IMPROVING 8.0 / BASE_BUILDING 5.0 / DECLINING 1.5 / MIXED 4.0；DECLINING 封顶日线分 5.0（:303-305）
- trend :250-320：MA20 斜率/价格位置/低点抬高，标签 TURNING_UP ≥9 / REPAIRING ≥6 / BASE_BUILDING ≥3 / WEAK
- flow :323-366：volume_multiple 1.2-3.0 放量涨→+5、5 日阳量占优→+3、20d 突破放量→+3（**全为量价代理，非真实资金流**）
- risk_reward :399-428：RR<1→0、<1.5→4、<2→8、<3→14、≥3→20（止损=支撑−0.6×ATR14，`support_resistance` :369-396）
- risk_penalty :470-505：ret20>25→−4、volume_ratio>4→−3、涨停/一字→−20
- grade :508-525：A 需 score≥80 且 fundamental≥9 且周线≠DECLINING 且 RR≥2；`hard_reject`（涨停/一字）强制 C（:588）
- 排序：`scanner.py:496-498`，`raw_top30 == action_top30`（行业集中度未实现，占位），top100=scored[:100]（:513）

**V3 明确禁统一总分喂 AI**：`app/v3/domain/action.py:14-16` `FORBIDDEN_UNIFIED_SCORES = {final_total_score, action_total_score, final_rank_score, opportunity_score}`；`app/v3/domain/context.py:130-135` `reject_unified_final_score` 校验器连嵌套 dict 一起拦。**这是设计基线 Machine/Deep Score 与现有 V3 架构的直接冲突点**（见 §5 适配策略）。

### 2.4 V3 现有召回通道（8 路专家的存量底座）

特征通道引擎 `app/v3/application/evaluate_recall_channels.py`：通用 `FeatureRecallChannel.evaluate`（:50-82，谓词+强度注入式，命中不设上限），9 条特征通道（:85-170）：

| Code | 谓词（精确） |
|---|---|
| `LOW_POSITION_TURNING` | `position_60d<=0.4 and return_3d>0 and ma20_slope>=0` |
| `TREND_IGNITION` | `return_5d>=0.03 and ma20_slope>0 and position_60d<0.85` |
| `FIRST_BREAKOUT` | `breakout_20d and return_3d>0` |
| `FIRST_PULLBACK` | `pullback_20d and return_20d>0 and ma20_slope>0` |
| `WEEK_BASE_DAY_STRENGTH` | `weekly_trend_state in {BASE,UP} and daily_trend_state==UP and return_5d>0` |
| `RELATIVE_INDEX_STRENGTH` | `relative_index_strength>=0.03` |
| `RELATIVE_INDUSTRY_STRENGTH` | `relative_industry_strength>=0.03`（**当前永远不触发，见 §4 缺口**） |
| `VOLUME_EXPANSION` | `volume_expansion(=volume_ratio_5d>=1.5) and return_3d>0` |
| `ANOMALY_COMBINATION` | `breakout_20d and volume_expansion and return_3d>=0.03` |

证据通道 `app/v3/application/evaluate_evidence_recall_channels.py`：`FUNDAMENTAL_IMPROVEMENT`（营收/净利同比 ≥0.1，:75-77）、`EARNINGS_INFLECTION`（均值 YoY ≥20，:133-135）、`CATALYST_EVENT`（OFFICIAL_DISCLOSURE 30 天窗口关键词：回购/增持/中标/重大合同/业绩预增/股权激励，:157-207）。

编排 `app/v3/application/run_multi_recall.py:41-137`：通道集合无关、单通道故障隔离、content_hash 幂等，产 `RecallRun`+`RecallResult`+`RawOpportunity`+`PerformanceObservation`（3/5/10 期 PENDING，:168-182）。错评阈值 `scheduler-v1 raw_return_gte=0.15`（v3_scheduler.py:351-358）。调度接线 `v3_scheduler.py:274-305`（full_recall_handler）+ `:500-517`（job 图）。

特征谓词的数据源：`app/v3/application/calculate_features.py:22-125`，28 字段（returns 3d~250d、position 60/120/250、MA+slopes、ATR14/atr_pct、volatility20、breakout_20d :188-191、pullback_20d :194-199、volume_ratio_5d :202-206、daily/weekly_trend_state（周线 MA10W/MA30W）:234-271、coverage）。

### 2.5 持久化与暴露

| 层 | 位置 |
|---|---|
| V1/V2 快照 | `{scan_history_path}/{date}.jsonl`（`app/history.py:50-53`，future_return 全 None 占位） |
| V3 表 | `recall_channels/recall_runs/recall_results/raw_opportunities/performance_observations/recall_miss_evaluations`（models.py:1123-1283）；repo `repositories.py:1825+` |
| HTTP | `/scan`、`/scan/v2`、`/scan/coverage`（routes.py:346-379）；`/gpt/{secret}/scan/v2/html`（:448）；`/scan/ab`（:471-477 返回 {v1_top30,v2_top30}）；V3：`/api/v3/recalls`、`/raw-opportunities`、`/recalls/misses`（v3.py:434-502） |
| MCP | `scan_mainboard(_v2/_ab)`（mcp/server.py:62-103）；`v3_scan_opportunities(mode=EOD/INTRADAY)`（mcp/v3_tools.py:179-245，EOD 读 raw_opportunities） |
| 看板 | V1 Top30 实时页 `app/api/live.py:269-444`（`#top30-table` + live.js 客户端排序）；V2 Top30 HTML `routes.py:271-281`；V3 `/v3/dashboard` = 特征浏览器**不是** Top30 页（v3_dashboard.py:28-55 可排序字段 return_20d/60d、position_60d、amount、atr_pct、volume_ratio_5d、coverage） |

## 3. 数据层能力盘点（新引擎可用/缺失）

### 3.1 已有（可直接复用）

- **日/周线 PIT 仓库**：`bar_series_revisions`/`market_bars`（models.py:933/973），QFQ DAY/WEEK/MONTH（`BarPeriod` 枚举仅 DAY/WEEK/MONTH，domain/market_data.py:32-35）；读 `SQLAlchemyBarRepository.latest_daily_revisions(security_ids, as_of)` PIT 查询（repositories.py:1254）、`latest_weekly_revisions`（:1353）。**全量回填器现成**：`backfill_daily_bars.py:24 BackfillDailyBarsService`（断点续传、并发受控）。
- **分钟线**：V3 库**不落**（设计约定 fetch-time fact，deep_market_data.py:15-21）；live 读走 `IntradayMarketDataService`（periods 1m/5m/15m/60m/day/week，intraday_market_data.py:97-195）与 `DeepMarketDataService`（5m/15m/60m + 各周期趋势/支撑阻力 :73-100）。5m→60m 聚合已存在（kline_aggregation.py:40）。
- **指标**：MA5-60/RSI14/ATR14/20d·60d 高低窗（indicators/technical.py:17-61）；V3 向量化 28 字段（calculate_features.py）。支撑阻力=20d 窗口高低（models.py:269-277）+ 全市场一次查询 `features.daily_levels`（repositories.py:1659-1751，runtime.py:112-115 已接线）。
- **基准**：6 指数（HS300/CSI500/CSI1000/SSE/SZSE/CHINEXT）PIT 20d 收益（ingest_index_benchmarks.py:16-23、calculate_index_benchmark_return.py:25-58），features 管线已接（run_full_market_features.py:50-56）。
- **催化/新闻/公告**：完整 evidence 子系统——Cninfo/SSE/EastmoneyReport/GovPolicy/EastmoneyNews 五 Provider + 注册表限流重试（infrastructure/providers/evidence.py），`uow.evidence.for_securities(security_ids, as_of)` 读路径（repositories.py:636）。
- **报价快照**：全市场 quote 带 turnover_rate/volume_ratio/新鲜度/fallback 标记（models.py:31-48；V3 映射 map_quote_snapshot intraday_market_data.py:36-87）。
- **特征查询面**：`FeatureQuery` 游标分页 + 排序（domain/features.py:31-41；repositories.py:1521）。

### 3.2 缺失（新引擎不能假设存在的）

| 能力 | 现状 | 对设计的影响 |
|---|---|---|
| **资金流/主力单** | **全仓不存在**（grep 资金流/f62 零命中）；DataCoverage.flow 恒 false；V2 flow_score 是量价代理 | 设计 AC（资金）专家无原生数据 → 需量价代理适配或新增数据源（见 §5） |
| **行业分类** | 无 industry 列（models.py:706-781 无此字段）；唯一来源=基本面 Provider 的 live `industry` 字段（fundamentals/eastmoney.py:270-273），未入 V3 库 | Deep Rank 的 industry 分量、RS 行业拐点、行业集中度控制受影响 |
| `relative_industry_strength` | **死通道**：定义在 calculate_features.py:79-83，但 run_full_market_features.py:78-82 永不传 industry_return_20d → 恒 None → RELATIVE_INDUSTRY_STRENGTH 永不触发 | 行业 20d 收益数据补上才能启用 |
| 基本面存储 | live-only（fundamentals/manager.py，6h TTL），V3 库无表；V3 内仅 evidence FINANCIAL 行可部分替代 | FQ 专家需经 evidence 通道（现状已如此）或新建存储 |
| MACD/OBV/VWAP/BOLL/KDJ/摆动点 | **全仓不存在**（grep 零命中）；swing 仅为 20d/60d 窗口高低 | 设计若需要摆动结构需自算 |
| 全市场 K线预热 | SQLite 缓存在但**无全量预热任务**（仅 V2 对 ~420 池拉 day260+week80） | Feature Enrichment 需自带预热/复用 backfill 服务 |
| 换手率序列/自由流通换手 | 仅 quote 级 turnover_rate 与 amount | 评分需注意口径 |

## 4. 基建约束（新代码必须遵守的既有规范）

- **Alembic**：线性链至 `0017_regime_stale_reason`（migrations/versions/）；新迁移 = `0018_<snake_name>.py`，`down_revision="0017_regime_stale_reason"`，全部 `schema="v3"`（0001 建 schema）。命名约定 `app/v3/infrastructure/db/base.py:7-13`。迁移由 worker 容器启动时跑（docker-compose.yml:69）。
- **模型惯例**：UUID PK、`as_of`/`known_at` 对 + `known_at>=as_of` CheckConstraint、`content_hash String(64)` 唯一（内容寻址幂等）、JSONB payload、具名 CheckConstraint、`{"schema": V3_SCHEMA}`。
- **UoW**：`app/v3/infrastructure/db/uow.py:36-68`（repo 在 `__aenter__` 实例化）；协议 protocols.py:366-392（滞后于实现，新增 repo 需同步协议）。
- **应用服务惯例**：`app/v3/application/` 一文件一服务、`uow_factory` 注入首参、`execute(...)`。
- **加 job**：`scripts/v3_scheduler.py` `build_orchestrators()` 内定义 handler + 对应组的 `JobDefinition`（data-prep/evidence/eod-scan/maintenance 四组，`GROUP_REQUIRED_JOBS` 登记 required Job）；幂等/重试/跳过由 Orchestrator 处理（orchestrator.py:115-151）；catch-up 按组追平（组内 required Job 最近全部成功日之后补齐，`_group_last_success_key`）——**新 required Job 需同步 `GROUP_REQUIRED_JOBS`，否则该组 catch-up 追平判定缺位**。
- **API 惯例**：裸 contract 返回（无包装 envelope）、错误 envelope `{code,message,request_id,details,retryable}`（v3/errors.py:45-58）、游标分页 limit∈[1,200] 默认 50、写操作 `_bind_principal`（v3.py:128-139）。**新增公开 GET 必须加入 `app/v3/security.py:63-76` 公开白名单**，否则要求 MARKET_READ token。
- **前端**：server-rendered HTMLResponse（v3_dashboard.py:316-378 样式；`_status_badge` :140-153、行枚举 :252-270、GET 表单控件 :308-309），可选 vanilla JS `data-*` 排序（live.js 样例）。无 SPA/无图表库。
- **测试惯例**：`tests/v3/test_<feature>.py` + `test_<feature>_postgres.py`（`skipif not V3_TEST_DATABASE_URL` 样例 test_recall_postgres.py:37）；PG 套件必须 fresh 库单跑（历史测试无清理，二连跑幂等跳过必失败——既有行为）。
- **Runtime 单例**：`app/v3/runtime.py:73-138` `build_v3_runtime`，worker/engine 有副作用、HTTP/MCP `engine=None` 只读；新只读服务一律挂 V3Runtime 走 Factory（FC-05 已收口）。

## 5. 设计基线 vs 现状的冲突与适配策略（最小侵入）

| # | 设计要求 | 现状冲突 | 适配（保持现有稳定架构） |
|---|---|---|---|
| C1 | Machine Score/Deep Score 统一分值排序 | V3 域层黑名单禁统一总分**喂 AI**（action.py:14-16、context.py:130-135） | 排序引擎内部照设计打分排序（新域对象承载，不受黑名单管）；**AI Structured Review 输入继续剔除统一分**，黑名单语义不动 |
| C2 | AC 资金专家（资金流驱动） | 资金流数据不存在 | 首版 AC = 量价代理（volume_multiple/阳量占优/突破放量，复用 opportunity_scoring.py:323-366 思路），DataCoverage.flow 机制保留，真实资金源接入后切换；差异写入实施报告 |
| C3 | RS 行业拐点、Deep Rank industry 分量 | 无行业分类库 | 首版 industry 分量按 missing 降 confidence（对齐 FQ missing ≠ 0 分原则）；补行业映射为后续项 |
| C4 | Feature Enrichment 需 60m/15m | V3 库不落分钟线 | 按 TopN 截断后 live 拉取（设计本就 Top120 后才补 60m/15m，量级 ~120 只可承受）；不建分钟线仓库 |
| C5 | 全市场扫描量级 | Universe 4500-5700 gate 现成；K线缓存无全量预热 | L1 后的 RecallPool 批量走 `latest_daily_revisions` PIT 批查（已按 security_ids 批量）；预热复用 `BackfillDailyBarsService` |
| C6 | Top30 出口 | V3 无 Top30；V1/V2 出口在用 | 新引擎落 V3 新表 + 新 API（/scan/* 白名单）+ /v3/dashboard 新页；V1/V2 路径不动，dual 配置切换（old/new/dual） |
| C7 | 漏斗/回测表 | `performance_observations`/`recall_miss_evaluations` 已有同类 | outcome_label/miss_audit 优先扩展既有表语义（设计允许"已有同类表优先扩展"），真正缺的（scan_run/candidate_snapshot/expert_recall/pareto_result/shadow_pool）新增 0018 |
| C8 | catch-up 判定 | TERMINAL_MAIN_JOB="full-recall" | 若新引擎 job 设为主链终结点，同步更新该常量；否则挂在 full-recall 之后作普通依赖 job |

## 6. 组件分类清单（整改施工图）

**可复用不动（稳定能力，禁止重写）**：行情 Provider 体系（eastmoney/tencent/manager）、K线缓存与聚合、`backfill_daily_bars`、`CalculateSecurityFeatureService`（calculate_features.py）、`RefreshUniverseService`、evidence 五 Provider + 证据读路径、基准 PIT、`FeatureRecallChannel` 通用引擎 + `RunMultiRecallService`（通道集合无关）、`IntradayOverlayService`/`IntradayScannerService`（阈值构造器注入）、`daily_levels`、Orchestrator/调度器框架、V3Runtime Factory、UoW/repo 体系、持仓/自选/决策全链。

**需改造**：V1/V2 硬过滤块（机会型过滤降级为 Soft 分量——新引擎内实现，旧扫描器按 dual 配置保留原样）、`run_full_market_features.py:78-82`（若启用行业收益需传参，首版不改）、`v3_scheduler.py`（追加候选引擎 job + 可能的 TERMINAL_MAIN_JOB）、`app/v3/security.py` 白名单、`app/config.py`（V3_CANDIDATE_ENGINE_MODE 等 env）。

**全新增**：8 路专家评估器（复用 FeatureRecallChannel 注入式，一部分=改造现有通道阈值/改用 52w position 等 Soft 公式）、Recall Union + RRF、Pareto + 单专家保护、Soft Opportunity + Penalty、Machine Rank、Deep Rank、Trace、scan_run/candidate_snapshot/expert_recall/pareto_result/outcome_label/miss_audit/shadow_pool 表与 repo、`/api/v3/scan/*` 读 API、/v3/dashboard Top30 页、dual 模式开关。

## 7. 风险与注意点

1. `kline_required`（V1 :226-232）与 V2 `build_candidate_pool` 都是**机会型指标单条件截断**，与设计"L1 只许交易资格/数据质量硬过滤"直接冲突——整改核心目标所在，新引擎 L1 不得复刻。
2. `ST`/涨停过滤在 V1/V2 是合理交易资格过滤（可交易性），保留进新 L1 没有架构冲突；`exclude_st`、`min_amount=5000万` 归类为交易资格类，是否保留需按设计 L1 白名单核对（设计：只许交易资格/数据质量类）。
3. V1/V2 过滤逻辑三处拷贝（scanner.py 两份 + opportunity_scoring.py:41-55）——新引擎**不**去合并旧代码（dual 保留），只在新引擎内单点实现。
4. `raw_top30 == action_top30` 占位（scanner.py:498）与 `missing_data_sources`（:518-522）是 V2 已知欠账，不在本次整改范围（不扩大战线）。
5. 9 路存量特征通道与设计 8 路专家**不是一一映射**：映射关系（LP/BT/RV/PB/RS ← 存量特征通道改造；AC ← 新建量价代理；FQ/CAT ← 存量证据通道增强）在实施时逐路写明，避免"改了名就算实现了"。
6. PG 测试必须 fresh 库单跑（历史测试无清理）；新测试文件 fixture 按 `test_active_plan_postgres.py` 双向 TRUNCATE 模式写，不加剧泄漏。
