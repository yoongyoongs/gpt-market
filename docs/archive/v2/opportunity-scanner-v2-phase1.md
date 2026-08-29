# scan_mainboard V2 Phase 1 验收文档

最后更新：2026-08-28

## 目标

V2 的目标不是寻找“今天涨得最猛”的股票，而是在不引入自动交易的前提下，从全市场中筛出未来几天到几周值得人工重点观察的股票。Phase 1 只使用当前项目已有且可验证的数据：全市场 Quote、日 K、周 K、指数涨跌幅、Provider 健康状态与现有缓存。基本面、估值、公告新闻、政策产业催化和主力资金暂不伪造。

## V1 保留方式

V1 没有删除，也没有替换默认接口。

| 类型 | V1 调用 |
|---|---|
| REST | `GET /scan` |
| GPT Web JSON | `GET /gpt/{secret}/scan` |
| MCP | `scan_mainboard` |

V1 仍返回 `ScanResult.candidates[]`，并按 `total_score` 排序。

## V2 调用方式

| 类型 | V2 调用 |
|---|---|
| REST | `GET /scan/v2?top_n=30&pool_size=420` |
| GPT Web JSON | `GET /gpt/{secret}/scan/v2?top_n=30&pool_size=420` |
| GPT Web HTML | `GET /gpt/{secret}/scan/v2/html?top_n=30&pool_size=420` |
| MCP | `scan_mainboard_v2` |
| A/B | `GET /gpt/{secret}/scan/ab` 或 MCP `scan_mainboard_ab` |

V2 返回 `OpportunityScanResult`，包含 `raw_top30`、`action_top30`、`top100`、`candidate_pool_size`、`channel_counts`、`score_version=v2`、`score_formula` 和 `missing_data_sources`。

## 候选池

V2 先执行和 V1 类似的主板硬过滤：排除创业板、科创板、北交所、非主板前缀、ST/退市、停牌、低成交额、涨跌停和一字板。与 V1 不同的是，V2 不把 `pct_change > 5%` 作为绝对剔除条件；涨幅过热交给 `risk_penalty`。

硬过滤后，通过 quote-only 多通道并集生成 300–500 只宽候选池：

- `trend_improvement_proxy`：当日涨跌幅接近启动区间的代理。
- `low_position_proxy`：涨幅不高且量能未完全沉寂的低位代理。
- `flow_activity`：量比和当日表现显示资金活跃。
- `relative_strength`：相对强势通道。
- `liquidity_floor`：成交额靠前，保证能正常观察和进出。

预筛只负责扩大研究面并排除明显不适合对象，不提前替最终评分做决定。

## V2 评分公式

```text
opportunity_score = clamp(
  position_score(15)
  + fundamental_score(15, Phase1 missing)
  + trend_score(20)
  + flow_score(15)
  + catalyst_score(10, Phase1 missing)
  + risk_reward_score(20)
  + liquidity_score(5)
  + risk_penalty(0..-20),
  0,
  100
)
```

每个子分都保留：

```text
raw_value / normalized_value / score / max_score / reason / data_source / data_timestamp / coverage
```

缺失数据不会被当成 0，也不会生成看似精确的高分结论。Phase 1 中 `fundamental_score` 和 `catalyst_score` 为 `null`，并在 `data_quality.missing_fields` 中列明。

## 子模块

### position_score 15

基于 260 日 K 计算 250 日、120 日、60 日位置，距 20/60/120 日高点，近 20/60/120 日收益，MA20/MA60 偏离和近 20 日波动收敛。低位不是自动高分；长期深跌但未止跌会被谨慎处理。

### trend_score 20

周 K 8 分，日 K 12 分。周 K 判断 `IMPROVING`、`BASE_BUILDING`、`MIXED`、`DECLINING`、`UNKNOWN`。日 K 判断 `TURNING_UP`、`REPAIRING`、`BASE_BUILDING`、`WEAK`。若周 K 明确下降，日 K 上涨按下降趋势中的反弹处理，并限制趋势分。

### flow_score 15

使用当前成交量与 20 日均量、量比、近 5 日上涨/下跌量能、放量突破和相对指数表现。成交额主要放在 `liquidity_score`，不再像 V1 一样在多个模块重复大幅加分。

### risk_reward_score 20

