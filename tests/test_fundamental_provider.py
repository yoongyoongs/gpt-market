from __future__ import annotations

from app.fundamentals.eastmoney import snapshot_from_rows


def test_eastmoney_financial_rows_are_normalized_with_metadata() -> None:
    result = snapshot_from_rows(
        "603019",
        [{
            "REPORT_DATE": "2026-06-30 00:00:00",
            "NOTICE_DATE": "2026-08-26 00:00:00",
            "TOTALOPERATEREVE": 1000,
            "DJD_TOI_YOY": 20,
            "DJD_TOI_QOQ": 5,
            "PARENTNETPROFIT": 100,
            "KCFJCXSYJLR": 90,
            "DJD_DPNP_YOY": 30,
            "DJD_DPNP_QOQ": 10,
            "ROEJQ": 12,
            "NETCASH_OPERATE_PK": 120,
            "XSMLL": 35,
            "ZCFZL": 45,
        }],
        source="eastmoney_datacenter",
        valuation={"f9": 18, "f23": 2, "f100": "计算机设备"},
    )
    assert result.fields["revenue"].value == 1000
    assert result.fields["revenue"].report_period == "2026-06-30"
    assert result.fields["revenue"].upstream_source == "eastmoney"
    assert result.fields["pe"].value == 18
    assert result.coverage == 1
    assert len(result.quarterly_trend) == 1
