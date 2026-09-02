from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.api.v3 import router
from app.v3.contracts.agent import (
    AIResultEnvelope,
    AgentIdentity,
    AgentProvider,
    AgentType,
)
from app.v3.domain.action import (
    ActionCandidateCreate,
    ActionState,
    EntryAssessmentCreate,
    EntryReadiness,
)
from app.v3.domain.ai_import import AIResultAtomicGroup, AIResultBundle
from app.v3.application.import_ai_results import ConfirmAIResultImportService
from app.v3.domain.ai_import import (
    AIResultConfirmCommand,
    AIResultImportPreview,
    ImportGroupPreview,
    ImportStatus,
)
from app.v3.domain.hashing import canonical_hash
from app.v3.domain.decision import (
    ExpectedHorizon,
    TimeEfficiencyState,
    WatchlistState,
    validate_watchlist_transition,
)
from app.v3.domain.performance import (
    PerformanceAbility,
    PerformanceAttributionCreate,
    ReplayRunCreate,
)
from app.v3.domain.portfolio import (
    EffectiveTradeState,
    PositionProjection,
    TradeCorrectionCreate,
    TradeCorrectionStep,
    TradeDraftCreate,
    TradeSide,
    apply_trade_correction_chain,
)
from app.v3.domain.strategy import (
    ActorType,
    ExperimentEventCommand,
    ExperimentType,
    ReleaseMode,
    StrategyActivationCommand,
    StrategyExperimentCreate,
    StrategyRollbackCommand,
    StrategyVersionCreate,
)


NOW = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
HASH = "a" * 64
AGENT = AgentIdentity(
    agent_type=AgentType.CHATGPT_WEB,
    provider=AgentProvider.OPENAI,
    model="acceptance-fixture",
)


def test_phase8_trade_corrections_apply_partial_patches_cumulatively() -> None:
    original = EffectiveTradeState(
        side=TradeSide.BUY,
        quantity=Decimal("100"),
        price=Decimal("10"),
        fee=Decimal("5"),
    )
    quantity_patch = TradeCorrectionStep.build(
        correction_type="CORRECT",
        replacement={"quantity": "80"},
        previous_state=original,
    )
    after_quantity = apply_trade_correction_chain(original, (quantity_patch,))
    fee_patch = TradeCorrectionStep.build(
        correction_type="CORRECT",
        replacement={"fee": "4"},
        previous_state=after_quantity,
    )

    effective = apply_trade_correction_chain(
        original, (quantity_patch, fee_patch)
    )

    assert effective.quantity == Decimal("80")
    assert effective.fee == Decimal("4")
    assert effective.price == Decimal("10")
    assert effective.reversed is False


def test_phase8_trade_reverse_is_terminal_and_hash_chain_is_verified() -> None:
    original = EffectiveTradeState(
        side=TradeSide.BUY,
        quantity=Decimal("100"),
        price=Decimal("10"),
        fee=Decimal("5"),
    )
    reverse = TradeCorrectionStep.build(
        correction_type="REVERSE", replacement={}, previous_state=original
    )
    reversed_state = apply_trade_correction_chain(original, (reverse,))
    assert reversed_state.reversed is True

    later_patch = TradeCorrectionStep(
        correction_type="CORRECT",
        replacement={"fee": "4"},
        previous_effective_hash=reversed_state.effective_hash,
        effective_hash="0" * 64,
    )
    with pytest.raises(ValueError, match="reversed trade is terminal"):
        apply_trade_correction_chain(original, (reverse, later_patch))

    tampered = reverse.model_copy(update={"previous_effective_hash": "f" * 64})
    with pytest.raises(ValueError, match="previous effective hash"):
        apply_trade_correction_chain(original, (tampered,))