根据 20/60/120/250 日高低点、MA20/MA60 和 ATR14 推导支撑、压力、ATR 缓冲止损与目标位。

```text
RR < 1      => 0
1 ~ 1.5    => 4
1.5 ~ 2    => 8
2 ~ 3      => 14
>= 3       => 20
```

### liquidity_score 5

只回答“是否能正常进出”，考虑成交额、换手率、停牌和一字板。

### risk_penalty 0~-20

识别近 20/60 日涨幅过大、极端量比、极端换手、周 K 下降、RR<1、涨停或一字板等风险。重大风险可触发 `hard_reject=true`。

## A/B 与分级

`raw_top30` 完全按 `opportunity_score` 排名，不人工改榜。`action_top30` 当前等于 `raw_top30`，因为 Phase 1 尚无可验证行业分类；Phase 2/3 接入申万二级行业后，可配置同一行业最多保留数量。

A级不会强制生成。A级至少要求高机会分、基本面覆盖充分、无重大风险、周 K 非明确下降、RR 原则上大于等于 2、趋势有效改善、资金量价有确认。Phase 1 因基本面和催化缺真实源，通常只会产生 B/C。

## 历史快照

每次实际执行 V1/V2 扫描都会写入 JSONL 快照，路径由 `SCAN_HISTORY_PATH` 配置，本地默认 `data/scan_history`，Docker 默认 `/data/scan_history`。每条记录包含：

- `date/code/name/rank/price/score_version`
- 全部子分、总分、grade、支撑、压力、止损、目标位、RR
- 完整原始评分输入和评分解释
- T+1/T+3/T+5/T+10/T+20、最大上涨、最大回撤、止损/目标触发占位字段

Phase 1 不实现完整回测引擎，但历史结构已可承接后续统计。

## 真实验收结果

本地确定性测试：

```text
53 passed, 5 skipped
```

服务器部署后验证：

```text
/health => {"status":"ok"}
/scan/v2?top_n=1&pool_size=300 => score_version=v2, coverage=5905/5905
container health => healthy
```

服务器真实 V2 扫描摘要：

```text
stocks_total=5905
quotes_success=5905
filtered_mainboard=2329
candidate_pool_size=300
coverage_rate=1.0
duration_seconds=21.809
```

东方财富周 K 请求大面积断连时，系统曾按设计输出 `week_trend=UNKNOWN`，没有伪造周线结论。2026-08-29 数据层修复后，周 K 可由 Tencent 前复权日 K 本地聚合并明确标注来源；V2 评分公式、权重和排序规则未修改。详见 [周期能力矩阵与修复验收](period-capability-audit-20260829.md)。

## 当前 Top30 样例

以下为 2026-08-28 部署验收时的实时样例，不是可回放历史行情：

| # | 代码 | 名称 | opportunity_score | grade | RR | 周K | 日K |
|---:|---|---|---:|---|---:|---|---|
| 1 | 000089 | 深圳机场 | 60.0 | B | 3.6024 | UNKNOWN | TURNING_UP |
| 2 | 601360 | 三六零 | 57.5 | B | 3.3591 | UNKNOWN | TURNING_UP |
| 3 | 600872 | 中炬高新 | 55.5 | B | 4.6182 | UNKNOWN | REPAIRING |
| 4 | 601390 | 中国中铁 | 52.0 | C | 3.0524 | UNKNOWN | TURNING_UP |
| 5 | 601229 | 上海银行 | 51.5 | C | 3.2753 | UNKNOWN | TURNING_UP |

## 未完成项

- 基本面、财务风险、估值和行业相对估值。
- 业绩拐点、扣非净利润、现金流和低基数误判防护。
- 公告、新闻、政策和产业催化。
- 主力/大单资金。
- 申万二级行业分类和 `action_top30` 行业集中度控制。
- 60 分钟/15 分钟 `entry_score`。
- 完整 T+N 回测统计和 V1/V2 长期对比。

## Phase 2 建议

优先接入可审计的财务和估值数据源，新增按天缓存，并把 `fundamental_score` 从 `coverage=false` 升级为真实评分。评分必须同时检查营收、净利润、扣非、经营现金流、毛利率、ROE、负债率、PE/PB 和连续季度趋势，防止低基数同比造成误判。
