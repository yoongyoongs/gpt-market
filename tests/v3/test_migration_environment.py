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
    assert scripts.get_heads() == ["0011_strategy_stabilization"]


def test_v3_metadata_contains_phase1_through_phase11_tables() -> None:
    tables = {table.fullname for table in Base.metadata.sorted_tables}
    assert {
        "v3.evidence_sources",
        "v3.raw_documents",
        "v3.evidence_records",
        "v3.evidence_fetch_runs",
        "v3.raw_document_parse_attempts",
        "v3.evidence_entity_links",
        "v3.evidence_relations",
        "v3.evidence_conflicts",
        "v3.evidence_conflict_members",
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
        "v3.feature_runs",
        "v3.security_features",
        "v3.market_regime_snapshots",
        "v3.recall_channels",
        "v3.recall_runs",
        "v3.recall_results",
        "v3.raw_opportunities",
        "v3.performance_observations",
        "v3.recall_miss_evaluations",
        "v3.candidate_comparison_packs",
        "v3.candidate_comparison_members",
        "v3.context_packs",
        "v3.context_evidence_selections",
    } <= tables
    assert {
        "v3.ai_result_imports",
        "v3.ai_result_atomic_groups",
        "v3.decisions",
        "v3.entry_plans",
        "v3.accounts",
        "v3.trade_drafts",
        "v3.trade_ledger",
        "v3.position_projections",
        "v3.action_candidates",
        "v3.entry_assessments",
        "v3.position_reviews",
        "v3.performance_attributions",
        "v3.replay_runs",
        "v3.regression_cases",
        "v3.strategy_versions",
        "v3.strategy_experiments",
        "v3.shadow_observations",
        "v3.release_states",
        "v3.release_events",
    } <= tables
