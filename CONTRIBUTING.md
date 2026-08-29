# 开发与贡献指南

开始任何任务前，先阅读 [开发工作规范](docs/开发规范.md) 和 [当前工作状态](docs/工作状态.md)。每完成一个可验证步骤，都必须同步状态、提交并推送 GitHub。

## 环境

要求 Python 3.12。

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

本地调试可将 `.env` 中的 token/secret 留空；不要提交 `.env`。

## 启动

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 修改边界

- 原始行情字段只允许进入 Provider。
- 新入口只能调用 Service，不得直接调用 Provider。
- 新指标必须加入统一技术指标服务。
- 新扫描条件必须同时作用于 MCP 和 Web。
- 新响应字段必须先进入 Pydantic schema。
- 不得用缓存旧值或推测值兜底实时事实。

## 提交前检查

```powershell
python -m pytest -q
python -m compileall -q app tests
git diff --check
```

涉及外部字段时额外执行：

```powershell
python scripts/probe_eastmoney.py
$env:RUN_LIVE_TESTS='1'
python -m pytest tests/test_live_eastmoney.py -q -vv
```

涉及入口、缓存、模型、指标或扫描时必须执行：

```powershell
python -m pytest tests/test_mcp_web_parity.py -q
```

## Commit 建议

提交应保持单一目的，并使用中文说明，例如：

```text
功能：增加共享快照元数据
修复：统一扫描缓存键
文档：补充MCP与Web一致性约定
```

不要提交真实行情凭据、服务器密码、私有域名证书、运行日志或本地虚拟环境。