def test_phase8_trade_correction_contract_rejects_ambiguous_patch() -> None:
    with pytest.raises(ValidationError, match="replacement must be empty"):
        TradeCorrectionCreate(
            trade_id=uuid4(), correction_type="REVERSE",
            replacement={"fee": "1"}, reason="reverse", confirmed_by="human",
        )
    with pytest.raises(ValidationError, match="unsupported correction fields"):
        TradeCorrectionCreate(
            trade_id=uuid4(), correction_type="CORRECT",
            replacement={"trade_time": NOW.isoformat()},
            reason="bad patch", confirmed_by="human",
        )


def envelope(
    result_type: str = "DecisionResult",
    result: dict | None = None,
    task_run_id=None,
):
    return AIResultEnvelope.build(
        {
            "result_id": uuid4(),
            "result_type": result_type,
            "agent": AGENT,
            "task_id": uuid4(),
            "task_run_id": task_run_id or uuid4(),
            "task_profile": "DEEP_REPLAY",
            "trigger_type": "USER_REQUEST",
            "context_pack_id": uuid4(),
            "context_pack_hash": HASH,
            "prompt_version": "p1",
            "strategy_version": "v3.1",
            "produced_at": NOW,
            "as_of": NOW,
            "result": result or {"decision": "OBSERVE"},
        }
    )


def test_phase7_ai_cannot_assert_trade_or_holding_facts() -> None:
    item = envelope(result={"actual_trade": True})
    with pytest.raises(ValidationError, match="unconfirmed trade or holding"):
        AIResultAtomicGroup.build(
            group_id="stock-1",
            task_run_id=item.task_run_id,
            results=(item,),
            dependencies={},
        )


def test_phase7_dependencies_cannot_cross_atomic_group() -> None:
    item = envelope()
    with pytest.raises(ValidationError, match="remain inside the group"):
        AIResultAtomicGroup.build(
            group_id="stock-1",
            task_run_id=item.task_run_id,
            results=(item,),
            dependencies={item.result_id: (uuid4(),)},
        )


def test_phase7_bundle_hash_detects_preview_payload_drift() -> None:
    item = envelope()
    group = AIResultAtomicGroup.build(
        group_id="stock-1",
        task_run_id=item.task_run_id,
        results=(item,),
        dependencies={},
    )
    bundle = AIResultBundle.build(
        agent=AGENT,
        task_run_ids=(item.task_run_id,),
        produced_at=NOW,
        atomic_groups=(group,),
    )
    with pytest.raises(ValidationError, match="bundle_hash"):
        AIResultBundle.model_validate(
            {**bundle.model_dump(), "produced_at": NOW + timedelta(seconds=1)}
        )


def test_phase7_holding_and_closed_states_require_ledger_facts() -> None:
    with pytest.raises(ValueError, match="confirmed positive ledger position"):
        validate_watchlist_transition(
            WatchlistState.TRIGGERED,
            WatchlistState.HOLDING,
            confirmed_position_quantity=0,
        )
    with pytest.raises(ValueError, match="zero confirmed ledger quantity"):
        validate_watchlist_transition(
            WatchlistState.HOLDING,
            WatchlistState.CLOSED,
            confirmed_position_quantity=100,
        )


def test_phase8_trade_plan_binding_is_all_or_nothing() -> None:
    with pytest.raises(ValidationError, match="supplied together"):
        TradeDraftCreate(
            account_id=uuid4(),
            security_id=uuid4(),
            side=TradeSide.BUY,
            trade_time=NOW,
            price=Decimal("10.20"),
            quantity=Decimal("100"),
            entry_plan_id=uuid4(),
        )


def test_phase8_position_projection_hash_is_deterministic_and_tamper_evident() -> None:
    values = {
        "account_id": uuid4(),
        "security_id": uuid4(),
        "quantity": Decimal("100"),
        "cost_basis": Decimal("1020"),
        "average_cost": Decimal("10.2"),
        "cash_impact": Decimal("-1020"),
        "realized_pnl": Decimal("0"),
        "last_ledger_sequence": 1,
        "last_adjustment_sequence": 0,
        "projection_version": 1,
        "rebuilt_at": NOW,
    }
    first = PositionProjection.build(**values)
    second = PositionProjection.build(**values)
    assert first.input_hash == second.input_hash
    with pytest.raises(ValidationError):
        PositionProjection.model_validate(
            {**first.model_dump(), "input_hash": "b" * 64, "quantity": Decimal("200")}
        )


