from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.v3.contracts.agent import AIResultEnvelope, AgentIdentity, AgentProvider, AgentType
from app.v3.contracts.evidence import EvidenceRecord, EvidenceType
from app.v3.domain.hashing import canonical_hash, canonical_json
from app.v3.domain.task import TaskGroupCounts, TaskRunStatus, derive_task_run_status


def test_canonical_hash_is_stable_across_mapping_order() -> None:
    first = {"code": "600519", "facts": {"b": 2, "a": 1}}
    second = {"facts": {"a": 1, "b": 2}, "code": "600519"}
    assert canonical_json(first) == canonical_json(second)
    assert canonical_hash(first) == canonical_hash(second)


def test_canonical_json_rejects_non_finite_numbers() -> None:
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json({"invalid": float("nan")})


def test_task_group_counts_and_partial_completed_status() -> None:
    counts = TaskGroupCounts(expected=30, successful=29, failed=1, pending=0)
    assert derive_task_run_status(counts) is TaskRunStatus.PARTIAL_COMPLETED
    assert derive_task_run_status(TaskGroupCounts(expected=1, successful=1, failed=0, pending=0)) is TaskRunStatus.COMPLETED
    assert derive_task_run_status(TaskGroupCounts(expected=1, successful=0, failed=0, pending=1)) is TaskRunStatus.PENDING_IMPORT


def test_task_group_counts_reject_inconsistent_total() -> None:
    with pytest.raises(ValidationError, match="expected must equal"):
        TaskGroupCounts(expected=3, successful=1, failed=0, pending=1)


def test_evidence_requires_timezone_and_monotonic_known_at() -> None:
    fetched = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(ValidationError, match="known_at cannot be earlier"):
        EvidenceRecord(
            evidence_id=uuid4(),
            evidence_type=EvidenceType.FACT,
            subject={"type": "STOCK", "code": "600519"},
            source="fixture",
            fetch_time=fetched,
            known_at=fetched - timedelta(seconds=1),
            confidence=1,
            relevance=1,
            payload={"value": 1},
        )
    with pytest.raises(ValidationError, match="fetch_time must include a timezone"):
        EvidenceRecord(
            evidence_id=uuid4(),
            evidence_type=EvidenceType.FACT,
            subject={"type": "STOCK", "code": "600519"},
            source="fixture",
            fetch_time=fetched.replace(tzinfo=None),
            known_at=fetched,
            confidence=1,
            relevance=1,
            payload={"value": 1},
        )


def test_ai_envelope_verifies_canonical_content_hash() -> None:
    now = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)
    payload = {
        "schema_version": "v3.0",
        "result_id": str(uuid4()),
        "result_type": "MarketReview",
        "agent": {
            "agent_type": AgentType.CHATGPT_WEB.value,
            "provider": AgentProvider.OPENAI.value,
            "model": "UNKNOWN",
            "model_version": None,
        },
        "task_id": str(uuid4()),
        "task_run_id": str(uuid4()),
        "task_profile": "POST_MARKET",
        "trigger_type": "USER_REQUEST",
        "context_pack_id": str(uuid4()),
        "context_pack_hash": "a" * 64,
        "prompt_version": "v1",
        "strategy_version": "v1",
        "produced_at": now.isoformat(),
        "as_of": now.isoformat(),
        "evidence_ids": [],
        "result": {"state": "NEUTRAL"},
    }
    envelope = AIResultEnvelope.build(payload)
    assert envelope.agent == AgentIdentity(
        agent_type=AgentType.CHATGPT_WEB,
        provider=AgentProvider.OPENAI,
        model="UNKNOWN",
    )
    with pytest.raises(ValidationError, match="content_hash does not match"):
        AIResultEnvelope(**payload, content_hash="0" * 64)
