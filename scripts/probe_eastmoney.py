"""Record live Eastmoney responses before changing provider field mappings."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import httpx

CODES = {"002284": "0.002284", "600722": "1.600722", "600519": "1.600519", "000001": "0.000001"}
FIELDS = "f57,f58,f43,f60,f46,f44,f45,f170,f169,f47,f48,f168,f50,f171,f86"
UT = "fa5fd1943c7b386f172d6893dbfba10b"


def has_usable_response(record: dict, *, kline: bool = False) -> bool:
    payload = record.get("response") or {}
    data = payload.get("data") or {}
    return bool(data.get("klines")) if kline else bool(data)


async def request_with_retry(client: httpx.AsyncClient | httpx.Client, url: str, params: dict, headers: dict, proxy: str | None) -> dict:
    last_error: Exception | None = None
    urls = [url, url.replace("push2.eastmoney.com", "push2delay.eastmoney.com"), url.replace("push2.eastmoney.com", "push2his.eastmoney.com")]
    if "push2his.eastmoney.com" in url:
        urls = [url, url.replace("push2his.eastmoney.com", "push2.eastmoney.com"), url.replace("push2his.eastmoney.com", "push2delay.eastmoney.com")]
    for attempt in range(3):
        attempt_client = httpx.Client(timeout=5, headers=headers, proxy=proxy) if proxy else client
        try:
            response = attempt_client.get(urls[attempt], params=params) if isinstance(attempt_client, httpx.Client) else await attempt_client.get(urls[attempt], params=params)
            response.raise_for_status()
            payload = json.loads(response.content.decode("utf-8"))
            if "kline/get" in url and not (payload.get("data") or {}).get("klines"):
                raise RuntimeError("eastmoney returned empty klines")
            return {"requested_url": str(response.request.url), "params": params, "response": payload}
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(0.2 * (2**attempt))
        finally:
            if attempt_client is not client and isinstance(attempt_client, httpx.Client):
                attempt_client.close()
    return {"requested_url": url, "params": params, "error": str(last_error)}


async def main() -> None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139.0 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://quote.eastmoney.com/",
    }
    output = Path(__file__).resolve().parents[1] / "docs" / "eastmoney_probe.json"
    previous = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    proxy = os.getenv("EASTMONEY_PROXY") or os.getenv("HTTPS_PROXY")
    client: httpx.AsyncClient | httpx.Client
    client = httpx.Client(timeout=5, headers=headers, proxy=proxy) if proxy else httpx.AsyncClient(timeout=5, headers=headers)
    try:
        records = {}
        for code, secid in CODES.items():
            key = f"quote_{code}_raw"
            result = await request_with_retry(
                client,
                "https://push2.eastmoney.com/api/qt/stock/get",
                {"ut": UT, "secid": secid, "fltt": 1, "invt": 2, "fields": FIELDS},
                headers,
                proxy,
            )
            records[key] = previous.get(key, result) if "error" in result and has_usable_response(previous.get(key, {})) else result
        kline_result = await request_with_retry(
            client,
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {"ut": UT, "secid": "0.002284", "klt": 101, "fqt": 1, "lmt": 5, "end": "20500101", "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"},
            headers,
            proxy,
        )
        key = "kline_002284_day"
        records[key] = previous.get(key, kline_result) if "error" in kline_result and has_usable_response(previous.get(key, {}), kline=True) else kline_result
    finally:
        if isinstance(client, httpx.AsyncClient):
            await client.aclose()
        else:
            client.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"probed_at": datetime.now().astimezone().isoformat(), **records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    asyncio.run(main())
