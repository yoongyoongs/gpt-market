# 服务器部署记录

## 当前实例

- 服务器：`106.13.171.166`
- 项目目录：`/opt/gpt-market`
- 健康检查：`http://106.13.171.166/health`
- MCP 端点：`http://106.13.171.166/mcp/`
- 应用容器：`market-mcp`
- 对外入口：Nginx 80 端口
- 应用端口：仅绑定服务器回环地址 `127.0.0.1:8000`

MCP Token 只保存在服务器的 `/opt/gpt-market/.env` 中，不进入 Git 仓库。

## 日常检查

```bash
cd /opt/gpt-market
docker-compose ps
docker-compose logs --tail=100 market-mcp
curl http://127.0.0.1:8000/health
nginx -t
```

## 更新部署

服务器位于中国大陆网络环境。构建镜像时可显式使用国内 PyPI 镜像：

```bash
cd /opt/gpt-market
docker-compose build \
  --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
  market-mcp
docker-compose up -d --no-build
```

## HTTPS

服务器已安装 Cloudflare Quick Tunnel，提供无需自有域名的开发测试 HTTPS 入口。
当前地址通过下列命令查询：

```bash
/opt/gpt-market/show-tunnel-url.sh
```

服务状态与日志：

```bash
systemctl status gpt-market-tunnel.service
journalctl -u gpt-market-tunnel.service -f
```

ChatGPT 开发者模式连接时，把查询结果加上 `/mcp/` 作为 MCP 地址。服务仍要求：

```text
Authorization: Bearer <MCP_TOKEN>
```

Quick Tunnel 仅适合开发和验收，没有可用性承诺；`cloudflared` 每次重启都可能获得
新的随机 `trycloudflare.com` 地址。Cloudflare 官方还声明 Quick Tunnel 不支持持续
SSE。当前 MCP 的 initialize、tools/list 和 tools/call 已通过有限 SSE 响应实测，但
长期 SSE GET 不应视为稳定能力。

正式使用时，准备一个解析到 `106.13.171.166` 的域名或 Cloudflare Named Tunnel，
并使用稳定 HTTPS 地址，例如：

```text
https://你的域名/mcp/
```

生产级 ChatGPT 插件还应实现 MCP 标准授权发现/OAuth。不要把 MCP Token 写入
Nginx 配置、README、日志或 Git 提交，也不要为了连接测试永久关闭鉴权。

## GPT Web JSON Adapter

在 `/opt/gpt-market/.env` 中设置独立的长随机 `GPT_WEB_SECRET`；如果省略，应用会复用 `MCP_TOKEN`。重新构建容器后可使用：

```text
https://当前隧道地址/gpt/{secret}/stock/002284
https://当前隧道地址/gpt/{secret}/stock/002284/detail
https://当前隧道地址/gpt/{secret}/market
https://当前隧道地址/gpt/{secret}/sectors?sector_type=industry&limit=10
https://当前隧道地址/gpt/{secret}/scan?top_n=10
```

Web 与 MCP 只在最外层包装不同，业务 `data` 来自相同 Pydantic 模型、Service、缓存和序列化函数。路径 secret 属于敏感信息，不得写入公开文档、截图或 Git；长期使用时还应关闭或脱敏 Nginx 对 `/gpt/` 的 access log。
