# API 与 MCP 工具参考

本文描述当前 1.0.0 接口。业务字段以 `app/models.py` 为唯一 schema 来源。

## 1. 基础地址和认证

| 类型 | 地址 | 认证 |
|---|---|---|
| 健康检查 | `GET /health` | 无 |
| 数据源健康 | `GET /health/providers` | 无；建议只在受控网络开放 |
| MCP | `/mcp/` | `Authorization: Bearer <MCP_TOKEN>` |
| GPT Web | `/gpt/{secret}/...` | URL 中的 `GPT_WEB_SECRET` |
| REST 调试 | `/quote`、`/market` 等 | 当前版本无认证，仅用于受控环境调试 |

生产环境应在 Nginx 或防火墙限制 REST 调试端点，不应依赖其公开可访问性。

### V3 Phase 3 READ（Feature Flag）

以下端点仅在 `V3_ENABLED=true` 且 PostgreSQL 已迁移至 `0003_full_market_features` 时可用；禁用时返回 HTTP 503：

| 路由 | 说明 |
|---|---|
| `GET /api/v3/universe/features` | 查询最新或指定 Published Feature Run |
| `GET /api/v3/universe/query` | 上述接口的兼容别名 |
| `GET /api/v3/market-regime` | 读取最新 Published Run 对应的事实型 Regime |

Feature Query 参数包括 `feature_run_id`、`market`、`stale`、`sort_by`、`descending`、`min_value`、`max_value`、逗号分隔的 `fields`、`limit` 和 `cursor`。默认 50、最大 200；字段和排序键使用白名单，下一页必须原样沿用排序参数。当前部署仍默认关闭 V3，这些接口不代表 V3 已在生产启用。

## 2. MCP tools

| 工具 | 主要参数 | 返回模型 |
|---|---|---|
| `get_quote` | `code` | `QuoteResponse` |
| `get_quotes` | `codes`，最多 100 个 | `list[QuoteResponse]` |
| `get_kline` | `code, period=day, limit=120` | `KlineResult` |
| `get_stock_detail` | `code` | `StockDetailResponse` |
| `get_market_overview` | 无 | `MarketOverviewResponse` |
| `get_sector_ranking` | `sector_type=industry, limit=30` | `SectorRankingResponse` |
| `scan_mainboard` | 扫描参数 | `ScanResponse` |
| `get_scan_coverage` | 无 | `CoverageResponse` |
| `scan_mainboard_v2` | `top_n=30, pool_size=420` | `OpportunityScanResponse` |
| `scan_mainboard_ab` | `top_n=30` | `{v1_top30, v2_top30}` |

## 3. GPT Web 路由

| 路由 | 对应 MCP tool |
|---|---|
| `GET /gpt/{secret}/stock/{code}` | `get_quote` |
| `GET /gpt/{secret}/stocks?codes=002284&codes=600519` | `get_quotes` |
| `GET /gpt/{secret}/stock/{code}/kline?period=day&limit=120` | `get_kline` |
| `GET /gpt/{secret}/stock/{code}/detail` | `get_stock_detail` |
| `GET /gpt/{secret}/market` | `get_market_overview` |
| `GET /gpt/{secret}/sectors?sector_type=industry&limit=10` | `get_sector_ranking` |
| `GET /gpt/{secret}/scan?top_n=10` | `scan_mainboard` |
| `GET /gpt/{secret}/scan/v2?top_n=10&pool_size=420` | `scan_mainboard_v2` |
| `GET /gpt/{secret}/scan/v2/html?top_n=30&pool_size=420` | V2 HTML Top30 与评分拆解页面 |
| `GET /gpt/{secret}/scan/ab?top_n=30` | V1/V2 并行 A/B |
| `GET /gpt/{secret}/scan/coverage` | `get_scan_coverage` |
| `GET /gpt/{secret}/coverage` | 读取 Live 缓存中与当前页面完全一致的完整覆盖报告 |

### Live Refresh HTML Adapter

| 路径 | 说明 |
|---|---|
| `GET /gpt/{secret}/live` | 立即读取后台成功快照，渲染市场、覆盖率、行业/概念 Top20 及股票 Top30，并生成新 nonce 链接 |
| `GET /gpt/{secret}/live/{nonce}` | 立即读取同一个内存快照并渲染 HTML，不调用行情源 |
| `GET /gpt/{secret}/live/{nonce}/stock/{code}` | 读取快照内的共享 QuoteService 结果，不调用行情源 |

每个页面生成的后续链接都使用 `secrets.token_urlsafe(24)` 产生新的 nonce。所有 live 页面返回 `text/html`，并设置：

```text
Cache-Control: no-store, no-cache, must-revalidate, max-age=0
Pragma: no-cache
Expires: 0
```

Header 是第二道防线；核心机制仍是每次点击进入不同 URL。原 JSON 路由及 MCP 工具不受影响。

