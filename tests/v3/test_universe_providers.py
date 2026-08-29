from __future__ import annotations

from app.v3.infrastructure.providers.universe import ExchangeUniverseProvider, _plain_text


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
