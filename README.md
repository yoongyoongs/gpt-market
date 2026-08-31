# 极简 A 股实时行情 Remote MCP

Python 3.12 服务，向 ChatGPT 或其他客户端提供 A 股行情、全市场研究和 V3 决策辅助能力。生产已部署 V1/V2 与 V3 API、PostgreSQL 17 和 Phase 2 Worker；V3 账户事实与不可变成交账本只接受人工确认，不连接券商、不自动交易。

V3 按 [架构设计实施稿](docs/架构设计实施稿.md) 分阶段建设。Phase 1–11 已完成技术验收；Phase 7–11 的隔离 PostgreSQL 17、迁移往返、并发和全项目回归证据见 [Phase 7–11 验收报告](docs/Phase7-11技术验收报告.md)。2026-09-01 已在生产启用 V3 API 和独立数据库，策略发布状态保持保护性的 `mode=V2`，尚未激活任何 V3 策略版本；详见 [工作状态](docs/工作状态.md)。

## 接手开发必读

换电脑、换模型或新会话时，不要依赖聊天记录恢复上下文。必须按顺序阅读：

1. 本 README；
2. [当前工作状态](docs/工作状态.md)；
3. [V3 需求规格说明](docs/需求规格说明.md)、[需求追踪矩阵](docs/需求追踪矩阵.md)与[系统功能架构](docs/系统功能架构.md)；
4. [技术架构](docs/技术架构设计.md)、[数据库设计](docs/数据库设计.md)和[详细设计](docs/详细设计.md)；
5. [V3 架构设计实施稿](docs/架构设计实施稿.md)与[功能清单](docs/功能清单与开发状态.md)；
6. [开发工作规范](docs/开发规范.md)；
7. 当前任务相关代码和测试。

然后检查 `git status --short --branch`、最近提交和远端同步状态。默认无需阅读 V1/V2 历史过程文档；需要追溯时再通过 [文档索引](docs/README.md) 定向查看。

开发必须遵循“小步可验证”：每完成一个独立步骤，就更新工作状态、运行测试、使用中文提交说明提交并立即推送 GitHub。若 Token、时间或外部条件即将中断，必须在任务分支创建可恢复检查点并写清下一步，禁止只把进度留在对话中。

## 文档导航

- [完整文档索引](docs/README.md)：必读、当前参考和历史归档分类。
- [API 与 MCP 工具参考](docs/api-reference.md)：现有工具、Web 路由、请求参数和响应模型。
- [测试与验收规范](docs/testing-acceptance.md)：parity、联网测试、发布门禁和排障流程。
- [东方财富字段实测记录](docs/eastmoney_fields.md)：原始字段、缩放、时间和 K 线格式。
- [服务器部署记录](docs/deployment.md)：Docker、Nginx、Quick Tunnel 与运维命令。
- [开发与贡献指南](CONTRIBUTING.md)：本地环境、修改边界和提交前检查。

## 能力与端点

Streamable HTTP MCP endpoint：`https://YOUR_DOMAIN/mcp/`（建议保留结尾 `/`）。8 个工具：

1. `get_quote`
2. `get_quotes`
3. `get_kline`
4. `get_stock_detail`
5. `get_market_overview`
6. `get_sector_ranking`
7. `scan_mainboard`
8. `get_scan_coverage`

V2 选股作为并行能力保留 V1，不覆盖旧工具：

- MCP：`scan_mainboard_v2`、`scan_mainboard_ab`
- REST/GPT：`GET /scan/v2`、`GET /gpt/{secret}/scan/v2`、`GET /gpt/{secret}/scan/v2/html`、`GET /gpt/{secret}/scan/ab`

V3 与 Legacy 隔离。生产已设置 `V3_ENABLED=true` 并迁移至 `0011_strategy_stabilization`；READ API 已上线，但新数据库的数据集需要由正式任务逐步生成。策略发布状态仍为 `mode=V2`，不得把“API 已部署”表述为“V3 策略已激活”。

