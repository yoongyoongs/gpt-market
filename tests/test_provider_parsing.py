from __future__ import annotations

import pytest

from app.providers.eastmoney import market_of, parse_kline_row, parse_kline_rows, parse_quote, scale_raw, to_eastmoney_secid


@pytest.mark.parametrize(
    ("code", "secid", "market"),
    [
        ("600519", "1.600519", "SH"),
        ("600131", "1.600131", "SH"),
        ("002284", "0.002284", "SZ"),
        ("000001", "0.000001", "SZ"),
        ("399001", "0.399001", "SZ"),
        ("830799", "0.830799", "BJ"),
        ("920001", "0.920001", "BJ"),
    ],
)
def test_code_to_secid(code: str, secid: str, market: str) -> None:
    assert to_eastmoney_secid(code) == secid
    assert market_of(code) == market


@pytest.mark.parametrize("bad", ["", "123", "600519.SH", "ABCDEF", "700001"])
def test_invalid_code(bad: str) -> None:
    with pytest.raises(ValueError):
        to_eastmoney_secid(bad)


def test_raw_quote_scaling_volume_and_amount() -> None:
    raw = {
        "f43": 979, "f44": 982, "f45": 969, "f46": 979, "f47": 203556,
        "f48": 198606168.71, "f50": 59, "f57": "002284", "f58": "亚太股份",
        "f60": 984, "f86": 1787813505, "f168": 278, "f169": -5, "f170": -51, "f171": 132,
    }
    quote = parse_quote(raw, fltt=1)
    assert quote.price == 9.79
    assert quote.prev_close == 9.84
    assert quote.pct_change == -0.51
    assert quote.change == -0.05
    assert quote.volume == 20_355_600
    assert quote.amount == 198_606_168.71
    assert quote.turnover_rate == 2.78
    assert quote.volume_ratio == 0.59
    assert quote.amplitude == 1.32


@pytest.mark.parametrize(
    ("code", "raw_price", "expected"),
    [
        ("002284", 958, 9.58),
        ("600722", 512, 5.12),
        ("600519", 148800, 1488.00),
        ("000001", 1123, 11.23),
    ],
)
def test_verified_f43_price_scaling_for_acceptance_codes(code: str, raw_price: int, expected: float) -> None:
    raw = {
        "f43": raw_price,
        "f44": raw_price,
        "f45": raw_price,
        "f46": raw_price,
        "f47": 100,
        "f48": 1000,
        "f57": code,
        "f58": "fixture",
        "f60": raw_price,
        "f86": 1787813505,
        "f168": 100,
        "f169": 0,
        "f170": 0,
        "f171": 0,
    }
    assert parse_quote(raw, fltt=1).price == expected


def test_scale_missing_value() -> None:
    assert scale_raw("-") is None
    assert scale_raw(None) is None


def test_daily_kline_parsing() -> None:
    item = parse_kline_row("2026-08-27,9.79,9.80,9.82,9.69,203556,198606168.71,1.32,-0.41,-0.04,2.78")
    assert item.open == 9.79
    assert item.close == 9.80
    assert item.high == 9.82
    assert item.low == 9.69
    assert item.volume == 20_355_600
    assert item.amount == 198_606_168.71


def test_kline_parser_accepts_array_and_observed_space_delimited_encoding() -> None:
    first = "2026-08-26,9.46,9.84,9.97,9.46,401272,393833770.78"
    second = "2026-08-27,9.79,9.80,9.82,9.69,203556,198606168.71"
    assert [item.close for item in parse_kline_rows([first, second])] == [9.84, 9.8]
    assert [item.close for item in parse_kline_rows(f"{first} {second}")] == [9.84, 9.8]
