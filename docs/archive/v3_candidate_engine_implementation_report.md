# V3 候选生成主链路整改 实施报告

- 分支：`feature/v3-candidate-engine-recall`
- 基线设计：《V3_低位埋伏候选生成与排序_详细设计.md》（2361 行）
- 执行依据：《Claude_Code_V3候选生成主链路整改_执行任务书.md》Step 1-21
- 完成日期：2026-09-08
- 变更规模：50 文件，+8512 行（`git diff main...HEAD --stat`）

---

## A. 修改文件清单

### 新增（引擎核心）

| 文件 | 职责 |
|---|---|
| `app/v3/candidate_engine/indicators.py` | 指标库：CROWDING_BOUNDARY、smoothstep、soft_band、weighted_combine 等 |
| `app/v3/candidate_engine/config.py` | 引擎配置（`V3_CE_*` 环境变量，禁止 magic number 散落） |
| `app/v3/candidate_engine/safety.py` | L1 Hard Safety Filter（只做资格/数据质量类） |
| `app/v3/candidate_engine/experts/base.py` | 专家基类（confidence=生效权重/总权重） |
| `app/v3/candidate_engine/experts/feature_experts.py` | 6 路特征专家 LP/BT/RV/AC/PB/RS |
| `app/v3/candidate_engine/experts/evidence_experts.py` | 2 路证据专家 FQ/CAT |
| `app/v3/candidate_engine/union.py` | L2 Recall Union + RRF（K=60，W=1.0） |
| `app/v3/candidate_engine/enrichment.py` | L3 特征富集（union 池特征补齐） |
| `app/v3/candidate_engine/pareto.py` | L4 Pareto 多目标分层 + NSGA-II crowding |
| `app/v3/candidate_engine/risk_reward.py` | RR 锚点分段线性评分（§17） |
| `app/v3/candidate_engine/penalty.py` | 六规则 Penalty（总封顶 -30，§20） |
| `app/v3/candidate_engine/soft_opportunity.py` | 八维 SoftOpportunity（§19） |
| `app/v3/candidate_engine/machine_rank.py` | Machine Rank（§21，Top120） |
| `app/v3/candidate_engine/deep_rank.py` | Deep Rank（§22，Top60） |
| `app/v3/candidate_engine/trace.py` | 全链 Trace（7 阶段生命轨迹，§35.2/§37.4） |
| `app/v3/application/candidate_pipeline.py` | 主链路装配（L1→…→Final30 + 漏斗计时） |
| `app/v3/application/outcome_label.py` | 结果标签 MFE/MAE/A-B-C（§26） |
| `app/v3/application/backtest_metrics.py` | Recall@K / Precision@K / NDCG@K（§27-§29） |
| `app/v3/application/miss_audit.py` | 漏选审计（§30） |
| `app/v3/application/shadow_pool.py` | 影子池四组分层抽样（§31.2） |
| `app/v3/application/trace_view.py` | TraceView（内存/落库双路径统一输入） |
| `app/v3/application/mature_scan_outcomes.py` | mature 回填编排（标签/审计/影子池落库） |
| `app/v3/application/scan_universe.py` | 全市场扫描编排（universe×特征行 join → pipeline → 落库） |
| `app/v3/infrastructure/db/scan_repositories.py` | 扫描 7 表读写 + mature 回填 + bars_from 批量 K 线 |
| `app/api/v3_scan.py` | 9 个只读扫描端点 |
| `migrations/versions/0018_candidate_scan_tables.py` | 7 张扫描表 DDL（down_revision=0017） |

### 修改