V1 仍按 `total_score` 排名；V2 按 `opportunity_score` 排名，并返回 `raw_top30`、`action_top30`、`top100`、`score_version=v2`、完整评分拆解、支撑压力、ATR 止损和风险收益比。Phase 1 只使用当前可验证行情/K 线数据；基本面、估值、公告新闻、政策产业催化和主力资金不会被伪造，相关组件以 `coverage=false`、`score=null` 和 `missing_fields` 暴露。

REST 调试接口：`GET /health`、`/quote/{code}`、`/quotes?codes=...`、`/kline/{code}`、`/detail/{code}`、`/market`、`/sectors`、`/scan`、`/scan/coverage`。REST 与 MCP 使用完全相同的 service/provider 层。

供 GPT 直接读取 JSON 的受保护 Web Adapter 使用 `GET /gpt/{secret}/...`：`stock/{code}`、`stocks`、`stock/{code}/kline`、`stock/{code}/detail`、`market`、`sectors`、`scan`、`scan/coverage`、`coverage`。`secret` 来自 `GPT_WEB_SECRET`；未单独设置时复用 `MCP_TOKEN`。

为规避 ChatGPT Web 对固定 URL 的结果缓存，另提供 `GET /gpt/{secret}/live` HTML 快照。后台任务通过上述共享 Service 刷新一个内存快照，live 请求只读取该快照，绝不等待 Provider。页面中的“获取最新行情快照”和个股链接每次都使用 `secrets.token_urlsafe(24)` 生成新的 nonce；不建立独立 Provider 或行情算法。所有 live 响应同时设置 `no-store` 等防缓存 Header。页面同时展示本轮证券/行情/过滤覆盖、数据新鲜度分布、失败源与缺失字段摘要，以及行业和概念 Top20。`GET /gpt/{secret}/coverage` 从同一份已发布快照读取完整 `CoverageReport`，不会重新请求全市场行情。

后台在交易时段每次刷新完成后等待 2 秒，非交易时段等待 30 秒；现有 Service TTL 仍决定真实上游采集频率。刷新失败会保留上一份成功数据。进程刚启动且首份快照尚未完成时，live 页面立即返回 `INITIALIZING`，不会阻塞请求。

所有成功行情对象带 `source`、兼容字段 `source_timestamp` / `data_timestamp`、`server_timestamp`、`age_seconds`、`timestamp_source`、`snapshot_id`、`confidence`、`stale` 和 `quality`。f86/f124 只能证明为 Provider 更新时间，不能称为最后成交时间；Live HTML 会用更明确的拆分字段展示。质量只由 `DataQualityService` 计算：30 秒内 `LIVE`、30–60 秒 `STALE`、60–300 秒 `OLD`、超过 300 秒 `UNAVAILABLE`。如果东方财富没有时间字段，明确使用 `timestamp_source=fetch_time`。失败不会返回旧行情：REST 返回 4xx/503 JSON，MCP 返回 `ok=false` 结构。

## 单一事实源与一致性

`Market/Scanner Service → MarketDataService → ProviderManager → EastmoneyProvider/TencentProvider` 是唯一业务数据路径。东方财富 `secid`、腾讯 `sh/sz` 等代码转换和字段缩放只存在于各自 Provider。日 K 线由 `MarketDataService` 统一读取 L1 内存与 L2 SQLite；MCP 与 Web 使用同一个 singleton Container、同一个缓存、同一个技术指标服务、同一个扫描器以及同一个 `serialize_business()`。

V2 Phase2A 另有独立只读链路：`ScannerService → FundamentalProviderManager → FundamentalProvider`。它只为 V2 增加基本面分与财务风险，不改变上述行情链路、MCP 或 V1。历史设计见 [Phase2A 基本面设计](docs/archive/v2/phase2a-fundamentals.md)。

