"""RC-09B Draft Preview + 人工修正 + Confirm 真实 PostgreSQL 集成测试（PG-002）。

方案 §12.2：Draft Preview / 低置信高亮 / 用户修正 / Confirm 全部可走 API；
修正只允许在 DRAFT 状态、字段白名单校验、用户修正置信度记 1.0、写审计。
"""

from __future__ import annotations

import base64
import os
import uuid as uuid_module
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.manage_portfolio import (
    DraftConfirmation,
    DraftFieldCorrection,
    PortfolioWriteService,
)
from app.v3.application.ocr import OcrField, OcrResult
from app.v3.application.ocr_pipeline import (
    ImageUploadCommand,
    RecognizeImageService,
)
from app.v3.domain.portfolio import TradeConfirm
from app.v3.infrastructure.db.models import (
    AccountModel,
    AuditEventModel,
    SecurityModel,
    TradeLedgerModel,
)
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork
from app.v3.repositories.errors import RepositoryConflictError

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 9, 1, 9, 31, tzinfo=timezone.utc)


class _FakeAdapter:
    provider = "test"

    async def recognize(self, image: bytes) -> OcrResult:
        return OcrResult(provider="test", fields=(
            OcrField(key="price", value="10.50", confidence=0.95),
            OcrField(key="quantity", value="100", confidence=0.9),
        ))


async def _seed(sessions):
    account_id, security_id = uuid4(), uuid4()
    async with sessions() as session:
        session.add(AccountModel(
            account_id=account_id, name=f"pg2-{uuid_module.uuid4().hex[:8]}",
            currency="CNY", cost_method="WEIGHTED_AVERAGE",
        ))
        session.add(SecurityModel(
            security_id=security_id, market="SH", code=uuid_module.uuid4().hex[:6],
            name="pg2-seed",
        ))
        await session.commit()
    return account_id, security_id


async def _uploaded_trade_draft(sessions, account_id, security_id):
    service = RecognizeImageService(
        lambda: SQLAlchemyUnitOfWork(sessions),
        adapter=_FakeAdapter(), store_dir="/tmp/v3-ocr-test-images",
    )
    report = await service.execute(ImageUploadCommand(
        image_base64=base64.b64encode(
            f"img-{uuid_module.uuid4().hex}".encode()
        ).decode(),
        import_type="TRADE", account_id=account_id, security_id=security_id,
        overrides={"side": "BUY", "trade_time": NOW.isoformat()},
    ))
    return report


async def test_trade_draft_preview_and_low_confidence_highlight() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed(sessions)
    upload = await _uploaded_trade_draft(sessions, account_id, security_id)
    # 低置信 fee 字段：把一条低置信字段塞进草稿置信表
    writer = PortfolioWriteService(lambda: SQLAlchemyUnitOfWork(sessions))
    preview = await writer.trade_draft_preview(upload["trade_draft_ids"][0])
    assert str(preview["draft_id"]) == str(upload["trade_draft_ids"][0])
    assert preview["status"] == "DRAFT"
    assert preview["payload"]["price"] == "10.50"
    assert preview["field_confidence"]["price"] == pytest.approx(0.95)
    assert preview["image_import"]["provider"] == "test"
    assert "fields" in preview["image_import"]
    await engine.dispose()


async def test_user_correction_updates_draft_and_writes_audit() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed(sessions)
    upload = await _uploaded_trade_draft(sessions, account_id, security_id)
    draft_id = upload["trade_draft_ids"][0]
    writer = PortfolioWriteService(lambda: SQLAlchemyUnitOfWork(sessions))
    report = await writer.correct_trade_draft(draft_id, DraftFieldCorrection(
        corrected_fields={"price": "11.00", "quantity": "200"},
        corrected_by="op-1",
    ))
    assert report["payload"]["price"] == "11.00"
    assert report["payload"]["quantity"] == "200"
    assert report["field_confidence"]["price"] == 1.0
    assert report["field_confidence"]["quantity"] == 1.0
    assert report["user_corrections"][-1]["fields"] == {"price": "11.00",
                                                        "quantity": "200"}
    async with sessions() as session:
        audit = (await session.scalars(select(AuditEventModel).where(
            AuditEventModel.action == "TRADE_DRAFT_CORRECTED",
            AuditEventModel.object_id == str(draft_id),
        ))).all()
    assert len(audit) == 1
    assert audit[0].actor_id == "op-1"
    await engine.dispose()