| 文件 | 变化 |
|---|---|
| `app/v3/domain/candidate_engine.py` | +573 行契约（8 专家评分、Pareto、Machine/Deep、Trace、Outcome/Metrics/Miss/Shadow 全部 frozen 契约） |
| `app/v3/application/calculate_features.py` | +129 行：候选引擎扩展 extras（weekly_slope_8w、decline_deceleration、趋势状态等） |
| `app/v3/infrastructure/db/models.py` | +7 张扫描表模型；pareto 行加 protected；outcome_labels 加 close_t/bars_used |
| `app/v3/infrastructure/db/uow.py` | 挂 `self.scans` |
| `app/api/v3_dashboard.py` | +116 行：漏斗区 / Top 区 / Why Not 区三段 |
| `app/api/main.py`（`app/main.py`） | 注册 v3_scan router |
| `app/v3/domain/recall.py` | RecallFeatureView 补 14 个宽表 additive 字段（真实扫描数据链修复） |
| `app/v3/infrastructure/db/repositories.py` | `_feature()` 全宽表字段映射 |
| `app/v3/infrastructure/db/scan_repositories.py` | 明细 INSERT 2000 行/批（asyncpg 32767 参数上限） |
| `app/v3/security.py` | `/scan/latest`、`/scan/` 前缀、`(stock,*,scan-trace)` 模板加入公开只读白名单（逐项设计考量见注释） |
| `tests/v3/test_migration_environment.py` | head 断言 0017→0018 |

### 测试（新增 9 文件，约 200 用例）

`test_candidate_indicators.py`(34) / `test_candidate_safety.py`(21) / `test_candidate_experts.py`(28) / `test_candidate_union.py`(11) / `test_candidate_pareto.py`(12) / `test_candidate_soft.py`(28) / `test_candidate_rank.py`(32) / `test_candidate_pipeline_trace.py`(7) / `test_candidate_features_extras.py`(5) / `test_outcome_metrics_shadow.py`(32) / `test_scan_universe.py`(5) / `test_candidate_scan_postgres.py`(2, PG)

---

## B. 架构变化

新管线在旧管线**旁边并行**（不改动旧扫描路径），由 `V3_CE_MODE`（old/new/dual，默认 dual）承载模式切换。

```
Universe(全市场 SecurityMember + 特征行)
  → L1 Hard Safety Filter        只淘汰资格/数据质量类（ST、停牌、新股、K线缺失、低价）
  → L2 8 路专家召回               LP/BT/RV/AC/PB/RS(特征) + FQ/CAT(证据)
  → Recall Union + RRF           rrf = Σ W_i/(K+rank_i)，K=60
  → L3 Feature Enrichment        union 池特征补齐（观测层，不做淘汰）
  → L4 Pareto 五维分层            LP / (BT+RV)/2 / AC / 0.55FQ+0.45CAT / RR
      + NSGA-II crowding + 单专家 Top3 保护
  → SoftOpportunity(八维) + Penalty(六规则, 封顶-30)
  → Machine Rank                 0.35*rrf_norm + 0.50*soft_net + 0.15*pareto_quality → Top120
  → Deep Rank                    machine45/weekly15/daily15/60m10/market5/industry5/rr5 → Top60
  → Final Top30（RAW，AI Review 后置）
  → 全链 Trace（7 阶段）→ 落库（7 表）→ API/Dashboard
 mature 回填（T+20 交易日）：Outcome Label → Miss Audit → Shadow Pool
```

关键机制：

1. **淘汰即记录**：任何一层淘汰都写 `candidate_snapshots`（stage/alive/drop_reason），已淘汰股在后续阶段继续留轨迹（alive=False 沿用首次 drop_reason），保证「每一只股票为什么死在哪一层」全量可查（§37.4）。
2. **机会型指标零硬过滤**：Safety 只保留资格类；位置/涨幅/量能等全部降级为 Soft 分、Pareto 维度、Penalty 扣分（详见 D 段）。
3. **单专家保护**：union 中仅一个专家命中且 rank≤3 的股票不经 Pareto 支配排序直接并入入选池（front=0，protected=True），防止多目标排序吞掉单通道强信号。
4. **可复现**：Machine/Deep/RR/Soft 全部纯函数；Shadow Pool seed=42；Feature version 写入每行快照。

