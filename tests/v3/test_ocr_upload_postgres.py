"""RC-09A 图片上传识别管线真实 PostgreSQL 集成测试（OCR-001）。

上传 → hash/reference 落盘落库 → Adapter 识别 → field+confidence+region
→ Draft（识别不完整则 INCOMPLETE，绝不硬补）。Adapter 不可用 → 整体失败。
"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid as uuid_module
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.ocr import (
    OcrField,
    OcrResult,
    OcrUnavailableError,
)
from app.v3.application.ocr_pipeline import (
    ImageUploadCommand,
    RecognizeImageService,
)
from app.v3.infrastructure.db.models import (
    AccountModel,
    ImageImportModel,
    SecurityModel,
    TradeDraftModel,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 9, 1, 9, 31, tzinfo=timezone.utc)


class _FakeAdapter:
    provider = "test"

    def __init__(self, fields=(), error: str | None = None):
        self._fields = fields
        self._error = error

    async def recognize(self, image: bytes) -> OcrResult:
        if self._error:
            raise OcrUnavailableError(self._error)
        return OcrResult(provider=self.provider, fields=self._fields)


def _trade_fields():
    return (
        OcrField(key="side", value="BUY", confidence=0.9),
        OcrField(key="price", value="10.50", confidence=0.95,
                 region={"x": 100, "y": 40, "w": 60, "h": 20}),
        OcrField(key="quantity", value="100", confidence=0.9),
        OcrField(key="fee", value="5", confidence=0.8),
        OcrField(key="trade_time", value=NOW.isoformat(), confidence=0.85),
    )


async def _seed_account_security(sessions):
    account_id, security_id = uuid4(), uuid4()
    async with sessions() as session:
        session.add(AccountModel(
            account_id=account_id, name=f"ocr-{uuid_module.uuid4().hex[:8]}",
            currency="CNY", cost_method="WEIGHTED_AVERAGE",
        ))
        session.add(SecurityModel(
            security_id=security_id, market="SH", code=uuid_module.uuid4().hex[:6],
            name="ocr-seed",
        ))
        await session.commit()
    return account_id, security_id


def _command(account_id, security_id, fields_error=None, **overrides):
    payload = dict(
        image_base64=base64.b64encode(
            f"fake-image-bytes-{uuid_module.uuid4().hex}".encode()
        ).decode(),
        import_type="TRADE", account_id=account_id, security_id=security_id,
    )
    payload.update(overrides)
    return ImageUploadCommand(**payload), fields_error


async def test_upload_recognizes_and_creates_trade_draft(tmp_path) -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account_security(sessions)
    service = RecognizeImageService(
        lambda: SQLAlchemyUnitOfWork(sessions),
        adapter=_FakeAdapter(_trade_fields()), store_dir=str(tmp_path),
    )
    command, _ = _command(account_id, security_id)
    report = await service.execute(command)
    assert report["status"] == "DRAFT_ONLY"
    assert report["provider"] == "test"
    assert report["missing_fields"] == []
    assert len(report["trade_draft_ids"]) == 1

    image_hash = hashlib.sha256(
        base64.b64decode(command.image_base64)
    ).hexdigest()
    # 图片已按 hash 落盘
    assert (tmp_path / f"{image_hash}.img").exists()
    async with sessions() as session:
        import_row = (await session.scalars(select(ImageImportModel).where(
            ImageImportModel.image_hash == image_hash
        ))).one()
        draft_row = await session.get(TradeDraftModel, report["trade_draft_ids"][0])
    assert import_row.import_type == "TRADE"
    assert import_row.image_reference.endswith(f"{image_hash}.img")
    assert import_row.field_regions["price"]["x"] == 100
    assert draft_row is not None
    assert draft_row.status == "DRAFT"
    assert str(draft_row.image_import_id) == str(import_row.image_import_id)
    assert draft_row.payload["price"] == "10.50"
    assert draft_row.field_confidence["price"] == pytest.approx(0.95)
    await engine.dispose()


async def test_upload_incomplete_recognition_creates_no_draft(tmp_path) -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account_security(sessions)
    service = RecognizeImageService(
        lambda: SQLAlchemyUnitOfWork(sessions),
        adapter=_FakeAdapter((OcrField(key="price", value="10.50", confidence=0.3),)),
        store_dir=str(tmp_path),
    )
    command, _ = _command(account_id, security_id)
    report = await service.execute(command)
    assert report["status"] == "INCOMPLETE"
    assert "side" in report["missing_fields"]
    assert report["trade_draft_ids"] == []
    assert report["low_confidence_fields"] == ["price"]
    async with sessions() as session:
        rows = (await session.scalars(select(TradeDraftModel).where(
            TradeDraftModel.account_id == account_id
        ))).all()
    assert rows == []
    await engine.dispose()


async def test_upload_with_unavailable_adapter_writes_nothing(tmp_path) -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account_security(sessions)
    service = RecognizeImageService(
        lambda: SQLAlchemyUnitOfWork(sessions),
        adapter=_FakeAdapter(error="TESSERACT_NOT_AVAILABLE"),
        store_dir=str(tmp_path),
    )
    command, _ = _command(account_id, security_id)
    with pytest.raises(OcrUnavailableError):
        await service.execute(command)
    async with sessions() as session:
        rows = (await session.scalars(select(ImageImportModel).where(
            ImageImportModel.image_hash == hashlib.sha256(
                base64.b64decode(command.image_base64)
            ).hexdigest()
        ))).all()
    assert rows == []
    await engine.dispose()


async def test_upload_invalid_base64_rejected(tmp_path) -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed_account_security(sessions)
    service = RecognizeImageService(
        lambda: SQLAlchemyUnitOfWork(sessions),
        adapter=_FakeAdapter(_trade_fields()), store_dir=str(tmp_path),
    )
    command, _ = _command(account_id, security_id, image_base64="!!!not-base64!!!")
    with pytest.raises(ValueError):
        await service.execute(command)
    await engine.dispose()
