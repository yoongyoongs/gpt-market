from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import uuid4

from app.v3.domain.market_data import (
    CorporateAction,
    CorporateActionContent,
    CorporateActionFetchResult,
)
from app.v3.providers.corporate_actions import CorporateActionProvider
from app.v3.repositories.protocols import UnitOfWork


class AllCorporateActionProvidersFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class CorporateActionIngestionResult:
    source_code: str
    fetched_count: int
    published_count: int
    unchanged_count: int
    outside_universe_count: int
    provider_errors: tuple[str, ...]


class IngestCorporateActionsService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        providers: Sequence[CorporateActionProvider],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not providers:
            raise ValueError("at least one corporate action provider is required")
        self._uow_factory = uow_factory
        self._providers = tuple(providers)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(self, since: date) -> CorporateActionIngestionResult:
        fetched, errors = await self._fetch(since)
        async with self._uow_factory() as uow:
            snapshot = await uow.universes.latest()
            if snapshot is None:
                raise ValueError("no universe snapshot is available for corporate actions")
            targets = await uow.universes.targets(snapshot.snapshot_id)
            security_ids = {(target.market, target.code): target.security_id for target in targets}
            relevant = tuple(
                action
                for action in fetched.actions
                if (action.market, action.code) in security_ids
            )
            latest = await uow.corporate_actions.latest_by_source_references(
                fetched.source_code,
                tuple(action.source_reference for action in relevant),
            )
            published = 0
            unchanged = 0
            known_at = max(self._clock(), fetched.fetch_time)
            for draft in relevant:
                previous = latest.get(draft.source_reference)
                action = CorporateAction.build(
                    CorporateActionContent(
                        corporate_action_id=uuid4(),
                        security_id=security_ids[(draft.market, draft.code)],
                        action_type=draft.action_type,
                        announcement_time=draft.announcement_time,
                        record_time=draft.record_time,
                        effective_time=draft.effective_time,
                        payload=draft.payload,
                        source=draft.source,
                        source_reference=draft.source_reference,
                        fetch_time=draft.fetch_time,
                        known_at=known_at,
                        supersedes_action_id=(
                            previous.corporate_action_id if previous is not None else None
                        ),
                    )
                )
                if previous is not None and previous.content_hash == action.content_hash:
                    unchanged += 1
                    continue
                if await uow.corporate_actions.publish(action):
                    published += 1
                else:
                    unchanged += 1
            if published:
                await uow.commit()
        return CorporateActionIngestionResult(
            source_code=fetched.source_code,
            fetched_count=len(fetched.actions),
            published_count=published,
            unchanged_count=unchanged,
            outside_universe_count=len(fetched.actions) - len(relevant),
            provider_errors=errors,
        )

    async def _fetch(
        self, since: date
    ) -> tuple[CorporateActionFetchResult, tuple[str, ...]]:
        errors: list[str] = []
        for provider in self._providers:
            try:
                fetched = await provider.fetch_since(since)
                if fetched.source_code != provider.code:
                    raise ValueError("provider returned a mismatched source_code")
                return fetched, tuple(errors)
            except Exception as exc:
                errors.append(f"{provider.code}:{type(exc).__name__}:{exc}")
        raise AllCorporateActionProvidersFailed(" | ".join(errors))