---

## C. 8 路专家说明与数据代理映射

| 代码 | 专家 | 类型 | 核心维度 | 数据代理说明 |
|---|---|---|---|---|
| LP | 低位潜伏 | 特征 | position_250d / position_52w / distance_60d_low | 250/52 周位置分位直接来自日K |
| BT | 筑底企稳 | 特征 | atr_contraction / low_slope_20 / weekly_slope_8w / decline_deceleration | 周线特征为日K聚 Weekly 的代理 |
| RV | 底部反转 | 特征 | ma20_slope_delta / macd_hist_delta / multi_timeframe_state | MACD 由日K算；多周期状态=日/周趋势状态组合 |
| AC | 吸筹 | 特征 | up_down_volume_ratio / obv_slope_z / volume_5_20 | 上涨日/下跌日量比 + OBV 斜率 z-score |
| PB | 回踩不破 | 特征 | close_ma20_distance / swing_low_trend / pullback_20d | 摆动低点为 20d 枢轴点代理 |
| RS | 相对强度 | 特征 | rs_5d_slope / rs_20d_slope | 相对基准指数（5432 全市场等权基准的代理） |
| FQ | 基本面修复 | 证据 | 财报营收/净利同比 | `RPT_F10_FINANCE_MAINFINADATA`（东财 F10） |
| CAT | 催化剂 | 证据 | 公告/新闻事件 | 证据管道现有公告源，事件新鲜度加权 |

专家通用规则：confidence = 已生效权重 / 总权重（missing 维度不参与计分但拉低置信度）；任一专家不可评不影响其他专家；全不命中在 RECALL 层显式 drop（`no_expert_hit`），不静默。

---

## D. 硬过滤审计（任务书 §39 审计项）

| 旧管线行为 | 新管线处置 |
|---|---|
| ST 直接剔除 | 保留为 Safety 资格类可配置项 `V3_CE_SAFETY_EXCLUDE_ST`（默认 true，属交易资格而非机会判断） |
| 涨幅>阈值直接剔除 | 移除。改为：overheat>0.40→Penalty -12 / >0.25→-5；nonchase 子代理按 return_20d/consecutive_up_days/volume_spike 连续降分 |
| 连涨 N 天直接剔除 | 移除。nonchase 子代理 3→8 天平滑降分（0.30），永不直接淘汰 |
| 放量异常直接剔除 | 移除。volume_spike 1.5→3.0 子代理；crowding 作为 Pareto 质量微调（+5 封顶） |
| MA20 偏离>阈值剔除 | 移除。Penalty -8（>0.15） |
| 距 60 日高过近剔除 | 移除。Penalty -10/-5（<0.01/<0.03） |
| 周线斜率恶化剔除 | 移除。Penalty -5（weekly_slope_8w<-0.03）；WEEKLY_DOWN_DAILY_BOUNCE 在 Deep 层做趋势冲突降权（daily 压 40），但 reversal 证据可豁免 |
| 基本面恶化剔除 | 移除。Penalty -10（yoy<-0.10），FQ 专家照常评分 |

结论：Safety 层剩余硬过滤 = `exclude_st`、`reject_stale`（数据新鲜度）、`min_daily_bars≥120`、`close>close_floor`（低价面值类），全部为**交易资格/数据质量**类，且均可配置关闭；其余一切「机会型」判断全部进入 Soft/Penalty/Pareto/Deep 分层链路。

---

## E. 测试结果