Live 缓存状态包含 `snapshot_time`、`server_time`、`age_ms`、`market_status`、`stale` 和可选 `warning`。后台刷新原子替换整个成功快照；刷新异常时保留上一份数据并设置 `stale=true`。首份快照尚未生成时立即返回 HTTP 503 HTML，内容为 `INITIALIZING / market snapshot is initializing`。

Web 成功响应：

```json
{
  "ok": true,
  "data": {}
}
```

`data` 与对应 MCP tool 的业务结果逐字段相同。

## 4. 公共行情元数据

除纯 Kline 行外，主要成功模型包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `source` | string | 当前为 `eastmoney` |
| `source_timestamp` | ISO datetime | 兼容字段；对 f86/f124 仅代表 Provider 返回的更新时间，不能解释为最后成交时间 |
| `data_timestamp` | ISO datetime | 兼容字段，等于源时间 |
| `server_timestamp` | ISO datetime | 标准对象生成时间 |
| `age_seconds` | number | 数据年龄 |
| `stale` | boolean | 是否不再 LIVE |
| `quality` | enum | `LIVE/STALE/OLD/UNAVAILABLE/CONFLICT` |
| `timestamp_source` | enum | `eastmoney/fetch_time` |
| `snapshot_id` | string | 标准行情快照标识 |
| `confidence` | enum | `HIGH/MEDIUM/LOW` |

## 5. QuoteResponse

```json
{
  "code": "002284",
  "name": "亚太股份",
  "market": "SZ",
  "price": 9.8,
  "prev_close": 9.84,
  "open": 9.79,
  "high": 9.82,
  "low": 9.69,
  "pct_change": -0.41,
  "change": -0.04,
  "volume": 23110900,
  "amount": 225577871.29,
  "turnover_rate": 3.16,
  "volume_ratio": 0.65,
  "amplitude": 1.32,
  "suspended": false,
  "source": "eastmoney",
  "source_timestamp": "2026-08-27T15:34:15+08:00",
  "data_timestamp": "2026-08-27T15:34:15+08:00",
  "server_timestamp": "2026-08-27T15:34:16+08:00",
  "age_seconds": 1.0,
  "stale": false,
  "quality": "LIVE",
  "timestamp_source": "eastmoney",
  "snapshot_id": "snapshot-20260827T153415.000",
  "confidence": "HIGH"
}
```

价格、涨跌、成交等字段在源端缺失时为 `null`，不会自动补值。

## 6. KlineResult

`period` 可选：`1m/5m/15m/30m/60m/day/week/month`；`limit` 范围 1–1000。

```json
{
  "code": "002284",
  "period": "day",
  "klines": [
    {
      "timestamp": "2026-08-27T00:00:00+08:00",
      "open": 9.79,
      "high": 9.82,
      "low": 9.69,
      "close": 9.80,
      "volume": 20355600,
      "amount": 198606168.71,
      "provisional": false
    }
  ],
  "source": "eastmoney",
  "source_timestamp": "2026-08-27T15:34:15+08:00",
  "data_timestamp": "2026-08-27T15:34:15+08:00",
  "server_timestamp": "2026-08-27T15:34:16+08:00",
  "age_seconds": 1.0,
  "stale": false,
  "quality": "LIVE",
  "timestamp_source": "eastmoney",
  "snapshot_id": "snapshot-20260827T153415.000",
  "confidence": "HIGH"
}
```

日 K 在盘中可能附加由同一 Quote 快照生成的今日行，此时 `provisional=true`；该行不在 15:10 前持久化。正式历史行均为 `false`。`source` 可能为 `eastmoney`、`tencent`、`cache:eastmoney`、`cache:tencent`，与盘中 Quote 合并时会追加 `+<quote source>`。这些是向后兼容的来源/状态扩展，路由和既有字段不变。

## 7. StockDetailResponse

包含 `quote`、`technical`、`day_klines` 和 `minute_5_klines`。`technical` 字段：

- `ma5/ma10/ma20/ma60`
- `atr14/rsi14`
- `high_20d/low_20d/high_60d/low_60d`
- `distance_ma20_pct/distance_high_20d_pct`
- `return_5d/return_20d`

历史长度不足时，对应指标为 `null`。详情的公共元数据与内部 `quote` 使用同一快照。

## 8. MarketOverviewResponse

```json
{
  "indices": {
    "shanghai": {"code": "000001", "name": "上证指数", "price": 0, "pct_change": 0},
    "shenzhen": {"code": "399001", "name": "深证成指", "price": 0, "pct_change": 0},
    "chinext": {"code": "399006", "name": "创业板指", "price": 0, "pct_change": 0}
  },
  "breadth": {
    "up_count": 0,
    "down_count": 0,
    "flat_count": 0,
    "limit_up_count": {"value": null, "available": false},
    "limit_down_count": {"value": null, "available": false}
  },
  "amount": 0
}
```

示例中的 0 仅用于说明结构，不是行情样例。

## 9. SectorRankingResponse

