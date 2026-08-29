from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.v3.domain.market_data import AdjustType, BarPeriod
from app.v3.infrastructure.providers.bars import SinaHistoricalBarProvider


def test_sina_jsonp_and_factor_payload_are_structurally_parsed() -> None:
    rows = SinaHistoricalBarProvider._decode_json_value(
        "/* guard */ var x([{\"day\":\"2026-08-28\",\"close\":\"10\"}]);", "["
    )
    payload = SinaHistoricalBarProvider._decode_json_value(
        'var x={"data":[{"d":"2026-07-16","f":"1.0"}]}; /* guard */', "{"
    )
    assert rows[0]["close"] == "10"
    assert payload["data"][0]["f"] == "1.0"


def test_qfq_factor_selection_uses_latest_effective_factor() -> None:
    factors = [(date(2026, 7, 16), 1.0), (date(2025, 7, 16), 1.05)]
    assert SinaHistoricalBarProvider._factor_for(date(2026, 8, 1), factors) == 1.0
    assert SinaHistoricalBarProvider._factor_for(date(2026, 6, 1), factors) == 1.05


@pytest.mark.asyncio
async def test_sina_provider_derives_qfq_without_fabricating_amount() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("qfq.js"):
            return httpx.Response(
                200,
                text='var x={"data":[{"d":"2026-07-16","f":"1.0"},'
                '{"d":"2025-07-16","f":"2.0"}]};',
            )
        return httpx.Response(
            200,
            text='var x([{"day":"2026-06-01","open":"20","high":"22",'
            '"low":"18","close":"20","volume":"100"},'
            '{"day":"2026-08-01","open":"12","high":"13",'
            '"low":"11","close":"12","volume":"200"}]);',
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = SinaHistoricalBarProvider(client=client)
    qfq = await provider.fetch("688981", BarPeriod.DAY, AdjustType.QFQ, 2)
    raw = await provider.fetch("688981", BarPeriod.DAY, AdjustType.RAW, 2)
    await client.aclose()

    assert [bar.close for bar in qfq.bars] == [10, 12]
    assert [bar.close for bar in raw.bars] == [20, 12]
    assert all(bar.amount is None for bar in qfq.bars)