| 套件 | 结果 |
|---|---|
| 离线全量 `pytest tests/v3` | **690 passed / 96 skipped / 0 failed**（16.5s） |
| 其中本轮新增候选引擎测试 | ~200 用例（指标库/Safety/8专家/RRF/Pareto/RR+Penalty+Soft/Machine/Deep/Trace+Pipeline/Outcome/Metrics/Miss/Shadow/编排器） |
| PG 集成（`test_candidate_scan_postgres.py` 等） | 96 skip：本机 127.0.0.1:5432 存在但 `V3_TEST_DATABASE_URL` 凭据未配置；**migration 0018 未在真实 PG 上 upgrade 验证**（脚本强制显式配置，禁止默认连生产） |
| `python -m compileall app tests` | PASS |
| OpenAPI 路由 | 9 个扫描端点注册确认（`/api/v3/scan/latest` 等，含子 router path 校验） |
| 公开只读面 | `/scan/latest` + `/scan/` 前缀 + `stock scan-trace` 模板验证通过；backtest/shadow 保持认证 |

关键精确值测试（防回归）：RR rr=2.2→91.4286；Machine 公式 78.0 精确；Deep 全维 67.0/confidence 0.80；Penalty 封顶 -30；NDCG 手算（gain A=3/B=2/C=1）；A/B/C 阈值压线（0.15/0.10/0.08、-0.05/-0.06/-0.08 含等号）；浮点容差 1e-9 压线不误杀；观察窗 19 根→PENDING。

---

## F. 真实扫描漏斗（Step 20）

**状态：已执行（2026-09-08，生产环境真实数据）。**

执行路径：全市场特征重跑（新代码含候选引擎 extras，as_of=9-7 收盘后，发布 feature run `893e4b2a`，5556 成功 / coverage 99.96%）→ `UniverseScanOrchestrator` 显式消费该 run → 扫描落库 + 当日 PENDING 回填。

真实漏斗（scan_run `2fd049b4`）：

| 阶段 | 数量 | 说明 |
|---|---|---|
| UNIVERSE | 5556 | 全市场特征行 |
| SAFETY | 5261 | 淘汰 295：ST 200 / 缺日K 86 / 停牌 6 / ST+停牌 3 |
| RECALL | 1117 | 8 专家并集（本轮 RS 专家因基准序列缺失未出分，6 特征专家生效） |
| ENRICH | 1117 | 观测层不淘汰 |
| PARETO | 275 | 五维分层 + 单专家保护 |
| MACHINE | 120 | Top120 |
| DEEP | 60 | Top60 |
| FINAL | 30 | RAW_TOP30（AI Review 后置） |

落库验证：candidate_snapshots 12329 行（全阶段 trace）、expert_recall_rows 1230、pareto_result_rows 1117、outcome_labels 5556（当日全 PENDING，符合 T+20 设计）、shadow_pool_rows 106、FINAL top10 分数区间 39.06~46.44。

执行中发现并修复的数据链问题（随本报告 commit）：
1. `scan_repositories.py` 单条 INSERT 超 asyncpg 32767 参数上限 → 明细表 2000 行/批分批写入。
2. 特征数据断裂：`_feature()` 只映射 12 列到 RecallFeatureView、`_assemble` 只传 JSONB extras → 宽表 28 主字段全丢。修复：RecallFeatureView 补 14 个 additive 字段 + `_assemble` 宽表与 JSONB 合成。
3. 编排器加 `feature_run_id` 显式通道（操作员决策），绕过 latest_run 的域 hash 校验而不放宽契约（PIT 不可变保护不允许改坏 hash 的历史 run）。

---

## G. 新旧管线对照

| 维度 | 旧管线 | 新管线 |
|---|---|---|
| 过滤模型 | 单阶段阈值过滤（早期高误杀） | 分阶段分层（高召回 + 逐层收敛） |
| 机会型指标 | 硬过滤直接淘汰 | Soft 分 / Penalty / Pareto 维度 |
| 单通道强信号 | 可能被综合排名吞掉 | 单专家 Top3 保护直通入选池 |
| 排序 | 单一综合分 | RRF + Machine(3 因子) + Deep(7 维) 两级 |
| 可解释性 | 只有最终分数 | 每阶段 score/rank/drop_reason 全轨迹 |
| 漏选 | 无记录 | Miss Audit（未来 A/B 未进 Final 自动归因） |
| 对照组 | 无 | Shadow Pool 四组分层（seed 可复现） |
| 效果度量 | 无 | Recall@K 六池 / Precision@K / NDCG@K |
| 旧路径 | — | 未改动，dual 模式并存 |