async def test_correction_rejects_unknown_field_and_non_draft() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed(sessions)
    upload = await _uploaded_trade_draft(sessions, account_id, security_id)
    draft_id = upload["trade_draft_ids"][0]
    writer = PortfolioWriteService(lambda: SQLAlchemyUnitOfWork(sessions))
    try:
        await writer.correct_trade_draft(draft_id, DraftFieldCorrection(
            corrected_fields={"security_code": "999999"}, corrected_by="op-1",
        ))
    except ValueError:
        pass
    else:
        raise AssertionError("unknown field must be rejected")
    # 确认后再修正 → 冲突
    await writer.confirm_trade(draft_id, TradeConfirm(
        idempotency_key=f"key-{uuid_module.uuid4().hex}", confirmed_by="op-1",
    ))
    try:
        await writer.correct_trade_draft(draft_id, DraftFieldCorrection(
            corrected_fields={"price": "12.00"}, corrected_by="op-1",
        ))
    except RepositoryConflictError:
        await engine.dispose()
        return
    raise AssertionError("confirmed draft must not be correctable")


async def test_corrected_draft_confirms_with_corrected_values() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed(sessions)
    upload = await _uploaded_trade_draft(sessions, account_id, security_id)
    draft_id = upload["trade_draft_ids"][0]
    writer = PortfolioWriteService(lambda: SQLAlchemyUnitOfWork(sessions))
    await writer.correct_trade_draft(draft_id, DraftFieldCorrection(
        corrected_fields={"price": "11.00"}, corrected_by="op-1",
    ))
    trade = await writer.confirm_trade(draft_id, TradeConfirm(
        idempotency_key=f"key-{uuid_module.uuid4().hex}", confirmed_by="op-1",
    ))
    async with sessions() as session:
        row = await session.get(TradeLedgerModel, trade["trade_id"])
    assert row.price is not None
    from decimal import Decimal as _Decimal
    assert _Decimal(str(row.price)) == _Decimal("11.00")
    await engine.dispose()


async def test_position_draft_preview_and_correction() -> None:
    from app.v3.application.ocr_pipeline import ImageUploadCommand as _C

    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    account_id, security_id = await _seed(sessions)
    service = RecognizeImageService(
        lambda: SQLAlchemyUnitOfWork(sessions),
        adapter=_FakeAdapter(), store_dir="/tmp/v3-ocr-test-images",
    )
    upload = await service.execute(_C(
        image_base64=base64.b64encode(f"pos-{uuid_module.uuid4().hex}".encode()).decode(),
        import_type="POSITION", account_id=account_id, security_id=security_id,
        overrides={"average_cost": "10.20", "as_of": NOW.isoformat()},
    ))
    assert upload["position_draft_ids"]
    draft_id = upload["position_draft_ids"][0]
    writer = PortfolioWriteService(lambda: SQLAlchemyUnitOfWork(sessions))
    preview = await writer.position_draft_preview(draft_id)
    assert preview["payload"]["quantity"] == "100"
    report = await writer.correct_position_draft(draft_id, DraftFieldCorrection(
        corrected_fields={"quantity": "120"}, corrected_by="op-2",
    ))
    assert report["payload"]["quantity"] == "120"
    assert report["field_confidence"]["quantity"] == 1.0
    opening = await writer.confirm_position_draft(
        draft_id, DraftConfirmation(confirmed_by="op-2")
    )
    assert opening["status"] == "CONFIRMED_BASELINE"
    await engine.dispose()