def test_phase9_action_and_entry_reject_unified_scores() -> None:
    common = {
        "raw_opportunity_id": uuid4(),
        "security_id": uuid4(),
        "task_run_id": uuid4(),
        "context_pack_id": uuid4(),
        "context_pack_hash": HASH,
        "action_state": ActionState.ACTIONABLE,
        "expected_horizon": ExpectedHorizon.D3_10,
        "time_efficiency": TimeEfficiencyState.NORMAL,
        "time_efficiency_reason": "within expected window",
        "supporting_facts": {"final_total_score": 99},
        "contrary_facts": {},
        "conditions": {},
        "as_of": NOW,
    }
    with pytest.raises(ValidationError, match="unified final score"):
        ActionCandidateCreate(**common)
    with pytest.raises(ValidationError, match="unified final score"):
        EntryAssessmentCreate(
            action_candidate_id=uuid4(),
            readiness=EntryReadiness.READY,
            trigger_facts={"opportunity_score": 88},
            cancel_facts={},
            time_efficiency=TimeEfficiencyState.NORMAL,
            explanation="trigger observed",
            as_of=NOW,
        )


def test_phase10_attribution_requires_maturity_and_real_trade_for_execution() -> None:
    common = {
        "subject_type": "SECURITY",
        "subject_id": uuid4(),
        "strategy_version": "v3.1",
        "horizon_sessions": 5,
        "as_of": NOW,
        "matures_at": NOW + timedelta(days=5),
        "known_at": NOW + timedelta(days=5),
        "explanation": "acceptance",
    }
    with pytest.raises(ValidationError, match="requires a trade"):
        PerformanceAttributionCreate(
            ability=PerformanceAbility.USER_EXECUTION,
            **common,
        )
    with pytest.raises(ValidationError, match="written after maturity"):
        PerformanceAttributionCreate(
            ability=PerformanceAbility.SELECTION,
            **{**common, "known_at": NOW + timedelta(days=4)},
        )


def test_phase10_replay_rejects_naive_as_of() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        ReplayRunCreate(
            strategy_version="v3.1",
            replay_as_of=datetime(2026, 9, 1),
        )


def test_phase11_version_and_experiment_chains_are_strict() -> None:
    with pytest.raises(ValidationError, match="requires a predecessor"):
        StrategyVersionCreate(
            strategy_code="mainboard",
            version=2,
            configuration={},
            rationale="candidate",
            created_by="human",
        )
    with pytest.raises(ValidationError, match="cannot route user traffic"):
        StrategyExperimentCreate(
            experiment_type=ExperimentType.SHADOW,
            treatment_strategy_version_id=uuid4(),
            guardrail_version_id=uuid4(),
            allocation_percent=10,
            starts_at=NOW,
            created_by="human",
        )
    with pytest.raises(ValidationError, match="requires a control"):
        StrategyExperimentCreate(
            experiment_type=ExperimentType.AB,
            treatment_strategy_version_id=uuid4(),
            guardrail_version_id=uuid4(),
            allocation_percent=10,
            starts_at=NOW,
            created_by="human",
        )


def test_phase11_ai_cannot_activate_control_experiment_or_rollback() -> None:
    with pytest.raises(ValidationError, match="only a human"):
        StrategyActivationCommand(
            proposal_id=uuid4(),
            strategy_version_id=uuid4(),
            guardrail_version_id=uuid4(),
            actor_type=ActorType.AI,
            actor_id="agent",
            approval_reason="self approve",
            expected_row_version=0,
        )
    with pytest.raises(ValidationError, match="cannot start"):
        ExperimentEventCommand(
            event_type="STARTED",
            actor_type=ActorType.AI,
            actor_id="agent",
            reason="self start",
        )
    with pytest.raises(ValidationError, match="cannot roll back"):
        StrategyRollbackCommand(
            actor_type=ActorType.AI,
            actor_id="agent",
            reason="self rollback",
            expected_row_version=1,
            target_mode=ReleaseMode.V2,
        )


