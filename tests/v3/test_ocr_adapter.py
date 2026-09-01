"""RC-09A OCR Adapter 与识别管线离线测试（OCR-001）。

方案 §12.1：上传图片 → 保存 hash/reference → OCR Adapter（可配置）
→ field + confidence + region → Draft Preview。Domain 不绑定供应商。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.v3.application.ocr import (
    DraftAssembler,
    NullOcrAdapter,
    OcrField,
    OcrResult,
    OcrUnavailableError,
    TesseractOcrAdapter,
    build_ocr_adapter,
    parse_tesseract_tsv,
)

NOW = datetime(2026, 9, 2, 8, tzinfo=timezone.utc)

# 模拟 tesseract TSV 输出：一行 A 股成交记录
TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
TSV_ROWS = [
    ('1', '1', '1', '0', '0', '0', '0', '0', '480', '40', '-1', ''),
    ('1', '1', '1', '1', '0', '0', '0', '0', '480', '40', '0', ''),
    ('1', '1', '1', '1', '0', '1', '10', '10', '80', '20', '92.0', '证券代码'),
    ('1', '1', '1', '1', '0', '2', '100', '10', '60', '20', '95.0', '600519'),
    ('1', '1', '1', '1', '0', '3', '200', '10', '40', '20', '88.0', '买入'),
    ('1', '1', '2', '1', '0', '1', '10', '40', '80', '20', '90.0', '成交价格'),
    ('1', '1', '2', '1', '0', '2', '100', '40', '60', '20', '94.0', '10.50'),
    ('1', '1', '3', '1', '0', '1', '10', '70', '80', '20', '91.0', '成交数量'),
    ('1', '1', '3', '1', '0', '2', '100', '70', '60', '20', '96.0', '100'),
    ('1', '1', '4', '1', '0', '1', '10', '100', '80', '20', '40.0', '成交时间'),
    ('1', '1', '4', '1', '0', '2', '100', '100', '120', '20', '55.0', '2026-09-01'),
]
TSV_SAMPLE = "\n".join(
    [TSV_HEADER] + ["\t".join(str(part) for part in row) for row in TSV_ROWS]
)


def test_parse_tesseract_tsv_groups_words_into_lines() -> None:
    lines = parse_tesseract_tsv(TSV_SAMPLE)
    assert len(lines) == 4
    first = lines[0]
    assert first["text"] == "证券代码 600519 买入"
    assert first["confidence"] == pytest.approx((92.0 + 95.0 + 88.0) / 3 / 100)
    assert first["region"]["x"] == 10
    assert first["region"]["y"] == 10


def test_parse_tesseract_tsv_maps_labeled_fields() -> None:
    from app.v3.application.ocr import map_fields

    lines = parse_tesseract_tsv(TSV_SAMPLE)
    fields = map_fields(lines)
    by_key = {f.key: f for f in fields}
    assert by_key["security_code"].value == "600519"
    assert by_key["side"].value == "BUY"
    assert by_key["price"].value == "10.50"
    assert by_key["quantity"].value == "100"
    assert by_key["trade_time"].value == "2026-09-01"
    assert by_key["price"].confidence == pytest.approx(0.92)  # 行内词均值
    assert by_key["price"].region is not None


async def test_tesseract_adapter_runs_binary_and_returns_result(monkeypatch) -> None:
    adapter = TesseractOcrAdapter()

    class _FakeProcess:
        async def communicate(self):
            return TSV_SAMPLE.encode(), b""

    async def _fake_exec(*args, **kwargs):
        assert "-l" in args
        return _FakeProcess()

    monkeypatch.setattr(
        "app.v3.application.ocr.asyncio.create_subprocess_exec", _fake_exec
    )
    result = await adapter.recognize(b"fake-png-bytes")
    assert result.provider == "tesseract"
    assert {f.key for f in result.fields} >= {"security_code", "side", "price", "quantity"}


async def test_tesseract_adapter_missing_binary_is_honest(monkeypatch) -> None:
    import asyncio

    async def _raise(*args, **kwargs):
        raise FileNotFoundError("tesseract")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise)
    adapter = TesseractOcrAdapter()
    with pytest.raises(OcrUnavailableError) as exc_info:
        await adapter.recognize(b"bytes")
    assert exc_info.value.reason == "TESSERACT_NOT_AVAILABLE"


async def test_null_adapter_always_unavailable() -> None:
    adapter = NullOcrAdapter()
    assert adapter.provider == "none"
    with pytest.raises(OcrUnavailableError) as exc_info:
        await adapter.recognize(b"bytes")
    assert exc_info.value.reason == "OCR_PROVIDER_NOT_CONFIGURED"


def test_factory_reads_provider_env(monkeypatch) -> None:
    monkeypatch.setenv("V3_OCR_PROVIDER", "none")
    assert isinstance(build_ocr_adapter(), NullOcrAdapter)
    monkeypatch.setenv("V3_OCR_PROVIDER", "tesseract")
    assert isinstance(build_ocr_adapter(), TesseractOcrAdapter)
    monkeypatch.setenv("V3_OCR_PROVIDER", "future-vision")
    try:
        build_ocr_adapter()
    except ValueError:
        pass
    else:
        raise AssertionError("unknown provider must be rejected")


def _result(**field_overrides) -> OcrResult:
    fields = (
        OcrField(key="side", value="BUY", confidence=0.9),
        OcrField(key="price", value="10.50", confidence=0.95),
        OcrField(key="quantity", value="100", confidence=0.9),
        OcrField(key="fee", value="5", confidence=0.8),
        OcrField(key="trade_time", value="2026-09-01T09:31:00+00:00", confidence=0.85),
    )
    if field_overrides:
        fields = tuple(
            OcrField(key=f.key, value=field_overrides.get(f.key, f.value),
                     confidence=f.confidence)
            for f in fields
        )
    return OcrResult(provider="test", fields=fields)


def test_assembler_builds_valid_trade_draft() -> None:
    assembler = DraftAssembler()
    report = assembler.build_trade(
        _result(), account_id=uuid4(), security_id=uuid4(), overrides={},
    )
    draft = report["draft"]
    assert draft is not None
    assert draft.side.value == "BUY"
    assert draft.price == Decimal("10.50")
    assert draft.quantity == Decimal("100")
    assert draft.fee == Decimal("5")
    assert draft.trade_time == datetime(2026, 9, 1, 9, 31, tzinfo=timezone.utc)
    assert draft.source == "OCR_TEST"
    assert draft.field_confidence["price"] == pytest.approx(0.95)
    assert report["missing_fields"] == []
    assert report["low_confidence_fields"] == []


def test_assembler_flags_low_confidence_and_missing_fields() -> None:
    result = _result(price="10.50")
    # 人为压低 price 置信度、去掉 side
    result = OcrResult(provider="test", fields=(
        OcrField(key="price", value="10.50", confidence=0.3),
        *[f for f in result.fields if f.key != "price" and f.key != "side"],
    ))
    assembler = DraftAssembler()
    report = assembler.build_trade(
        result, account_id=uuid4(), security_id=uuid4(), overrides={},
    )
    assert report["draft"] is None
    assert report["low_confidence_fields"] == ["price"]
    assert "side" in report["missing_fields"]
    assert "quantity" not in report["missing_fields"]


def test_assembler_user_overrides_fill_missing_fields() -> None:
    result = OcrResult(provider="test", fields=(
        OcrField(key="price", value="10.50", confidence=0.95),
        OcrField(key="quantity", value="100", confidence=0.9),
    ))
    assembler = DraftAssembler()
    report = assembler.build_trade(
        result, account_id=uuid4(), security_id=uuid4(),
        overrides={"side": "SELL", "trade_time": NOW.isoformat()},
    )
    draft = report["draft"]
    assert draft is not None
    assert draft.side.value == "SELL"
    assert draft.field_confidence["side"] == 1.0
    assert report["low_confidence_fields"] == []
    assert report["missing_fields"] == []


def test_assembler_builds_position_draft() -> None:
    result = OcrResult(provider="test", fields=(
        OcrField(key="quantity", value="100", confidence=0.9),
        OcrField(key="average_cost", value="10.20", confidence=0.9),
        OcrField(key="trade_time", value=NOW.isoformat(), confidence=0.9),
    ))
    assembler = DraftAssembler()
    report = assembler.build_position(
        result, account_id=uuid4(), security_id=uuid4(), overrides={},
    )
    draft = report["draft"]
    assert draft is not None
    assert draft.quantity == Decimal("100")
    assert draft.average_cost == Decimal("10.20")
    assert draft.as_of == NOW