同一底层行情生成确定性的 `snapshot_id`，扫描另带 `scan_id`。全市场快照会填充规范的 `quote:{code}` 缓存，因此紧邻的市场、扫描和单股调用会尽量复用同一标准化 Quote。`tests/test_mcp_web_parity.py` 自动比较三个验收股票、单股详情、市场概况、行业 Top10 和扫描结果。

## 本地启动

Linux/macOS：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
# 本地无认证调试：把 .env 中 MCP_TOKEN 留空
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

验证：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/quote/002284
curl "http://127.0.0.1:8000/gpt/$GPT_WEB_SECRET/stock/002284"
curl "http://127.0.0.1:8000/gpt/$GPT_WEB_SECRET/live"
curl "http://127.0.0.1:8000/gpt/$GPT_WEB_SECRET/coverage"
curl 'http://127.0.0.1:8000/kline/002284?period=day&limit=5'
curl 'http://127.0.0.1:8000/sectors?sector_type=industry&limit=10'
curl 'http://127.0.0.1:8000/scan?top_n=30'
curl 'http://127.0.0.1:8000/scan/v2?top_n=30&pool_size=420'
```

## 配置

| 变量 | 默认 | 说明 |
|---|---:|---|
| `MCP_TOKEN` | 空 | 非空时仅保护 `/mcp`，REST 保持便于健康检查/调试；生产必须设置 |
| `GPT_WEB_SECRET` | 空 | 保护 `/gpt/{secret}/...`；空时复用 `MCP_TOKEN` |
| `EASTMONEY_TIMEOUT` | 5 | 单次请求超时秒数 |
| `EASTMONEY_RETRIES` | 3 | 最大尝试次数 |
| `SCAN_CONCURRENCY` | 12 | K 线丰富阶段并发数 |
| `EASTMONEY_PROXY` | 空 | 出站网络需要代理时设置；通常 Linux 留空 |
| `TENCENT_TIMEOUT` | 5 | 腾讯备用源单次请求超时秒数 |
| `TENCENT_PROXY` | 空 | 腾讯备用源代理；通常 Linux 留空 |
| `MAX_KLINE_CONCURRENCY` | 8 | 所有 Provider 合计的 K 线网络并发上限 |
| `KLINE_CACHE_PATH` | `data/kline_cache.sqlite3` | 日 K 线 SQLite 持久化路径 |
| `SCAN_HISTORY_PATH` | `data/scan_history` | V1/V2 扫描 JSONL 历史快照目录，Docker 默认为 `/data/scan_history` |
| `KLINE_REFRESH_TRADING_SECONDS` | 300 | 交易时段正式历史 K 刷新间隔 |
| `KLINE_REFRESH_CLOSED_SECONDS` | 1800 | 非交易时段正式历史 K 刷新间隔 |

不要提交 `.env`。生成随机 token 示例：`openssl rand -hex 32`。

## Docker / Linux 部署

```bash
cp .env.example .env
sed -i 's/change_me_to_a_long_random_secret/替换为随机长字符串/' .env
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
```

Compose 默认把服务绑定到公网 `0.0.0.0:8000`，可通过 `http://服务器IP:8000` 访问。请在云安全组中只向可信来源开放 8000，或继续使用 Nginx/HTTPS 作为公网入口。容器使用非 root 用户、只读根文件系统和 `no-new-privileges`。

域名与 HTTPS：

1. 将域名 A/AAAA 记录指向服务器。
2. 安装 Nginx 与 Certbot，签发证书：`sudo certbot certonly --nginx -d market.example.com`。
3. 复制 `nginx.conf.example` 到 `/etc/nginx/sites-available/market-mcp`，替换域名和证书路径。
4. 建立 sites-enabled 链接，执行 `sudo nginx -t && sudo systemctl reload nginx`。
5. `curl https://market.example.com/health`，MCP 地址为 `https://market.example.com/mcp/`。

