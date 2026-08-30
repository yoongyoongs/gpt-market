from __future__ import annotations

import pytest

from app.v3.domain.market_data import Market, SecurityMember
from app.v3.infrastructure.providers.universe import (
    ExchangeUniverseProvider,
    OfficialUniverseWithVendorStatusProvider,
    _plain_text,
)
from tests.v3.test_refresh_universe import FakeProvider, fetched


def test_szse_html_name_is_parsed_without_markup() -> None:
    assert _plain_text("<a href='x'><u>平安银行</u></a>") == "平安银行"


def test_exchange_provider_parses_sse_rows() -> None:
    sse = ExchangeUniverseProvider._parse_sse(
        {
            "pageHelp": {
                "data": [
                    {"A_STOCK_CODE": "600000", "SEC_NAME_CN": "浦发银行", "STATE_CODE_STOCK": "4"}
                ]
            }
        }
    )
    assert [(item.market.value, item.code, item.name) for item in sse] == [
        ("SH", "600000", "浦发银行")
    ]


def test_exchange_provider_parses_szse_json() -> None:
    members = ExchangeUniverseProvider._parse_szse(
        [{"data": [{"agdm": "000001", "agjc": "<a><u>平安银行</u></a>", "bk": "主板"}]}]
    )

    assert [(item.market.value, item.code, item.name) for item in members] == [
        ("SZ", "000001", "平安银行")
    ]


def test_exchange_provider_parses_current_bse_codes() -> None:
    members = ExchangeUniverseProvider._parse_bse(
        {
            "content": [
                {
                    "xxzqdm": "920000",
                    "xxzqjc": "安徽凤凰",
                    "xxgprq": "20201223",
                    "xxhyzl": "汽车制造业",
                },
                {"xxzqdm": "830000", "xxzqjc": "旧代码不得进入当前快照"},
            ]
        }
    )

    assert [(item.market.value, item.code, item.name) for item in members] == [
        ("BJ", "920000", "安徽凤凰")
    ]


@pytest.mark.asyncio
async def test_official_membership_is_enriched_without_vendor_extras() -> None:
    official = fetched("official", 2)
    first = official.members[0]
    vendor_members = (
        first.model_copy(update={"suspended": True, "trading_status": "SUSPENDED"}),
        SecurityMember(code="999999", market=Market.SH, name="非官方额外证券"),
    )
    vendor = fetched("vendor", 2).model_copy(update={"members": vendor_members})
    provider = OfficialUniverseWithVendorStatusProvider(
        FakeProvider("official", result=official),
        FakeProvider("vendor", result=vendor),
    )

    result = await provider.fetch_snapshot()

    assert result.source_code == provider.code
    assert len(result.members) == len(official.members)
    assert result.members[0].code == first.code
    assert result.members[0].suspended is True
    assert all(member.code != "999999" for member in result.members)


@pytest.mark.asyncio
async def test_official_membership_survives_vendor_status_failure() -> None:
    official = fetched("official", 2)
    provider = OfficialUniverseWithVendorStatusProvider(
        FakeProvider("official", result=official),
        FakeProvider("vendor", error=RuntimeError("down")),
    )

    result = await provider.fetch_snapshot()

    assert result.source_code == provider.code
    assert result.members == official.members
