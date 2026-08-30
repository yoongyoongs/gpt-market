from __future__ import annotations

from uuid import uuid4

import pytest

from app.v3.providers.evidence import EvidenceCapability
from scripts.v3_phase4_evidence import build_registry, parse_resume, parse_time


def test_phase4_job_parses_resume_ids_and_requires_aware_times() -> None:
    run_id = uuid4()
    assert parse_resume([f"cninfo-announcements={run_id}"]) == {
        "cninfo-announcements": run_id
    }
    with pytest.raises(ValueError, match="SOURCE=FETCH_RUN_UUID"):
        parse_resume(["invalid"])
    with pytest.raises(ValueError, match="timezone"):
        parse_time("2026-08-31T09:00:00")


@pytest.mark.asyncio
async def test_phase4_job_registry_contains_all_core_bindings() -> None:
    registry = build_registry(("600519",))
    try:
        assert len(registry.providers_for(EvidenceCapability.ANNOUNCEMENT)) == 2
        assert len(registry.providers_for(EvidenceCapability.FINANCIAL)) == 1
        assert len(registry.providers_for(EvidenceCapability.PERFORMANCE)) == 2
        assert len(registry.providers_for(EvidenceCapability.POLICY)) == 1
        assert len(registry.providers_for(EvidenceCapability.NEWS)) == 1
    finally:
        await registry.close()
