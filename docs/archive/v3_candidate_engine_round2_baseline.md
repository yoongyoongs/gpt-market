# V3 候选引擎 第二轮问题基线（Round2 Baseline）

> 记录时间：2026-09-08（第二轮整改开始前，修复未动任何代码）
> 基线版本：`v3-candidate-engine-round1` tag（main @ dc5a666）
> 数据来源：生产库 scan `2fd049b4-f53a-4d0c-9e7b-f58c77111f0f`（经 REST API 读取）
> 用途：第二轮修复前的对照基线。修复后新扫描必须与本文数字对照。

## 1. 基线扫描

```text
scan_id        = 2fd049b4-f53a-4d0c-9e7b-f58c77111f0f
feature_run_id = 893e4b2a-dbc4-453a-b733-00d997bfeafb（as_of=2026-09-07 10:00 UTC）
scan_time      = 2026-09-08 06:53 UTC
status         = PUBLISHED
duration_ms    = 3241
```

## 2. 漏斗

```text
UNIVERSE  5556
SAFETY    5261   （淘汰 295：ST 200 / 缺日K 86 / 停牌 6 / ST+停牌 3）
RECALL    1117
ENRICH    1117
PARETO    275
MACHINE   120
DEEP      60
FINAL     30
```

## 3. 各专家 hit_count（RECALL 阶段）

```text
LP    300
BT    250
RV    250
AC    250
PB    180
RS    0     ← 已确认缺失：benchmark 序列未传入（P0-04）
FQ    0     ← 已确认缺失：evidence 未绑定（P0-03）
CAT   0     ← 已确认缺失：evidence 未绑定（P0-03）
```

专家 reasons 中 `higher_low_60m:missing`、`trendline_break:missing`、`restrong_60m:missing`
普遍出现 → 60m 数据链 missing（P1-01 范围）。

## 4. Pareto 现状

真实分布（/scan/{id}/pareto 全量 rows 按 front 分组）：

```text
front=0（protected）17 只
Front1  30
Front2  43
Front3  39
Front4  36
Front5  39
最深层  front=187（大量候选互不支配）
```

已知问题（P0-01/P0-02）：

```text
selected 258（F1~F3=112 不足 target_min=250，但 RRF 截断先于 front 保护）
protected_count 17
protected entry front=0 / crowding_distance=0.0（真实 front/rrf 信息丢失）
crowding 在截断之后才计算，未参与 survivor 选择
selected + protected 突破 target_max=400 的预留名额语义
```

## 5. Machine / Deep / Final

```text
Machine Top120（未入 Top120 者 trace 无 rank —— P0-08）
Deep   Top60
Final  30（当前语义 = RAW_TOP30，AI Review 未接入 —— P1-05）
```

## 6. Backtest Metrics 现状（bug 实录）

`GET /api/v3/backtest/metrics`（labeled_count=0，T+20 未成熟）：

```text
recall  top300   k=120  ← 实际只从 MACHINE alive(Top120) 取，Recall@300 == Recall@120（P0-12）
recall  top120   k=120
recall  top60    k=60
recall  top30    k=30
precision        k=10/20/30 分母固定 K，即使池中不足 K 只（P0-12）
所有 value=0.0   ← 未成熟返回 0 而非 PENDING/null（P0-12）
无 NDCG 字段     ← 现有 NDCG ideal 取自身 gains（P0-12）
```

## 7. Shadow Pool 现状（bug 实录）

```text
总数 106（目标 200~300 —— P0-10）
single_expert  6
random         50
other          50
near_miss      组缺失（near_rank_ceiling=max(size,100) 恒假，永不产生）
无 shortfall_reason
```

## 8. 已知缺口清单（修复前如实记录）

```text
RS 是否真实出分          否（benchmark_closes 未传入）
FQ 是否真实出分          否（ExpertInput.evidence=() 未绑定）
CAT 是否真实出分          否（ExpertInput.evidence=() 未绑定）
60m 是否 missing         是（专家 reasons + Deep 无 60m 组件）
Market Regime 是否 missing 是（Deep 未消费 regime 快照）
Industry Context 是否 missing 是（relative_industry_strength 有字段无行业数据源）
AI Review 是否接入        否（Final=RAW_TOP30，AI_REVIEW_STATUS 未声明）
Trace 未入选者            Machine #121~ 丢 rank/score；Deep #31~60 Final 外丢 rank（P0-08）
TraceView 重建            stage 字符串字典序排序，非业务顺序（P0-09）
Miss Audit experts        只存专家名列表，无 rank（P0-11）
weighted_combine          missing 未按有效权重归一，双重惩罚（P0-05）
RR upside                 -dist_high 而非 -dist_high/(1+dist_high)（P0-06）
周K下降反转               deceleration>0 即解除冲突，过宽（P0-07）
scan/trace 认证           公开可读（P0-14）
```

## 9. 第二轮修复后验收对照要求

新真实扫描（不得复用本 scan_id）必须输出：

```text
新 scan_id、漏斗各级、8 专家 hit（RS>0、FQ/CAT 数据链绑定）、
Pareto Front1~Front5 + selected + protected + target_max 约束证明、
Machine 未入选 trace 带 rank、Shadow near_miss 组 + 总量 200~300（不足给 shortfall_reason）、
Metrics pool 语义（top300≠top120 / Precision@60[DEEP] / NDCG ground truth / PENDING）、
认证 401/200 实测。
```
