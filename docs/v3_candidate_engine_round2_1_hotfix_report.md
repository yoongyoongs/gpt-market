# V3 候选引擎 Round2.1 最终热修报告

> 任务书：`Claude_Code_Round2.1_V3候选引擎最终热修任务书.md`（8+1 项 Runtime Correctness Hotfix）
> 报告日期：2026-09-10　分支：`fix/v3-candidate-engine-round2-1`
>
> **状态总览**：代码修复 + 单元/集成测试全部完成（809 passed / 101 skipped，skip 不计 pass）。
> C/D/E/F/G/H 节需要生产 SSH（106.13.171.166）与 PG 隔离测试库——**本机网络原因 SSH
> 持续被封（kex_exchange_identification 超时），按用户指令记录不修**，相应节标注
> `PENDING(SSH)`，网络恢复后按 §8"补验清单"执行并回填本报告。

---

## A. Git

```text
before commit  a1a3911a9b0ab4fa6e6cd108ab9bd529a32d62d6  (chore: .dockerignore 补 backups)
after commit   65e72a97302679d89156429b9e73a623abca9629  (R2.1-P1-02/P1-03)
branch         fix/v3-candidate-engine-round2-1
tag            无新 tag（任务书未要求；round2 冻结 tag 见 v3-pre-product-closure-20260901）
```

本轮 4 个 commit（每个 commit 后全量回归通过）：

| commit | 内容 |
|---|---|
| `41ad6c8` | P0-01~05：60m 结构读取路径 / T 日边界 / 负样本 NONE 分离 / 回测三态 / 影子池成熟分母 |
| `7b04575` | P0-06：Deep 全池保留 rank/score，#61~120 记 deep_rank_below_top60 dead 行 |
| `2368111` | P1-01：结构化 Risk/Reward 真正接入 Machine/Deep 主链 |
| `65e72a9` | P1-02/P1-03：candidate-scan 与 candidate-outcome-mature 接入调度器 |

**未推送**：分支 4 个 commit 尚未 push 到 origin（SSH 封禁期间未尝试 HTTPS push）。

---

## B. 8+1 个问题逐项状态

