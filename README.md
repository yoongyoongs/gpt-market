# 极简 A 股实时行情 Remote MCP

只读的 Python 3.12 服务，向 ChatGPT 或其他 MCP 客户端提供 A 股、ETF、指数、K 线、板块排名与基础主板扫描。数据来自东方财富公开页面接口，不包含账户、交易、数据库或任何下单能力。

## 文档导航

- [开发设计文档](docs/development-design.md)：架构、单一事实源、缓存、质量、指标、扫描与扩展设计。
- [API 与 MCP 工具参考](docs/api-reference.md)：8 个工具、Web 路由、请求参数和响应模型。
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

REST 调试接口：`GET /health`、`/quote/{code}`、`/quotes?codes=...`、`/kline/{code}`、`/detail/{code}`、`/market`、`/sectors`、`/scan`、`/scan/coverage`。REST 与 MCP 使用完全相同的 service/provider 层。

供 GPT 直接读取 JSON 的受保护 Web Adapter 使用 `GET /gpt/{secret}/...`：`stock/{code}`、`stocks`、`stock/{code}/kline`、`stock/{code}/detail`、`market`、`sectors`、`scan`、`scan/coverage`。`secret` 来自 `GPT_WEB_SECRET`；未单独设置时复用 `MCP_TOKEN`。

所有成功行情对象带 `source`、`source_timestamp`、兼容字段 `data_timestamp`、`server_timestamp`、`age_seconds`、`timestamp_source`、`snapshot_id`、`confidence`、`stale` 和 `quality`。质量只由 `DataQualityService` 计算：30 秒内 `LIVE`、30–60 秒 `STALE`、60–300 秒 `OLD`、超过 300 秒 `UNAVAILABLE`。如果东方财富没有可靠时间字段，明确使用 `timestamp_source=fetch_time`。失败不会返回旧行情：REST 返回 4xx/503 JSON，MCP 返回 `ok=false` 结构。

## 单一事实源与一致性

`EastmoneyProvider → 标准 Pydantic 模型 → 共享 AsyncTTLCache → Quote/Kline/Market/Sector/Scanner Service → MCP/Web Adapter` 是唯一数据路径。东方财富 `f43` 等字段和价格缩放只存在于 Provider；MCP 与 Web 使用同一个 singleton Container、同一个缓存、同一个技术指标服务、同一个扫描器以及同一个 `serialize_business()`。

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
curl 'http://127.0.0.1:8000/kline/002284?period=day&limit=5'
curl 'http://127.0.0.1:8000/sectors?sector_type=industry&limit=10'
curl 'http://127.0.0.1:8000/scan?top_n=30'
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

不要提交 `.env`。生成随机 token 示例：`openssl rand -hex 32`。

## Docker / Linux 部署

```bash
cp .env.example .env
sed -i 's/change_me_to_a_long_random_secret/替换为随机长字符串/' .env
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
```

Compose 只把后端绑定到 `127.0.0.1:8000`，由 Nginx 对公网提供 HTTPS。容器使用非 root 用户、只读根文件系统和 `no-new-privileges`。

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
- 同域节点主动断连时，在最多 3 次总尝试内轮换 `push2delay`/`push2his` 节点。
- 少量批量代码用 `asyncio.gather` 并发；实测 `ulist.np/get` 返回疑似混淆字段，未采用。全市场扫描通过分页 `clist/get` 批量抓取，不逐股获取 quote。
- Quote TTL 3 秒、市场 5 秒、板块 10 秒、扫描 15 秒、日 K 60 秒、分钟 K 5 秒。

字段、请求参数、原始 JSON、缩放和时间含义见 `docs/eastmoney_fields.md` 与 `docs/eastmoney_probe.json`。东方财富不是本项目控制的数据服务，正常延迟通常为秒级，但可能限流或变更字段。

## 扫描逻辑

只保留 `000/001/002/003/600/601/603/605`，排除创业板、科创板、北交所、ST/退市、停牌、一字板、低成交额及按昨收计算的涨跌停证券。先对一次全市场列表做位置/量比/流动性预筛，再对最多 120 个候选并发抓取 80 日 K 线，计算 MA 与最终五维评分。这样能报告全市场 quote coverage，同时避免数千个 K 线请求。

评分用于事实筛选和排序，不构成投资建议；默认重点奖励当日 `0%~3%`、相对指数较强、靠近 MA20 但未贴近 20 日高点的标的。

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

增加新浪/腾讯备用源时，实现 `MarketDataProvider`，统一转换成 Pydantic 语义模型，并在 service 层做熔断/优先级切换；每个源必须分别验证价格单位、成交量单位与时间戳。不要把多个源的不同时间点无提示拼成一条 quote。

## 已知限制

- 东方财富接口非官方承诺 API，可能限流、断连、返回空数组或调整字段。
- 板块涨停数与 10/30 分钟历史排名未找到稳定、可验证字段，明确返回 `null`。
- 涨跌停判断按名称与昨收/主板规则计算，除权、上市首日和特殊交易状态可能需要交易日规则表；一字板另行直接排除。
- 扫描对预筛 shortlist 计算 K 线指标，不是对全市场数千只逐一计算 MA；coverage 指 quote 列表覆盖率。
- 内存 TTL 缓存在重启与多 worker 之间不共享；第一版建议 Uvicorn 单 worker。
- 本项目不提供交易建议、自动交易、账户或资金接口。

## 项目结构

```text
app/{api,indicators,mcp,providers,services,utils}
tests/
scripts/{probe_eastmoney.py,test_mcp_client.py,acceptance.py}
docs/{development-design.md,api-reference.md,testing-acceptance.md,eastmoney_fields.md,deployment.md}
Dockerfile
docker-compose.yml
nginx.conf.example
requirements.txt
.env.example
```

下一阶段建议：加入第二只只读行情源与熔断指标、标准 OAuth、Prometheus 指标、交易日/证券状态规则表，以及用分钟快照实现板块历史排名；仍应保持交易能力与账户数据完全不在本服务范围内。
