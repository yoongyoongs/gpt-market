from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from app.v3.application.audit_helper import AuditRecorder
from app.v3.domain.ai_import import (
    AIResultBundle,
    AIResultConfirmCommand,
    AIResultConfirmResult,
    AIResultImportPreview,
    ConfirmedGroup,
    GroupCommitStatus,
    ImportGroupPreview,
    ImportStatus,
)


class PreviewAIResultImportService:
    def __init__(self, uow_factory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, bundle: AIResultBundle) -> AIResultImportPreview:
        previews: list[ImportGroupPreview] = []
        async with self._uow_factory() as uow:
            for group in bundle.atomic_groups:
                errors: list[str] = []
                warnings: list[str] = []
                for envelope in group.results:
                    errors.extend(await uow.ai_imports.validate_envelope(envelope))
                previews.append(
                    ImportGroupPreview(
                        group_id=group.group_id,
                        task_run_id=group.task_run_id,
                        valid=not errors,
                        result_ids=tuple(item.result_id for item in group.results),
                        creates=tuple(item.result_type for item in group.results),
                        warnings=tuple(dict.fromkeys(warnings)),
                        errors=tuple(dict.fromkeys(errors)),
                    )
                )
            payload = {
                "preview_revision": 1,
                "bundle": bundle,
                "groups": tuple(previews),
                "status": ImportStatus.PREVIEWED,
                "created_at": datetime.now(timezone.utc),
            }
            from app.v3.domain.hashing import canonical_hash

            preview = AIResultImportPreview(
                **payload,
                content_hash=canonical_hash(payload),
            )
            await uow.ai_imports.add_preview(preview)
            await AuditRecorder(uow).record(
                action="AI_RESULT_PREVIEW",
                object_type="ai_result_import",
                object_id=str(preview.import_id),
                actor_type="AI_AGENT",
                actor_id=bundle.agent.model,
                after={
                    "bundle_hash": bundle.bundle_hash,
                    "groups": [g.group_id for g in bundle.atomic_groups],
                },
                metadata={"bundle_id": str(bundle.bundle_id)},
            )
            await uow.commit()
        return preview


class ConfirmAIResultImportService:
    def __init__(self, uow_factory) -> None:
        self._uow_factory = uow_factory

    async def execute(
        self,
        import_id: UUID,
        command: AIResultConfirmCommand,
    ) -> AIResultConfirmResult:
        async with self._uow_factory() as uow:
            preview = AIResultImportPreview.model_validate(
                await uow.ai_imports.get_preview_payload(import_id)
            )
            claimed = await uow.ai_imports.claim_confirmation(
                import_id,
                command.preview_revision,
                command.bundle_hash,
                command.idempotency_key,
                command.confirmed_by,
            )
            if claimed is None:
                await AuditRecorder(uow).record(
                    action="AI_RESULT_CONFIRM",
                    object_type="ai_result_import",
                    object_id=str(import_id),
                    actor_type="HUMAN",
                    actor_id=command.confirmed_by,
                    after={"idempotency_key": command.idempotency_key},
                    metadata={"bundle_hash": command.bundle_hash},
                )
            await uow.commit()

        successful: list[ConfirmedGroup] = []
        failed: list[ConfirmedGroup] = []
        for group in preview.bundle.atomic_groups:
            preview_group = next(
                item for item in preview.groups if item.group_id == group.group_id
            )
            if not preview_group.valid:
                result = ConfirmedGroup(
                    group_id=group.group_id,
                    status=GroupCommitStatus.FAILED,
                    error="; ".join(preview_group.errors),
                    retryable=False,
                )
                failed.append(result)
                async with self._uow_factory() as uow:
                    await uow.ai_imports.fail_group(
                        preview.bundle.bundle_id,
                        group.group_id,
                        result.error or "invalid group",
                    )
                    await uow.commit()
                continue
            try:
                async with self._uow_factory() as uow:
                    created = await uow.ai_imports.commit_group(
                        import_id, preview.bundle, group
                    )
                    await uow.commit()
                successful.append(
                    ConfirmedGroup(
                        group_id=group.group_id,
                        status=GroupCommitStatus.COMMITTED,
                        result_ids=tuple(item.result_id for item in group.results),
                        created_object_ids=created,
                    )
                )
                async with self._uow_factory() as uow:
                    await AuditRecorder(uow).record(
                        action="AI_RESULT_GROUP_COMMIT",
                        object_type="ai_result_import",
                        object_id=str(import_id),
                        actor_type="SYSTEM",
                        after={
                            "group_id": group.group_id,
                            "created": [str(item) for item in created],
                        },
                        metadata={"bundle_id": str(preview.bundle.bundle_id)},
                    )
                    await uow.commit()
            except Exception as exc:
                async with self._uow_factory() as uow:
                    await uow.ai_imports.fail_group(
                        preview.bundle.bundle_id, group.group_id, str(exc)
                    )
                    await uow.commit()
                failed.append(
                    ConfirmedGroup(
                        group_id=group.group_id,
                        status=GroupCommitStatus.FAILED,
                        error=str(exc),
                        retryable=True,
                    )
                )

        task_statuses: dict[UUID, str] = {}
        for task_run_id in preview.bundle.task_run_ids:
            async with self._uow_factory() as uow:
                task_statuses[task_run_id] = await uow.ai_imports.refresh_task_run(
                    task_run_id
                )
                await uow.commit()
        async with self._uow_factory() as uow:
            status = await uow.ai_imports.finish_import(import_id)
            await uow.commit()
        return AIResultConfirmResult(
            import_id=import_id,
            status=status,
            successful_groups=tuple(successful),
            failed_groups=tuple(failed),
            task_run_statuses=task_statuses,
        )