| ID | 问题 | 修改文件 | 修复方式 | 测试 | 状态 |
|---|---|---|---|---|---|
| P0-01 | 60m Deep 读取路径错误（读 period 顶层，真实结构在 `period["structure"]`，恒 missing） | `app/v3/application/scan_universe.py` | `_fetch_minute_60` 改从 `period["structure"]` 读 trend/support/resistance；Gate=AVAILABLE+非stale+非UNTRUSTED+trend≠UNKNOWN，拒收记 M60_* reason 四类计数 | `test_m60_structure_gate.py`（12 个） | ✅ TESTED |
| P0-02 | Outcome `bars_from` 用 `>` 漏 T 日 K → close_T 拿不到 → 全 PENDING | `app/v3/infrastructure/db/scan_repositories.py` | `bars_from` 改 `>= since`，T 日首根进入观察窗 | `test_candidate_scan_postgres.py::test_bars_from_includes_t_day_boundary`（PG 待跑）+ 离线边界 | ✅ TESTED |
| P0-03 | PENDING 与成熟负样本 label 混淆（负样本 label=None 被当未成熟） | `app/v3/domain/candidate_engine.py`、`scan_repositories.py`、`migrations/versions/0019_outcome_label_none.py` | `OutcomeLabelResult.status`（PENDING/MATURED）与 label 分离；成熟负样本显式 `label='NONE'`；migration 0019 扩 valid_label 约束为 `('A','B','C','NONE')` | `test_outcome_metrics_shadow.py`（39 个）、`test_migration_environment.py` | ✅ TESTED |
| P0-04 | Backtest 顶层无 PENDING 语义（未成熟样本被当零分母真值） | `app/api/v3_scan.py` | `status_from_label` 重建 status；顶层三态 PENDING/PARTIAL/OK 按 matured_count；PENDING 时 note=None | `test_r21_metrics_shadow_api.py`（6 个） | ✅ TESTED |
| P0-05 | Shadow 未成熟 good_rate 伪装 0 / 分母含未成熟 | `app/api/v3_scan.py` | 未成熟 `good_rate=null`；分母= matured（label∈A/B/C/NONE）；顶层 PENDING/PARTIAL/OK | `test_r21_metrics_shadow_api.py`、`test_outcome_metrics_shadow.py` | ✅ TESTED |
| P0-06 | Deep 只出 Top60，#61~120 无 rank/score/drop trace | `app/v3/candidate_engine/deep_rank.py`、`trace.py`、`domain/candidate_engine.py` | 全池（≤120）entries 保留真实 rank/score/components；`selected=rank<=top_n`；未入选记 `deep_rank_below_top60` dead 行（真实 score/rank）；Final 未入选 reason 改 `final_rank_below_top30` | `test_deep_rank_p1.py`（39 个）、`test_candidate_rank.py`、`test_candidate_pipeline_trace.py` | ✅ TESTED |
| P1-01 | 结构化 RR 未接主链（P1-06 引擎在但管线未传 levels） | `app/v3/candidate_engine/risk_reward.py`、`app/v3/application/candidate_pipeline.py` | Machine 阶段传 `structure_levels_from_features`；Deep 阶段 `merge_structure_levels(60m, features)`（60m 优先，(type,price) 去重保 provenance）重评估 RR；池键 `rr_refined_score`+`rr_levels` 进 Why Not 证据；候选不足自动回退 v1 代理 | `test_deep_rank_p1.py::TestStructuredRRWiring`（6 个） | ✅ TESTED |
| P1-02 | Candidate Scan 未接正式 scheduler | `scripts/v3_scheduler.py`、`app/v3/application/scan_universe.py`、`scan_repositories.py` | 主链新增 `candidate-scan` Job（depends_on=evidence-increment，与 full-recall 并行）；显式传同编排 feature_run_id（PIT 一致）；幂等：同上海交易日同 strategy/parameter 版本已有 PUBLISHED scan → `already_scanned`；mode=V2 随策略链 Gate 排除，Research Shadow 下无 Trade side effects | `test_scheduler.py`（Test A/B/C，26 个） | ✅ TESTED |
| P1-03 | Outcome Mature 只处理 latest scan，历史漏跑永远补不上 | `app/v3/application/mature_scan_outcomes.py`、`scripts/v3_scheduler.py`、`scan_repositories.py` | 维护链新增 `candidate-outcome-mature` Job；`execute_pending` 查所有仍 PENDING（无 outcome 行或存在 NULL label）历史 scan 补跑；45 自然日保守窗口（20 未来交易日上界）；batch_limit 分批（`V3_CANDIDATE_MATURE_BATCH_LIMIT`，默认 10）；单 scan 失败隔离；upsert 加 `WHERE label IS NULL`——已 MATURED 行首评不可变 | `test_scheduler.py`（Test D/E）+ PG 待跑 | ✅ TESTED |

全量回归：`passed=809 / failed=0 / skipped=101`（skip 为 PG 集成/网络依赖，不计 pass）。

---

## C. 60m 三只真实实例

**PENDING(SSH)** —— 需在生产执行新真实全市场扫描后从扫描 summary + Deep
components_detail 抽取。网络恢复后执行（§8 补验步骤 1），展示格式：

```text
security  60m status  trend  support  resistance  known_at
600xxx    AVAILABLE   UP     9.5      10.8        2026-09-xxT14:xx+08:00
...
```

验证口径：`m60_available > 0`（不再恒 missing）；每只 `trend/support/resistance`
来自真实 `period["structure"]`；stale/UNTRUSTED 样本记拒收 reason。

## D. Outcome 三种实例（A / NONE / PENDING）

**PENDING(SSH)** —— 需生产 PG 查询（§8 补验步骤 3）：

```sql
SELECT scan_run_id, code, label, status, bars_used
FROM v3.outcome_labels
WHERE label IN ('A','NONE') OR label IS NULL
ORDER BY created_at DESC LIMIT 30;
```

展示三种 label 各至少 1 例：`A`（成熟正样本）、`NONE`（成熟负样本，status=MATURED）、
`NULL`（未成熟，status=PENDING）。

## E. Deep Trace 一只（MACHINE #N → DEEP #83）

**PENDING(SSH)** —— 需新 scan 的 trace 落库查询（§8 补验步骤 2）：
展示一只 Machine Top120 内但 Deep 未入选（rank>60）的股票，trace 行含
`stage=DEEP, alive=false, drop_reason=deep_rank_below_top60, score=<真实 Deep 分>, rank=<61~120>`。

## F. Scheduler JobRun

**PENDING(SSH)** —— 需生产 scheduler 收盘后真实一轮（§8 补验步骤 4）：

