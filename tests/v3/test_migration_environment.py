from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.v3.infrastructure.db.base import Base
from app.v3.infrastructure.db import models as _models  # noqa: F401


ROOT = Path(__file__).resolve().parents[2]


def test_alembic_environment_loads_without_database_credentials() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    assert Path(scripts.dir).resolve() == ROOT / "migrations"
    assert scripts.get_heads() == ["0002_market_data_foundation"]


def test_v3_metadata_contains_phase1_and_phase2_foundation_tables() -> None:
    assert {table.fullname for table in Base.metadata.sorted_tables} == {
        "v3.evidence_sources",
        "v3.raw_documents",
        "v3.evidence_records",
        "v3.task_profiles",
        "v3.expected_runs",
        "v3.task_runs",
        "v3.agent_tasks",
        "v3.ai_result_envelopes",
        "v3.audit_events",
        "v3.securities",
        "v3.universe_sources",
        "v3.universe_snapshots",
        "v3.universe_members",
        "v3.universe_diffs",
        "v3.market_data_ingestion_runs",
        "v3.adjustment_factor_revisions",
        "v3.adjustment_factors",
        "v3.bar_series_revisions",
        "v3.market_bars",
        "v3.corporate_actions",
    }