@pytest.mark.asyncio
async def test_phase7_twenty_nine_of_thirty_groups_are_partial_and_isolated() -> None:
    task_run_id = uuid4()
    groups = []
    previews = []
    for index in range(30):
        item = envelope(task_run_id=task_run_id)
        group = AIResultAtomicGroup.build(
            group_id=f"stock-{index + 1}",
            task_run_id=task_run_id,
            results=(item,),
            dependencies={},
        )
        groups.append(group)
        previews.append(
            ImportGroupPreview(
                group_id=group.group_id,
                task_run_id=task_run_id,
                valid=True,
                result_ids=(item.result_id,),
                creates=(item.result_type,),
            )
        )
    bundle = AIResultBundle.build(
        agent=AGENT,
        task_run_ids=(task_run_id,),
        produced_at=NOW,
        atomic_groups=tuple(groups),
    )
    payload = {
        "preview_revision": 1,
        "bundle": bundle,
        "groups": tuple(previews),
        "status": ImportStatus.PREVIEWED,
        "created_at": NOW,
    }
    preview = AIResultImportPreview(**payload, content_hash=canonical_hash(payload))

    class Imports:
        async def get_preview_payload(self, import_id):
            return preview.model_dump(mode="python")

        async def claim_confirmation(self, *args):
            return None

        async def commit_group(self, import_id, candidate_bundle, group):
            if group.group_id == "stock-30":
                raise RuntimeError("isolated fixture failure")
            return (uuid4(),)

        async def fail_group(self, *args):
            return None

        async def refresh_task_run(self, candidate_task_run_id):
            return "PARTIAL_COMPLETED"

        async def finish_import(self, import_id):
            return ImportStatus.PARTIAL_COMPLETED

    imports = Imports()

    class Audits:
        def __init__(self):
            self.events = []

        async def add(self, event):
            self.events.append(event)

    class Uow:
        ai_imports = imports
        audits = Audits()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def commit(self):
            return None

    result = await ConfirmAIResultImportService(Uow).execute(
        preview.import_id,
        AIResultConfirmCommand(
            preview_revision=1,
            bundle_hash=bundle.bundle_hash,
            idempotency_key="acceptance-key-0001",
            confirmed_by="acceptance-human",
        ),
    )

    assert result.status is ImportStatus.PARTIAL_COMPLETED
    assert len(result.successful_groups) == 29
    assert len(result.failed_groups) == 1
    assert result.failed_groups[0].group_id == "stock-30"
    assert result.task_run_statuses == {task_run_id: "PARTIAL_COMPLETED"}


def test_phase7_11_openapi_exposes_expected_surface_without_auto_trading() -> None:
    app = FastAPI()
    app.include_router(router)
    paths = app.openapi()["paths"]
    expected = {
        "/api/v3/ai-results/imports/preview": "post",
        "/api/v3/ai-results/imports/{import_id}/confirm": "post",
        "/api/v3/portfolio/trade-drafts/{draft_id}/confirm": "post",
        "/api/v3/portfolio/accounts/{account_id}/positions/{security_id}": "get",
        "/api/v3/actions": "post",
        "/api/v3/entries": "post",
        "/api/v3/performance/attributions": "post",
        "/api/v3/replays": "post",
        "/api/v3/strategies/experiments/{experiment_id}/assign": "get",
        "/api/v3/strategies/releases/{environment}/activate": "post",
        "/api/v3/strategies/releases/{environment}/rollback": "post",
        "/api/v3/operations/health-events": "post",
    }
    for path, method in expected.items():
        assert method in paths[path]
    forbidden_tokens = {"broker", "place-order", "execute-trade", "auto-trade"}
    assert not any(
        token in path
        for path in paths
        for token in forbidden_tokens
    )