`sector_type` 只能为 `industry` 或 `concept`，`limit` 范围 1–100。每项包含名称、板块代码、涨跌幅、成交额、涨跌家数、排名和可选历史排名字段。

未找到可靠源字段的 `limit_up_count`、`rank_10m_ago`、`rank_30m_ago`、`rank_change_10m`、`rank_change_30m` 返回 `null`。

## 10. ScanResponse

请求参数：

| 参数 | 默认值 | 限制/含义 |
|---|---:|---|
| `top_n` | 30 | 1–100 |
| `max_pct_change` | 5.0 | 排除涨幅更高标的 |
| `min_amount` | 50000000 | 最低成交额，元 |
| `exclude_st` | true | 排除 ST、*ST、退市名称 |
| `exclude_limit_up` | true | 排除涨停 |
| `exclude_limit_down` | true | 排除跌停 |

返回：

- `coverage.total/success/filtered_mainboard/failed/coverage_rate`
- `candidates[]`
- `scan_id`
- 公共质量元数据

每个候选包含行情、MA、五维分数、`total_score`、`reason[]` 和其 Quote 的 `snapshot_id`。

## 10.1 OpportunityScanResponse

V2 响应不替换 V1，独立由 `/scan/v2`、`/gpt/{secret}/scan/v2` 和 MCP `scan_mainboard_v2` 返回。

请求参数：

| 参数 | 默认值 | 限制/含义 |
|---|---:|---|
| `top_n` | 30 | 1–100 |
| `pool_size` | 420 | 300–500，宽候选池大小 |
| `min_amount` | 50000000 | 最低成交额，元，仅作流动性门槛 |
| `exclude_st` | true | 排除 ST、*ST、退市名称 |
| `exclude_limit_up` | true | 排除涨停 |
| `exclude_limit_down` | true | 排除跌停 |

返回顶层字段：

- `coverage`
- `raw_top30`
- `action_top30`
- `top100`
- `candidate_pool_size`
- `channel_counts`
- `score_version=v2`
- `score_formula`
- `missing_data_sources`
- `duration_seconds`

每只股票至少包含：

```json
{
  "stock_code": "002284",
  "stock_name": "亚太股份",
  "opportunity_score": 61.5,
  "position_score": 12.5,
  "fundamental_score": null,
  "trend_score": 17.0,
  "flow_score": 12.0,
  "catalyst_score": null,
  "risk_reward_score": 14.0,
  "liquidity_score": 5.0,
  "risk_penalty": -2.0,
  "grade": "B",
  "support": 9.3,
  "resistance": 10.8,
  "stop_loss": 9.0,
  "target_1": 10.8,
  "target_2": 11.6,
  "risk_reward_ratio": 2.2,
  "week_trend": "BASE_BUILDING",
  "day_trend": "TURNING_UP",
  "data_coverage": {},
  "data_quality": {},
  "score_version": "v2",
  "score_breakdown": {},
  "score_formula": "..."
}
```

`score_breakdown` 中每个模块保存 `raw_value / normalized_value / score / max_score / reason / data_source / data_timestamp / coverage`。Phase 1 不接入真实基本面和催化数据，因此 `fundamental_score` 与 `catalyst_score` 为 `null`，并在 `missing_fields` 中列明。

## 11. CoverageResponse

覆盖率状态：

- `FULL`：≥ 90%
- `BROAD`：≥ 60% 且 < 90%
- `PARTIAL`：< 60%

完整报告的 `coverage_rate` 固定按 `quotes_success / quotes_requested` 计算，不能用过滤后的主板数量或 Top30 反推。除公共质量元数据、`snapshot_id`、`scan_id` 和时间戳外，还包括：

- `total_securities`、`quotes_requested/success/failed`、`filtered_mainboard`
- 各市场、ST、停牌、流动性、涨跌停等互斥过滤计数
- `industry_total/success`、`concept_total/success`
- `scan_candidates_total`、`scan_top_n`
- `freshness.live/stale/old/unavailable`
- `failure_sources` 与 `missing_fields` 汇总
- 基金、ETF、LOF、REIT 的预留可空字段

这些数字由当前扫描和板块读取过程顺带记录；`/coverage` 和 `/live` 只读取已发布内存快照，不会为统计再抓取一遍股票行情。

返回最新扫描的证券总数、成功/失败数、过滤后主板数、扫描时间、数据年龄、覆盖率和 `scan_id`。

## 12. 调用示例

Web：

```bash
curl "https://HOST/gpt/$GPT_WEB_SECRET/stock/002284"
curl "https://HOST/gpt/$GPT_WEB_SECRET/sectors?sector_type=industry&limit=10"
curl "https://HOST/gpt/$GPT_WEB_SECRET/scan?top_n=10"
```

MCP：

```bash
MCP_URL=https://HOST/mcp/ MCP_TOKEN=YOUR_TOKEN \
  python scripts/test_mcp_client.py
```

不要把真实 secret 或 token 写入命令历史、截图、Issue 或公开 CI 日志。