---

## H. 未完成项（诚实清单）

1. ~~**真实全市场扫描未执行**~~（Step 20）：**已完成**（2026-09-08 生产执行，漏斗见 F 段）。遗留：RS 专家基准序列缺失未出分（见 H-10）、FQ/CAT 证据专家未接 evidence 视图。
2. **PG 集成测试未真跑**（Step 19 部分）：测试库凭据未配置，migration 0018 的真实 upgrade/downgrade 未验证；离线 DDL 语义已由模型 + alembic head 检查覆盖。
3. **AI Review 未接入**（§24）：Final Top30 为 RAW_TOP30；AI Review 的结构化 DTO 与保存（任务书 §25）待后续，AI 输入继续剔除统一总分（FORBIDDEN_UNIFIED_SCORES）。
4. **行业分散化未实现**（§25）：Final 30 未做行业配额约束。
5. **Deep Rank 三维恒 missing**：60 分钟线 / 市场 / 行业维度数据源未接，confidence 上限 0.80（(45+15+15+5)/100）。
6. **cooldown 未实现**（§33）：属回测多日聚合语义（10~15 交易日去重），v1 单扫描不触发，留待多日回测编排。
7. **adjustment_ok 恒 True**：复权因子新鲜度未接入 Safety/特征链，复权异常股暂按可用处理。
8. **dual 模式对照跑批未执行**：EngineMode=dual 配置就绪，但未编排「同一交易日旧管线 + 新管线双跑」的对照输出。
9. **Shadow Pool 对照分析需成熟数据**：`/shadow/metrics` 端点已返回各组 GOOD rate 与淘汰原因分布，但有效结论要等 T+20 观察窗成熟。
10. **指数/行业基准数据代理**：RS 专家用现有基准指数日K的 5/20 日斜率，未使用设计中的独立行业轮动因子。

---

## 附：完成判定核对（任务书 §43）

- [x] 旧流程仍可运行（未改动旧扫描路径）
- [x] 新流程完整跑通（离线全链测试 + 编排器测试）
- [x] 8 路专家全部存在
- [x] 机会型指标不再硬过滤（D 段逐项审计）
- [x] RRF 实际参与排序（union rrf_norm → Machine 0.35 权重）
- [x] Pareto 实际参与候选保护（五维分层 + crowding + front 晋级）
- [x] 单专家保护生效（protected=True 直通，front=0）
- [x] Machine Top120 可查询（`/scan/{id}/top?stage=MACHINE`）
- [x] Deep Top60 可查询（`?stage=DEEP`）
- [x] Final30 可查询（`?stage=FINAL`）
- [x] 任意股票 trace 可查询（`/stock/{code}/scan-trace` + Why Not 页）
- [x] 分数完全可解释（各层 reasons/components 落库）
- [x] drop reason 可追溯（candidate_snapshots 全量）
- [x] Recall/Precision/NDCG 可计算（`/backtest/metrics`）
- [x] MFE/MAE 可计算（outcome_labels 表 + mature 编排）
- [x] Miss Audit 可运行（`/backtest/misses`）
- [x] Shadow Pool 可运行（`/shadow/metrics`）
- [x] 页面可看漏斗（dashboard 漏斗区）
- [x] 页面可看 Why Not（dashboard Why Not 区）
- [x] 单元测试通过（690 passed 离线）
- [ ] 集成测试通过（**PG 集成 skip，待测试库**）
- [x] 原有行情/K线/持仓/自选接口未破坏（未触碰相关模块；全量套件通过）
- [x] 真实全市场扫描执行（2026-09-08 生产，5556→FINAL30，见 F 段）
- [x] 已生成 implementation report（本文件）
- [x] 未完成项已诚实列出（H 段 10 项）
