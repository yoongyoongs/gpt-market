"""测试全局环境。

NEW-SEC-001 收紧后，非 allowlist 的 /api/v3 GET 需要认证。测试环境固定一个
独立 token（os.environ 优先级高于 .env，且在 app.main 导入前设置，保证
get_settings() 拿到的是测试 token，而非开发者本地 .env 的真实值）。
"""

from __future__ import annotations

import os

TEST_V3_API_TOKEN = "test-v3-token-for-private-reads"

os.environ.setdefault("V3_API_TOKEN", TEST_V3_API_TOKEN)
os.environ.setdefault("V3_PUBLIC_MARKET_READ", "true")
