# Phase2A 基本面数据与评分设计

## 实施边界

Phase2A 只接入 `scan_mainboard_v2`。V1、MCP、行情 Provider、K 线缓存、Phase1 的位置/趋势/量价/风险收益/流动性算法均保持不变。新闻、政策、主力资金和催化评分不在本阶段实现。

## 数据链路

```text
ScannerService.scan_mainboard_v2
  -> FundamentalProviderManager.get_many(candidate_codes)
       -> EastmoneyDatacenterFundamentalProvider（批量主源）
       -> EastmoneyF10FundamentalProvider（主源失败或核心覆盖不足时 fallback）
       -> 字段级合并、冲突记录、6 小时内存缓存
  -> score_fundamental(FundamentalSnapshot)
```

评分代码只依赖 `FundamentalSnapshot`，不引用任何第三方 Provider。每次 V2 扫描按 40 只一组批量请求；估值、财务主指标、业绩预告和业绩快报并行读取，可选数据失败不会拖垮财务主指标。缓存键为 `fundamental:{code}`，默认 TTL 为 21600 秒，可通过 `FUNDAMENTAL_CACHE_SECONDS` 调整。

## 标准化字段

每个最新字段包含：

- `value`
- `source`
- `upstream_source`
- `source_type`
- `report_period`
- `fetch_time`
- `coverage`
- `stale`
- `confidence`
- `error`
- `conflicts`

`quarterly_trend` 保存最近最多 8 期的营收、同比/环比、净利润、扣非、经营现金流、ROE、毛利率和负债率，并保留报告期、来源、抓取时间、覆盖率和错误语义。业绩预告、业绩快报、审计意见也使用带元数据的字段对象；当前无法取得审计意见时明确返回 `coverage=false` 和错误原因，不解释成“无审计风险”。

## 评分与风险

`fundamental_score` 范围为 0–15，按可用维度评价：

- 业绩改善与拐点：5 分
- 盈利质量与现金流：3 分
- ROE：2 分
- 负债：2 分
- PE/PB 相对候选池同行业中位数：3 分

核心覆盖率低于 45% 时返回 `score=null`，缺失字段不按 0 分处理。部分覆盖时只在已覆盖维度内标准化，覆盖率低于 75% 的结果最高限制为 12 分。

利润同比高增长只有在上一期利润为正且利润率不属于极低基数时才足额计分；扣非利润显著低于归母利润会标记一次性收益风险。

基础财务风险单独生成 `fundamental_risk`，再与原技术风险合并到总 `risk_penalty`，总风险仍保持原有 `-20..0` 边界。当前规则识别持续亏损、利润恶化、盈利但经营现金流连续为负、异常高负债、扣非/归母不匹配，以及在真实审计字段可用时识别非标准审计意见。

## API 与页面

- `GET /fundamental/{code}`：标准化基本面对象。
- `GET /gpt/{secret}/stock/{code}/fundamental`：GPT Web 读取接口。
- `GET /gpt/{secret}/scan/v2`：候选的 `fundamental_score`、`fundamental_risk_penalty`、`score_breakdown.fundamental`、`raw_inputs.fundamental`。
- `GET /gpt/{secret}/scan/v2/html`：展示基本面得分、风险扣分、报告期、覆盖率、来源、冲突及逐字段元数据。

## 明确未实现

Phase2A 不接新闻、政策、主力资金或催化数据。审计意见 Provider 仍待后续官方披露适配器接入；当前只保留标准字段和风险规则入口，并明确标记不可用。
