# 测试与验收规范

## 1. 测试目标

测试必须证明两件事：

1. 东方财富原始字段被正确转换成标准业务事实。
2. MCP 与 Web 是同一业务系统的不同入口，业务结果完全一致。

联网行情具有时间和外部可用性波动，因此确定性单元测试与联网验收分开执行。

## 2. 测试分层

| 层级 | 文件 | 重点 |
|---|---|---|
| Provider 解析 | `test_provider_parsing.py` | secid、价格/百分比缩放、成交量、K 线格式 |
| 技术指标 | `test_indicators.py` | MA、ATR、RSI、高低点和收益 |
| 扫描业务 | `test_scanner.py` | 板块过滤、ST、涨跌停、评分、coverage、缓存键 |
| 入口一致性 | `test_mcp_web_parity.py` | 三只股票、详情、市场、行业 Top10、扫描 Top10 |
| 联网验证 | `test_live_eastmoney.py` | 当前 Quote 和日 K 可用性 |

## 3. 本地确定性测试

```bash
python -m pytest -q
```

联网用例默认跳过。当前基线为：

```text
35 passed, 5 skipped
```

任何变更不得通过删除断言、放宽关键字段比较或把失败改为跳过来维持基线。

## 4. MCP/Web parity

```bash
python -m pytest tests/test_mcp_web_parity.py -q
```

自动覆盖：

- `002284/600722/600519` Quote 全业务数据一致；
- 价格、涨跌幅、成交额、换手率、量比、源时间和 snapshot_id 一致；
- 单股详情的 MA、ATR、RSI 和 20 日高低点一致；
- 市场概况一致；
- 行业 Top10 顺序一致；
- 扫描 coverage、候选顺序、total_score 和 scan_id 一致；
- 错误 Web secret 被拒绝。

测试使用固定快照，避免第三方接口变化导致伪失败。两 Adapter 会被替换为同一个测试 Container，验证公共序列化和入口契约。

## 5. 价格缩放验收

离线 fixture 至少覆盖：

- 002284
- 600722
- 600519
- 000001

变更 `fltt`、字段映射或缩放函数前，必须先运行：

```bash
python scripts/probe_eastmoney.py
```

并人工核对 `docs/eastmoney_probe.json` 与东方财富页面显示。原始样本只是当次审计证据，不得作为当前行情数据源。

## 6. 联网测试

```bash
RUN_LIVE_TESTS=1 python -m pytest tests/test_live_eastmoney.py -q -vv
```

判定原则：

- Quote 失败通常意味着源接口、网络出口或字段发生变化，必须调查。
- K 线空数组或服务端断连记录为外部可用性失败，不得用历史 fixture 让联网测试“通过”。
- 非交易时段 Quote 可以返回 `OLD/UNAVAILABLE`；只要时间和质量真实，不应改成 `LIVE`。

## 7. 综合验收脚本

```bash
python scripts/acceptance.py
```

脚本调用三类真实服务并写入 `docs/acceptance_results.json`。该文件带运行时间，只是验收记录，不是稳定 API 返回，也不能作为投资分析依据。

## 8. 远程 MCP 验收

```bash
MCP_URL=https://HOST/mcp/ MCP_TOKEN=YOUR_TOKEN \
  python scripts/test_mcp_client.py
```

至少验证：

1. initialize；
2. tools/list；
3. `get_quote("002284")`；
4. 返回含源时间、质量和 snapshot_id；
5. token 错误时返回 401。

## 9. 线上 parity 验收表

同一应用进程中按 MCP→Web 顺序紧邻调用，并比较：

| 项目 | 必须比较 |
|---|---|
| 002284/600722/600519 Quote | 价格、涨跌幅、成交额、换手率、量比、源时间、snapshot_id |
| 002284 Detail | MA5/20/60、ATR14、RSI14、20 日高低点 |
| Market | 上涨家数、下跌家数、总成交额、snapshot_id |
| Sector Top10 | 名称、顺序、涨跌幅 |
| Scan Top10 | 代码顺序、每项 total_score、coverage、scan_id |

如果外部源返回不可用，必须把该行标记为 `UNAVAILABLE`，不能把“两个入口都失败”伪装成数据字段 PASS。

## 10. 发布门禁

部署前：

- 全部确定性测试通过；
- parity 测试通过；
- `.env` 和凭据未进入 Git；
- `nginx -t` 通过；
- Docker 镜像构建成功。

部署后：

- `/health` 返回 200；
- 容器 health 正常；
- MCP tools/list 显示 8 个工具；
- 公网 MCP 至少一次 tools/call 成功；
- 公网 Web Quote 成功；
- `/gpt/` 路径未出现在 Nginx/Uvicorn access log；
- systemd tunnel、Nginx、Docker 均 active。

## 11. 回归处理

发现 MCP/Web 不一致时按以下顺序排查：

1. 比较两入口是否落在同一进程和 Container；
2. 检查 Service 参数类型是否生成不同缓存键；
3. 比较 snapshot_id/scan_id；
4. 检查是否某 Adapter 自行序列化或计算；
5. 检查缓存 TTL 是否在两次调用之间过期；
6. 检查 Provider 是否对同一原始字段执行了不同缩放。

修复必须发生在共享 Provider、模型、Service 或缓存层，禁止在某个 Adapter 中打补丁。

## 12. Live Refresh 性能验收

- 后台先生成一份成功快照；
- 连续请求 `/gpt/{secret}/live` 或 nonce 快照页至少 10 次；
- 请求期间将 Market/Scanner/Quote 调用替换为失败桩，确认 live 仍从缓存成功返回；
- 检查 `snapshot_time`、`server_time`、`age_ms`、`market_status` 和 `stale`；
- 检查全部响应的 `Cache-Control`、`Pragma`、`Expires`；
- 服务器本机使用 `curl -w` 验证大部分响应低于 500ms；
- 重新运行 MCP 8 工具与 JSON/MCP 一致性测试。
