"""RC-07B Release Resolver 真实 PostgreSQL 集成测试。"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.v3.application.release_resolver import ReleaseResolver
from app.v3.domain.strategy import (
    ActorType,
    GuardrailVersionCreate,
    StrategyRollbackCommand,
    StrategyVersionCreate,
)
from app.v3.infrastructure.db.models import ReleaseStateModel
from app.v3.infrastructure.db.uow import SQLAlchemyUnitOfWork

DATABASE_URL = os.getenv("V3_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="V3_TEST_DATABASE_URL is not configured"
)
NOW = datetime(2026, 8, 28, 8, tzinfo=timezone.utc)


async def _seed_v3_release(sessions, environment: str) -> dict:
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        guardrail_id = await uow.strategies.add_guardrail(GuardrailVersionCreate(
            guardrail_code=f"gr-{uuid4().hex[:8]}", version=1,
            max_error_rate=Decimal("0.05"), max_p95_ms=Decimal("2000"),
            min_shadow_sample_count=1, max_divergence_rate=Decimal("0.10"),
            max_capacity_utilization=Decimal("0.8"), created_by="seed",
        ))
        strategy_id = uuid4()
        await uow.strategies.add_strategy_version(StrategyVersionCreate(
            strategy_version_id=strategy_id,
            strategy_code=f"strat-{uuid4().hex[:8]}", version=1,
            configuration={"feature_version": "feat-v2",
                           "horizons": [1, 3, 5, 10, 20]},
            rationale="resolver seed", created_by="seed",
        ))
        await uow._session.flush()
        uow._session.add(ReleaseStateModel(
            release_state_id=uuid4(), environment=environment, mode="V3",
            active_strategy_version_id=strategy_id,
            active_guardrail_version_id=guardrail_id, row_version=1,
            updated_at=NOW,
        ))
        await uow.commit()
        return {"strategy_id": strategy_id, "guardrail_id": guardrail_id}


async def test_resolver_reads_release_state_and_rollback_takes_effect() -> None:
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    environment = f"prod-{uuid4().hex[:8]}"
    seeded = await _seed_v3_release(sessions, environment)
    service = ReleaseResolver(
        lambda: SQLAlchemyUnitOfWork(sessions), v3_enabled=True, clock=lambda: NOW,
    )
    report = await service.resolve(environment)
    assert report["effective_mode"] == "V3"
    assert report["strategy_version_id"] == seeded["strategy_id"]
    assert report["configuration"]["feature_version"] == "feat-v2"

    # rollback 落库后，同一 resolver 不重启 → 下一次 resolve 立即 V2
    async with SQLAlchemyUnitOfWork(sessions) as uow:
        await uow.strategies.rollback(environment, StrategyRollbackCommand(
            actor_type=ActorType.HUMAN, actor_id="ops",
            reason="resolver integration", expected_row_version=1,
        ))
        await uow.commit()
    rolled = await service.resolve(environment)
    assert rolled["effective_mode"] == "V2"
    assert rolled["reason"] == "RELEASE_MODE_V2"
    assert rolled["configuration"] is None
    await engine.dispose()
