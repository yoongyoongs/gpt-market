# 多数据源、K 线缓存与自动降级设计

## 目标与不变边界

本改造只调整行情数据访问层。`score_candidate` 的 MA、趋势、量比、成交额、相对强度、位置和流动性权重未修改；主板过滤、预筛、Top30 排序参数未修改；MCP 工具名、GPT Web 路径和响应入口未修改。

业务层统一依赖关系：

```text
MCP / GPT Web / Live background refresh
                 |
Quote / Kline / Market / Sector / Scanner Service
                 |
           MarketDataService
            /           \
     KlineCache      ProviderManager
      L1 + L2        /             \
              EastmoneyProvider  TencentProvider
```

原链路中 `ScannerService` 直接调用一个 Eastmoney Provider 的 `get_kline(code, "day", 80)`。`push2his/push2delay` 偶发断链或返回 HTTP 200 但 `data.klines=[]`；异常在扫描 enrich 阶段被捕获后只追加“K线不可用，趋势指标未计分”，随后不含趋势分的候选仍被排序和缓存。因此数据缺失会改变 Top30，而不是一次扫描整体失败。

## Provider 规范与代码转换

`app/providers/base.py` 定义 `MarketDataProvider`。业务服务只拿到 `MarketDataService`，不会直接引用具体 Provider。

- 内部代码始终是六位代码，如 `603019`、`002284`。
- Eastmoney 在 Provider 内转换为沪市 `1.603019`、深市 `0.002284`。
- Tencent 在 Provider 内转换为 `sh603019`、`sz002284`。
- 扫描日 K 统一使用 `adjust=qfq`。Eastmoney 使用 `fqt=1`，Tencent 使用 `qfqday`，不会把 raw 与 qfq 混算。
- 腾讯 K 线成交量从“手”换算成“股”；腾讯历史 K 接口不稳定提供成交额时标准化为 `0.0`。现有趋势指标只使用 OHLC，不受该占位值影响。

Provider 返回 `null`、空 K 线、无效价格、解析失败或 HTTP 失败都视为失败，HTTP 200 不等于业务成功。异常分类包含 `NETWORK_ERROR`、`TIMEOUT`、`HTTP_ERROR`、`EMPTY_DATA`、`PARSE_ERROR`、`INVALID_SYMBOL`、`RATE_LIMIT`、`UNSUPPORTED` 和 `ALL_PROVIDER_FAILED`；`CACHE_MISS` 作为独立计数指标记录，不伪装成 Provider 异常。

## ProviderManager 与健康状态

默认顺序为 Eastmoney primary、Tencent secondary。每个源最多尝试 2 次；第一次失败后随机等待 200–500 ms，再重试。一个源耗尽后切换另一个源。两个源均失败时抛出包含每次源、异常分类、类型和消息的 `AllProvidersFailedError`。

每个 Provider 记录请求、成功、失败、超时、空数据、平均延迟、连续失败数、最近成功/失败时间、最近异常分类和消息。Eastmoney 连续失败 5 次后进入 30 秒 `DEGRADED`，期间腾讯优先；时间到后 Eastmoney 自动重新参与，不会永久禁用。

只读状态端点：

```text
GET /health/providers
```

它还包含 K 线累计指标、L1 条目数、最近 K 线错误聚合和最近一次 `SCAN SUMMARY`。

## 两级 K 线缓存

L1 是进程内字典，L2 是 SQLite，默认文件为：

```text
data/kline_cache.sqlite3
```

Docker 中使用 `/data/kline_cache.sqlite3`，Compose 将宿主机 `./data` 挂载到 `/data`。SQLite 开启 WAL。主键是 `(symbol, period, adjust, trade_date)`；日/周/月使用日期键，分钟周期使用完整时间戳键，保存 OHLC、成交量、成交额、来源、provisional 和更新时间。

读取顺序：

1. 读取 L1；不足时读 SQLite。
2. 数量足够且未到刷新阈值，直接返回缓存。
3. 需要刷新时，仅拉取最近 10 根并按交易日合并；首次无缓存才按请求数量初始化。
4. 成功结果写 SQLite 并原子更新 L1。
5. 两个网络源都失败且已有足量缓存，立即返回旧缓存并标记 `stale=true`、`quality=OLD`。
6. 两个源都失败且无缓存，才返回 `ALL_PROVIDER_FAILED`，扫描器将该股票标记为 K 线不可用。

周/月 Provider 请求失败后，服务会先用同复权口径的日 K 聚合；15/30/60 分钟请求失败后，会先用 5 分钟 K 按沪深交易时段聚合。聚合结果带明确 `aggregate:*` 来源，不会冒充 Provider 直出结果。完整能力和服务器验收见 [周期能力矩阵与修复验收](period-capability-audit-20260829.md)。

交易时段刷新阈值默认 300 秒，非交易时段 1800 秒。全局网络 K 线并发上限默认 8；扫描器原有 shortlist 并发仍保留，但真正触网会再经过这个全局 Semaphore。

## 盘中 provisional 日 K

扫描器把同一全市场快照中的 Quote 传给 `MarketDataService.get_kline`。盘中今日临时 K 使用：

```text
open=今日开盘价
high=今日最高价
low=今日最低价
close=当前价
volume=当前成交量
amount=当前成交额
provisional=true
```

它与缓存中的正式历史 K 按交易日合并，用于 MA5/MA10/MA20/MA60 等计算，但 15:10 前绝不写入 SQLite。收盘后由 Provider 的正式日 K 增量结果替换。

## 扫描可观测性

每次真正执行（非 15 秒 ScanResult TTL 命中）的扫描会记录单行 `SCAN SUMMARY`，包含：股票总数、Quote 成败、所需 K 线数、缓存命中、网络刷新、成功/失败、旧缓存使用、两个 Provider 的成功/失败/空数据/超时、可用率、命中率、Top 中 MA5/MA10/MA20 可用数量、来源分布、聚合异常和总耗时。

MA10 会被指标服务计算，但现有 `score_candidate` 并未使用 MA10 权重；为遵守“不修改评分算法”，报告会分别说明“MA10 数据可用数量”和“MA10 实际参与评分数量为 0”。

## 运维与故障验证

```bash
curl http://127.0.0.1:8000/health/providers
docker logs market-mcp 2>&1 | grep 'SCAN SUMMARY'
sqlite3 data/kline_cache.sqlite3 'select count(*) from kline_bars;'
```

发布验收应覆盖：Eastmoney 失败由 Tencent 接管；两个源失败但有缓存仍返回旧数据；两个源失败且无缓存才不可用；连续三次实际扫描中后两次缓存命中明显提高；MCP、GPT JSON 和 Live HTML 仍正常，且 Live handler 只读取预渲染内存快照。