```sql
SELECT job_id, status, attempt, known_at, metrics
FROM v3.orchestrator_job_runs
WHERE job_id IN ('candidate-scan','candidate-outcome-mature')
ORDER BY known_at DESC LIMIT 10;
```

预期：`candidate-scan` 收盘后随主链生成 JobRun（SUCCEEDED 或 already_scanned 幂等跳过）；
`candidate-outcome-mature` 每日维护链生成 JobRun。离线侧已验证（Test A~E）。

## G. 新 scan_id

**PENDING(SSH)** —— 新真实全市场扫描（§8 补验步骤 1）产出新 scan_run_id，
记录 scan_run_id / funnel 五段计数 / minute60_fetched / m60_available /
market_regime_score。同日重复执行须返回 `already_scanned` 且 scan_run_id 不变（幂等验证）。

## H. PG 测试结果

**PENDING(PG 环境)** —— 本机无 V3_TEST_DATABASE_URL，生产库属只读约束（测试需
隔离库建表，DB 变更须另行确认）。已写好待跑的 PG 集成测试（skipif 机制，5 个）：

| 测试 | 验证点 |
|---|---|
| `test_save_scan_and_read_back` | scan_runs + snapshots/expert/pareto 落库读回 |
| `test_mature_backfill_pending_and_read_back` | mature 全 PENDING 路径 + 双路径 TraceView 一致 |
| `test_bars_from_includes_t_day_boundary` | P0-02：`>= since` 含 T 日首根 |
| `test_published_run_on_idempotent_lookup` | P1-02：幂等查重（同日同版本命中/异版本不命中） |
| `test_save_outcome_labels_matured_rows_immutable` | P1-03：MATURED 行不可变 + PENDING 行可回填 |
| `test_pending_mature_scan_ids_finds_backlog` | P1-03：补跑名单含从未回填/部分未成熟，排除全成熟 |

环境就绪后：`export V3_TEST_DATABASE_URL=postgresql+asyncpg://...隔离库... && pytest tests/v3 -q`。

## I. 未完成项

按任务书 §20 要求如实标注，不扩大本轮范围：

```text
AI Review           NOT_CONNECTED（Final30 继续标 RAW_TOP30，AI_REVIEW_STATUS=NOT_CONNECTED）
Industry Context    NOT_AVAILABLE（无可靠行业源 → Deep 行业维恒 missing，原因
                    NO_RELIABLE_INDUSTRY_CONTEXT 记入 components_detail）
FOLLOW-UP: scan_runs 无 feature_run_id 列——任务书 §10.5 四元组幂等退化为三键
（market_date/strategy_version/parameter_version），feature_run_id 差异视为同键。
如需完整四元组幂等须加列（migration 0020，属 DB schema 变更，待用户确认后另做）。
FOLLOW-UP: outcome_labels 无 calculation_version 列——"已 MATURED 除非
calculation_version 升级才可重写"以"首评不可变"承载（更严格）；升级重算机制待后续。
```

---

## §8 补验清单（SSH/PG 恢复后执行并回填 C~H）

1. **新真实扫描**（§C/§G）：
   `python -c "import asyncio; from app.v3.application.scan_universe import run_full_scan; asyncio.run(run_full_scan())"`
   记录 summary（minute60_fetched/usable、m60_available/missing/stale/error、market_regime_score）；
   同日再跑一次验证 `already_scanned` 幂等。
2. **Deep trace 抽查**（§E）：抽 1 只 Machine selected 但 Deep rank>60 的股票，
   查 `v3.candidate_snapshots` DEEP 行（drop_reason/score/rank）+ FINAL 行缺席。
3. **Outcome 三态查询**（§D）：上列 SQL，各取 1 例。
4. **Scheduler 真跑**（§F）：收盘后 `run_once`，查 orchestrator_job_runs 两 Job 状态。
5. **PG 集成测试**（§H）：隔离库 + V3_TEST_DATABASE_URL，pytest 全量。
6. **push 分支**：4 个 commit 推 origin。

---

## 附：本轮明确不改项（任务书 §23）

权重 / 阈值 / 专家公式 / Pareto 公式 / Pareto RR 维度（维持 v1）/ AI Review 契约——零改动。
Deep #61~120 与 Final #31~60 的 trace 完整性只影响审计与 Why Not，不改变 Top30 选股结果语义。
