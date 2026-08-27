from __future__ import annotations

import asyncio
import os

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main() -> None:
    url = os.getenv("MCP_URL", "http://127.0.0.1:8000/mcp/")
    token = os.getenv("MCP_TOKEN")
    transport = StreamableHttpTransport(url=url, headers={"Authorization": f"Bearer {token}"}) if token else url
    async with Client(transport) as client:
        tools = await client.list_tools()
        print("tools:", [tool.name for tool in tools])
        result = await client.call_tool("get_quote", {"code": "002284"})
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
