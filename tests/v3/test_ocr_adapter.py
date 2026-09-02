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

def _tsv(words) -> str:
    """按真实 tesseract 结构构造 TSV：header + 页行 + level-5 词行。"""
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    rows = [header, "\t".join(["1", "1", "0", "0", "0", "0", "0", "0", "520", "260", "-1", ""])]
    for block, line, word, x, y, w, h, conf, text in words:
        rows.append("\t".join([
            "5", "1", str(block), "1", str(line), str(word),
            str(x), str(y), str(w), str(h), str(conf), text,
        ]))
    return "\n".join(rows)


# 真实截图版式：标签列(block 1)与数值列(block 2)分属不同 block、同一视觉行；
# 中文标签被逐字识别为多个词；tesseract 词行 level=5。
WORDS = [
    # 标签列
    (1, 1, 1, 31, 18, 117, 28, 90.0, "证"),
    (1, 1, 1, 76, 18, 28, 28, 90.0, "券"),
    (1, 1, 1, 104, 18, 25, 28, 90.0, "代"),
    (1, 1, 1, 128, 18, 23, 28, 90.0, "码"),
    (1, 2, 1, 32, 62, 117, 28, 90.0, "成"),
    (1, 2, 1, 74, 62, 26, 28, 90.0, "交"),
    (1, 2, 1, 99, 62, 30, 28, 90.0, "价"),
    (1, 2, 1, 128, 62, 24, 28, 90.0, "格"),
    (1, 3, 1, 32, 106, 117, 28, 90.0, "成"),
    (1, 3, 1, 74, 106, 26, 28, 90.0, "交"),
    (1, 3, 1, 99, 106, 30, 28, 90.0, "数"),
    (1, 3, 1, 128, 106, 24, 28, 90.0, "量"),
    (1, 4, 1, 32, 150, 117, 28, 90.0, "成"),
    (1, 4, 1, 74, 150, 26, 28, 90.0, "交"),
    (1, 4, 1, 98, 150, 26, 28, 90.0, "时"),
    (1, 4, 1, 123, 150, 26, 28, 90.0, "间"),
    # 数值列
    (2, 1, 1, 282, 22, 96, 23, 96.8, "600519"),
    (2, 2, 1, 284, 67, 103, 23, 92.9, "1700.50"),
    (2, 3, 1, 281, 111, 48, 24, 96.9, "200"),
    (2, 4, 1, 281, 156, 145, 24, 96.6, "2026-09-01"),
    (2, 4, 2, 441, 157, 120, 23, 94.0, "09:31:00"),
    (2, 5, 1, 281, 200, 60, 24, 91.0, "买入"),
]
TSV_SAMPLE = _tsv(WORDS)


def test_parse_tesseract_tsv_groups_level5_words_into_lines() -> None:
    lines = parse_tesseract_tsv(TSV_SAMPLE)
    # 标签列 4 行 + 数值列 5 行
    assert len(lines) == 9
    label = lines[0]
    assert label["text"] == "证 券 代 码"
    assert label["confidence"] == pytest.approx(0.9)
    assert label["region"]["x"] == 31
    assert label["region"]["y"] == 18


def test_parse_tesseract_tsv_maps_two_column_fields() -> None:
    from app.v3.application.ocr import map_fields

    lines = parse_tesseract_tsv(TSV_SAMPLE)
    fields = map_fields(lines)
    by_key = {f.key: f for f in fields}
    assert by_key["security_code"].value == "600519"
    assert by_key["price"].value == "1700.50"
    assert by_key["quantity"].value == "200"
    assert by_key["trade_time"].value == "2026-09-01 09:31:00"
    assert by_key["side"].value == "BUY"
    assert by_key["price"].confidence == pytest.approx(0.9)  # 标签行词均值
    assert by_key["price"].region is not None


def test_parse_tesseract_tsv_maps_same_line_label_value() -> None:
    # 同行标签+值（label 后紧跟值的版式）
    words = list(WORDS) + [(3, 1, 1, 10, 20, 100, 24, 90.0, "手续费"), (3, 1, 2, 110, 20, 60, 24, 90.0, "5")]
    from app.v3.application.ocr import map_fields

    lines = parse_tesseract_tsv(_tsv(words))
    fields = map_fields(lines)
    fee = {f.key: f for f in fields}["fee"]
    assert fee.value == "5"


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
    assert {f.key for f in result.fields} >= {
        "security_code", "price", "quantity", "trade_time", "side",
    }


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