示例已关闭 proxy buffering/cache、保留 HTTP/1.1 keepalive、提高 streaming 读取超时，并透传 `Authorization`，适合 Streamable HTTP/SSE 响应。

## Bearer 与 OAuth

设置 `MCP_TOKEN` 后：

```bash
curl -i https://market.example.com/mcp/ \
  -H 'Authorization: Bearer YOUR_TOKEN'
```

这是独立 ASGI 认证边界，REST `/health` 不需要 token。FastMCP 的工具层不依赖认证实现，因此可先空 token 完成连接测试，再开启 token。

部分 ChatGPT 连接入口不允许手工设置固定 Bearer header，而要求标准 OAuth discovery/DCR。此时应把当前中间件替换为 FastMCP `RemoteAuthProvider` + `JWTVerifier`（身份提供商支持 DCR），或用 `OAuthProxy` 对接不支持 DCR 的现有提供商。需要发布 `/.well-known/oauth-protected-resource`、授权服务器 metadata，并配置 HTTPS issuer/audience。参考 [FastMCP Authentication](https://gofastmcp.com/servers/auth/authentication) 与 [Remote OAuth](https://gofastmcp.com/servers/auth/remote-oauth)。

## MCP 测试与连接 ChatGPT

先启动服务，然后：

```bash
python scripts/test_mcp_client.py
MCP_URL=https://market.example.com/mcp/ MCP_TOKEN=YOUR_TOKEN python scripts/test_mcp_client.py
```

脚本会真实执行 MCP initialize、tools/list 和 `get_quote(002284)`。在 ChatGPT 创建自定义连接器/远程 MCP 时填写公网 HTTPS endpoint；若界面支持自定义 Bearer token，填入 `MCP_TOKEN`。若只支持 OAuth，按上一节升级。服务器必须能从公网访问，不能填写 localhost 或内网 IP。

## 数据源、缩放与延迟

- 行情：`push2.eastmoney.com/api/qt/stock/get`
- 全市场/板块：`push2.eastmoney.com/api/qt/clist/get`
- K 线：`push2his.eastmoney.com/api/qt/stock/kline/get`
- 腾讯备用 Quote：`qt.gtimg.cn/q=...`
- 腾讯备用前复权日 K：`web.ifzq.gtimg.cn/appstock/app/fqkline/get`
- 同域节点主动断连时，在最多 3 次总尝试内轮换 `push2delay`/`push2his` 节点。
- 少量批量代码用 `asyncio.gather` 并发；实测 `ulist.np/get` 返回疑似混淆字段，未采用。全市场扫描通过分页 `clist/get` 批量抓取，不逐股获取 quote。
- Quote TTL 3 秒、市场 5 秒、板块 10 秒、扫描 15 秒、分钟 K 5 秒。日 K 使用 L1 + SQLite，交易时段 300 秒、非交易时段 1800 秒才增量刷新；网络失败立即使用已有旧缓存。

字段、请求参数、原始 JSON、缩放和时间含义见 `docs/eastmoney_fields.md` 与 `docs/eastmoney_probe.json`。东方财富不是本项目控制的数据服务，正常延迟通常为秒级，但可能限流或变更字段。

## 扫描逻辑

只保留 `000/001/002/003/600/601/603/605`，排除创业板、科创板、北交所、ST/退市、停牌、一字板、低成交额及按昨收计算的涨跌停证券。先对一次全市场列表做位置/量比/流动性预筛，再对最多 120 个候选并发抓取 80 日 K 线，计算 MA 与最终五维评分。这样能报告全市场 quote coverage，同时避免数千个 K 线请求。

评分用于事实筛选和排序，不构成投资建议；默认重点奖励当日 `0%~3%`、相对指数较强、靠近 MA20 但未贴近 20 日高点的标的。

### V2 Phase 1 扫描

V2 保留同样的主板、ST、停牌、涨跌停、一字板和流动性硬过滤，但不再把 `pct_change > 5%` 作为绝对剔除条件，改由 `risk_penalty` 处理过热。候选池从硬过滤后的主板股票中通过五个 quote-only 通道取并集，默认形成 420 只宽候选：趋势改善代理、低位/安全边际代理、资金活跃代理、相对强度、流动性底线。随后对候选并发读取 260 日 K 和 80 周 K，计算：

```text
opportunity_score = clamp(
  position_score(15)
  + fundamental_score(15, Phase1缺真实源)
  + trend_score(20)
  + flow_score(15)
  + catalyst_score(10, Phase1缺真实源)
  + risk_reward_score(20)
  + liquidity_score(5)
  + risk_penalty(0..-20),
  0,
  100
)
```

`trend_score` 拆分为周 K 8 分和日 K 12 分；周 K 明确下降时会把日 K 上涨视为下降趋势中的反弹并限制趋势分。`risk_reward_score` 使用支撑、压力、ATR 缓冲止损和目标位计算，基础分档为 `RR<1=0`、`1~1.5=4`、`1.5~2=8`、`2~3=14`、`>=3=20`。A级不会强行分配；Phase 1 因基本面和催化缺真实源，默认无法轻易给 A。

## 测试

```bash
pytest -q
pytest tests/test_mcp_web_parity.py -q
RUN_LIVE_TESTS=1 pytest tests/test_live_eastmoney.py -q -vv
python scripts/probe_eastmoney.py
python scripts/acceptance.py
```

`RUN_LIVE_TESTS` 不设置时跳过联网用例，避免 CI 被第三方限流影响。验收输出写到 `docs/acceptance_results.json`；该文件只是带时间戳的当次样例，不能当作当前行情。

## 接口失效排查

1. 先运行探针，查看是 TLS/超时、`rc != 0`、`data=null` 还是字段为空。
2. 用浏览器网络面板核对东方财富页面当前请求的 host、query 与字段；不要凭旧映射修改。
3. 检查服务器 DNS、出口 IP、代理与时间同步；高频调用会触发对方限流。
4. 保留 `docs/eastmoney_probe.json` 后再更新解析器与 fixtures，运行全部单测。
5. 不要提高重试风暴；接口不可用时保持 `ok=false/UNAVAILABLE`。

新增数据源时，实现 `MarketDataProvider`，统一转换成 Pydantic 语义模型，并注册到 `ProviderManager`；每个源必须分别验证价格单位、成交量单位与时间戳。不要把多个源的不同时间点无提示拼成一条 quote。

## 已知限制

- 东方财富接口非官方承诺 API，可能限流、断连、返回空数组或调整字段。
- 板块涨停数与 10/30 分钟历史排名未找到稳定、可验证字段，明确返回 `null`。
- 涨跌停判断按名称与昨收/主板规则计算，除权、上市首日和特殊交易状态可能需要交易日规则表；一字板另行直接排除。
- 扫描对预筛 shortlist 计算 K 线指标，不是对全市场数千只逐一计算 MA；coverage 指 quote 列表覆盖率。
- Quote/扫描的内存 TTL 缓存在重启与多 worker 之间不共享；日 K 的 SQLite 可跨重启保留。第一版仍建议 Uvicorn 单 worker。
- 本项目不提供交易建议、自动交易、账户或资金接口。

## 项目结构

```text
app/{api,indicators,mcp,providers,services,utils}
tests/
scripts/{probe_eastmoney.py,test_mcp_client.py,acceptance.py}
docs/{架构设计实施稿.md,开发规范.md,工作状态.md,README.md}
docs/archive/{v2,v3-design-inputs}
Dockerfile
docker-compose.yml
nginx.conf.example
requirements.txt
.env.example
```

下一阶段建议：标准 OAuth、Prometheus 指标、交易日/证券状态规则表，以及用分钟快照实现板块历史排名；仍应保持交易能力与账户数据完全不在本服务范围内。
