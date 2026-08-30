from __future__ import annotations

from datetime import date, datetime, timezone
from types import TracebackType
from uuid import uuid4

import httpx
import pytest

from app.v3.application.ingest_corporate_actions import IngestCorporateActionsService
from app.v3.domain.market_data import (
    BarIngestionTarget,
    CorporateAction,
    CorporateActionContent,
    CorporateActionDraft,
    CorporateActionFetchResult,
    CorporateActionType,
    Market,
)
from app.v3.infrastructure.providers.corporate_actions import (
    EastmoneyCorporateActionProvider,
)
from tests.v3.test_refresh_universe import snapshot


NOW = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)


def draft(*, plan: str = "10派10元", code: str = "600000") -> CorporateActionDraft:
    return CorporateActionDraft(
        code=code,
        market=Market.SH,
        action_type=CorporateActionType.CASH_DIVIDEND,
        announcement_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
        record_time=datetime(2026, 6, 9, tzinfo=timezone.utc),
        effective_time=datetime(2026, 6, 10, tzinfo=timezone.utc),
        payload={"plan": plan, "cash_dividend_per_10_shares": 10.0},
        source="fixture-actions",
        source_reference=f"fixture://{code}/2025-12-31",
        fetch_time=NOW,
    )


def test_corporate_action_hash_represents_fact_not_ingestion_metadata() -> None:
    base = CorporateActionContent(
        corporate_action_id=uuid4(),
        security_id=uuid4(),
        **draft().model_dump(exclude={"code", "market"}),
        known_at=NOW,
    )
    first = CorporateAction.build(base)
    second = CorporateAction.build(
        base.model_copy(
            update={
                "corporate_action_id": uuid4(),
                "fetch_time": NOW.replace(hour=9),
                "known_at": NOW.replace(hour=9),
                "supersedes_action_id": first.corporate_action_id,
            }
        )
    )

    assert first.content_hash == second.content_hash


@pytest.mark.asyncio
async def test_eastmoney_corporate_action_provider_paginates_and_normalizes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["pageNumber"])
        row = {
            "SECUCODE": "600000.SH",
            "SECURITY_CODE": "600000",
            "REPORT_DATE": f"202{page + 3}-12-31 00:00:00",
            "NOTICE_DATE": "2026-06-01 00:00:00",
            "EQUITY_RECORD_DATE": "2026-06-09 00:00:00",
            "EX_DIVIDEND_DATE": f"2026-06-{page + 9:02d} 00:00:00",
            "ASSIGN_PROGRESS": "实施分配",
            "IMPL_PLAN_PROFILE": "10派10元",
            "PRETAX_BONUS_RMB": 10,
            "BONUS_IT_RATIO": None,
            "BONUS_RATIO": None,
            "IT_RATIO": None,
        }
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {"pages": 2, "count": 2, "data": [row]},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = EastmoneyCorporateActionProvider(client=client)
    result = await provider.fetch_since(date(2026, 1, 1))
    await client.aclose()

    assert len(result.actions) == 2
    assert all(item.action_type is CorporateActionType.CASH_DIVIDEND for item in result.actions)
    assert result.actions[0].payload["cash_dividend_per_10_shares"] == 10.0
    assert result.actions[0].payload["effective_date_status"] == "CONFIRMED"
    assert result.actions[0].source_reference.startswith(
        "eastmoney://RPT_SHAREBONUS_DET/600000.SH/"
    )


class Store:
    def __init__(self) -> None:
        self.snapshot = snapshot(1)
        self.target = BarIngestionTarget(
            security_id=uuid4(), code="600000", market=Market.SH
        )
        self.actions: list[CorporateAction] = []


class FakeUniverses:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def latest(self):
        return self.store.snapshot

    async def targets(self, snapshot_id):
        return (self.store.target,)


class FakeActions:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def latest_by_source_references(self, source, references):
        latest = {}
        for action in self.store.actions:
            if action.source == source and action.source_reference in references:
                latest[action.source_reference] = action
        return latest

    async def publish(self, action):
        if any(item.content_hash == action.content_hash for item in self.store.actions):
            return False
        self.store.actions.append(action)
        return True


class FakeUnitOfWork:
    def __init__(self, store: Store) -> None:
        self.universes = FakeUniverses(store)
        self.corporate_actions = FakeActions(store)

    async def __aenter__(self):
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self):
        return None


class FakeProvider:
    code = "fixture-actions"

    def __init__(self, actions: tuple[CorporateActionDraft, ...]) -> None:
        self.actions = actions

    async def fetch_since(self, since):
        return CorporateActionFetchResult(
            source_code=self.code, fetch_time=NOW, actions=self.actions
        )


@pytest.mark.asyncio
async def test_corporate_action_ingestion_is_idempotent_and_links_corrections() -> None:
    store = Store()
    provider = FakeProvider((draft(), draft(code="999999")))
    service = IngestCorporateActionsService(
        lambda: FakeUnitOfWork(store), (provider,), clock=lambda: NOW
    )

    first = await service.execute(date(2025, 1, 1))
    second = await service.execute(date(2025, 1, 1))
    original_id = store.actions[0].corporate_action_id
    provider.actions = (draft(plan="10派11元"),)
    corrected = await service.execute(date(2025, 1, 1))

    assert (first.published_count, first.outside_universe_count) == (1, 1)
    assert (second.published_count, second.unchanged_count) == (0, 1)
    assert corrected.published_count == 1
    assert len(store.actions) == 2
    assert store.actions[-1].supersedes_action_id == original_id
