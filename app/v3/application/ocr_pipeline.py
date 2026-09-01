"""图片上传识别管线服务（RC-09A / OCR-001）。

    上传图片(base64) → 落盘 + hash 落库 → OCR Adapter → field+confidence+region
    → Draft Preview（识别不完整 = INCOMPLETE，缺字段绝不硬补）

Adapter 不可用（OcrUnavailableError）时整个请求失败、不落任何数据——诚实契约：
要么识别成功产生可人工确认的 Draft，要么明确报供应商不可用。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import Field

from app.v3.application.ocr import DraftAssembler, OcrAdapter, OcrField
from app.v3.contracts.base import V3Contract


class ImageUploadCommand(V3Contract):
    image_base64: str = Field(min_length=4)
    import_type: str = Field(pattern=r"^(TRADE|POSITION)$")
    account_id: UUID
    security_id: UUID
    overrides: dict[str, Any] = Field(default_factory=dict)


def _fields_payload(fields: tuple[OcrField, ...]) -> list[dict[str, Any]]:
    return [
        {"key": f.key, "value": f.value, "confidence": f.confidence,
         "region": f.region}
        for f in fields
    ]


class RecognizeImageService:
    def __init__(
        self,
        uow_factory,
        *,
        adapter: OcrAdapter,
        store_dir: str,
        clock=None,
    ) -> None:
        self._uow_factory = uow_factory
        self._adapter = adapter
        self._store_dir = Path(store_dir)
        self._clock = clock

    async def execute(self, command: ImageUploadCommand) -> dict[str, Any]:
        try:
            image = base64.b64decode(command.image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("image_base64 is not valid base64") from exc
        image_hash = hashlib.sha256(image).hexdigest()
        self._store_dir.mkdir(parents=True, exist_ok=True)
        image_path = self._store_dir / f"{image_hash}.img"
        if not image_path.exists():
            image_path.write_bytes(image)

        result = await self._adapter.recognize(image)
        assembler = DraftAssembler()
        if command.import_type == "TRADE":
            report = assembler.build_trade(
                result, account_id=command.account_id,
                security_id=command.security_id, overrides=command.overrides,
            )
        else:
            report = assembler.build_position(
                result, account_id=command.account_id,
                security_id=command.security_id, overrides=command.overrides,
            )

        async with self._uow_factory() as uow:
            image_import_id = await uow.portfolios.add_image_import(
                image_hash, str(image_path), command.import_type,
                ocr_payload={
                    "provider": result.provider,
                    "fields": _fields_payload(result.fields),
                    "raw": result.raw,
                },
                field_regions={
                    f.key: f.region for f in result.fields if f.region is not None
                },
            )
            draft_ids: list[UUID] = []
            draft = report["draft"]
            if draft is not None:
                with_image = draft.model_copy(update={"image_import_id": image_import_id})
                if command.import_type == "TRADE":
                    draft_ids.append(await uow.portfolios.add_trade_draft(with_image))
                else:
                    draft_ids.append(
                        await uow.portfolios.add_position_draft(with_image)
                    )
            await uow.commit()

        return {
            "image_import_id": image_import_id,
            "image_hash": image_hash,
            "image_reference": str(image_path),
            "provider": result.provider,
            "status": "DRAFT_ONLY" if draft is not None else "INCOMPLETE",
            "requires_manual_confirmation": True,
            "fields": report["preview_fields"],
            "low_confidence_fields": report["low_confidence_fields"],
            "missing_fields": report["missing_fields"],
            "trade_draft_ids": draft_ids if command.import_type == "TRADE" else [],
            "position_draft_ids": draft_ids if command.import_type == "POSITION" else [],
        }
