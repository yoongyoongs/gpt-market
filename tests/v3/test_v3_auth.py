from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.v3.domain.ai_import import AIResultConfirmCommand
from app.v3.domain.decision import DecisionCorrectionCommand
from app.v3.domain.portfolio import (
    AdjustmentConfirmation,
    AdjustmentType,
    PortfolioAdjustmentCreate,
)
from app.v3.domain.strategy import ActorType, StrategyProposalCreate, StrategyVersionCreate
from app.v3.security import (
    V3AuthMiddleware,
    V3AuthPolicy,
    V3Principal,
    V3Scope,
    bind_v3_principal,
)


def _ok():
    return {"ok": True}


def _client(*, public_market_read: bool = True) -> TestClient:
    app = FastAPI()
    app.add_middleware(
        V3AuthMiddleware,
        policy=V3AuthPolicy(
            api_token="write-secret",
            api_principal_id="operator-1",
            strategy_admin_token="admin-secret",
            strategy_admin_principal_id="admin-1",
            public_market_read=public_market_read,
        ),
    )

    @app.get("/api/v3/universe/features")
    async def market_read():
        return {"ok": True}

    for path in (
        "/api/v3/watchlist",
        "/api/v3/decisions",
        "/api/v3/performance",
        "/api/v3/task-runs",
        "/api/v3/release/resolution",
        "/api/v3/strategies",
        "/api/v3/entry-plans/1/versions",
        "/api/v3/stocks/600000/decision-pipeline",
        "/api/v3/stocks/600000/decision-context",
        "/api/v3/intraday/attention",
    ):
        app.get(path)(_ok)

    for path in (
        "/api/v3/market-regime",
        "/api/v3/recalls/misses",
        "/api/v3/evidence/SECURITY/1",
        "/api/v3/stocks/600000/evidence",
        "/api/v3/stocks/600000/context-pack",
        "/api/v3/context-packs/1",
        "/api/v3/market/intraday-status",
        "/api/v3/health/data-quality",
        "/api/v3/raw-opportunities",
        "/api/v3/market-reviews",
        "/api/v3/candidates/comparison-pack",
    ):
        app.get(path)(_ok)

    @app.get("/api/v3/portfolio-preferences")
    async def portfolio_alias(request: Request):
        return {"principal_id": request.state.v3_principal.principal_id}

    @app.get("/api/v3/portfolio/account")
    async def portfolio_read(request: Request):
        return {"principal_id": request.state.v3_principal.principal_id}

    @app.post("/api/v3/portfolio/accounts")
    async def portfolio_write(request: Request):
        return {"principal_id": request.state.v3_principal.principal_id}

    @app.post("/api/v3/strategies/releases/production/activate")
    async def activate(request: Request):
        return {"principal_id": request.state.v3_principal.principal_id}

    return TestClient(app)


def test_market_read_is_public_by_default() -> None:
    with _client() as client:
        response = client.get("/api/v3/universe/features")
    assert response.status_code == 200


PRIVATE_READ_PATHS = (
    "/api/v3/watchlist",
    "/api/v3/decisions",
    "/api/v3/performance",
    "/api/v3/task-runs",
    "/api/v3/release/resolution",
    "/api/v3/strategies",
    "/api/v3/entry-plans/1/versions",
    "/api/v3/stocks/600000/decision-pipeline",
    "/api/v3/stocks/600000/decision-context",
    "/api/v3/intraday/attention",
)


PUBLIC_MARKET_PATHS = (
    "/api/v3/market-regime",
    "/api/v3/recalls/misses",
    "/api/v3/evidence/SECURITY/1",
    "/api/v3/stocks/600000/evidence",
    "/api/v3/stocks/600000/context-pack",
    "/api/v3/context-packs/1",
    "/api/v3/market/intraday-status",
    "/api/v3/health/data-quality",
    "/api/v3/raw-opportunities",
    "/api/v3/market-reviews",
    "/api/v3/candidates/comparison-pack",
)


def test_public_market_read_allowlist_is_anonymous() -> None:
    """NEW-SEC-001：allowlist 内的市场事实 GET 无 token 可读。"""
    with _client() as client:
        for path in PUBLIC_MARKET_PATHS:
            response = client.get(path)
            assert response.status_code == 200, path


def test_private_reads_require_token_even_when_market_read_is_public() -> None:
    """NEW-SEC-001：Watchlist/Decision/Task/Release/Strategy 等不再公开。"""
    with _client() as client:
        for path in PRIVATE_READ_PATHS:
            response = client.get(path)
            assert response.status_code == 401, path
            assert response.json()["details"]["required_scope"] == "MARKET_READ"


def test_any_valid_token_reads_private_paths() -> None:
    with _client() as client:
        for path in PRIVATE_READ_PATHS:
            response = client.get(
                path, headers={"Authorization": "Bearer write-secret"},
            )
            assert response.status_code == 200, path


def test_portfolio_preferences_alias_needs_portfolio_scope() -> None:
    """别名不得绕开 /portfolio 的 PORTFOLIO_READ 边界。"""
    with _client() as client:
        anonymous = client.get("/api/v3/portfolio-preferences")
        authorized = client.get(
            "/api/v3/portfolio-preferences",
            headers={"Authorization": "Bearer write-secret"},
        )
    assert anonymous.status_code == 401
    assert authorized.status_code == 200
    assert authorized.json() == {"principal_id": "operator-1"}


def test_market_read_can_be_protected_by_configuration() -> None:
    with _client(public_market_read=False) as client:
        response = client.get("/api/v3/universe/features")
    assert response.status_code == 401
    assert response.json()["details"]["required_scope"] == "MARKET_READ"


def test_portfolio_read_and_write_require_authentication() -> None:
    with _client() as client:
        read_response = client.get("/api/v3/portfolio/account")
        write_response = client.post("/api/v3/portfolio/accounts")
    assert read_response.status_code == 401
    assert write_response.status_code == 401
    assert read_response.json()["code"] == "V3_UNAUTHORIZED"
    assert write_response.json()["details"]["required_scope"] == "V3_WRITE"


def test_valid_write_token_sets_server_principal() -> None:
    with _client() as client:
        response = client.post(
            "/api/v3/portfolio/accounts",
            headers={"Authorization": "Bearer write-secret", "X-Request-ID": "request-1"},
        )
    assert response.status_code == 200
    assert response.json() == {"principal_id": "operator-1"}


def test_write_scope_cannot_activate_strategy() -> None:
    with _client() as client:
        response = client.post(
            "/api/v3/strategies/releases/production/activate",
            headers={"Authorization": "Bearer write-secret"},
        )
    assert response.status_code == 403
    assert response.json()["details"]["required_scope"] == "STRATEGY_ADMIN"


def test_strategy_admin_token_can_enter_business_gate() -> None:
    with _client() as client:
        response = client.post(
            "/api/v3/strategies/releases/production/activate",
            headers={"Authorization": "Bearer admin-secret"},
        )
    assert response.status_code == 200
    assert response.json() == {"principal_id": "admin-1"}


def test_confirmed_by_is_overwritten_by_authenticated_principal() -> None:
    command = AIResultConfirmCommand(
        preview_revision=1,
        bundle_hash="a" * 64,
        idempotency_key="idempotency-key-1",
        confirmed_by="forged-user",
    )
    principal = V3Principal(
        principal_id="operator-1",
        principal_type="HUMAN",
        scopes=frozenset({V3Scope.V3_WRITE}),
        request_id="request-1",
    )
    bound = bind_v3_principal(command, principal)
    assert bound.confirmed_by == "operator-1"
    assert command.confirmed_by == "forged-user"


def test_actor_identity_is_overwritten_by_authenticated_principal() -> None:
    command = StrategyProposalCreate(
        proposed_strategy_version_id="11111111-1111-1111-1111-111111111111",
        actor_type=ActorType.AI,
        actor_id="forged-agent",
        source_result_id="22222222-2222-2222-2222-222222222222",
        hypothesis="test",
        expected_improvements={"recall": "better"},
        risks=("known",),
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    principal = V3Principal(
        principal_id="operator-1",
        principal_type="HUMAN",
        scopes=frozenset({V3Scope.V3_WRITE}),
        request_id="request-1",
    )
    bound = bind_v3_principal(command, principal)
    assert bound.actor_type is ActorType.HUMAN
    assert bound.actor_id == "operator-1"
    assert command.actor_type is ActorType.AI
    assert command.actor_id == "forged-agent"


def test_created_by_and_corrected_by_are_overwritten_by_principal() -> None:
    """NEW-SEC-002：created_by / corrected_by 不再信任客户端声明。"""
    principal = V3Principal(
        principal_id="operator-1",
        principal_type="HUMAN",
        scopes=frozenset({V3Scope.V3_WRITE}),
        request_id="request-1",
    )
    version = StrategyVersionCreate(
        strategy_code="MOMENTUM",
        version=1,
        configuration={"universe": "all-a"},
        rationale="initial",
        created_by="forged-user",
    )
    correction = DecisionCorrectionCommand(
        old_values={"status": "ACTIVE"},
        new_values={"status": "REVIEWED"},
        reason="fact fix",
        corrected_by="forged-user",
    )
    assert bind_v3_principal(version, principal).created_by == "operator-1"
    assert bind_v3_principal(correction, principal).corrected_by == "operator-1"


def test_pending_adjustment_stays_unconfirmed_but_confirmed_one_gets_principal() -> None:
    principal = V3Principal(
        principal_id="operator-1",
        principal_type="HUMAN",
        scopes=frozenset({V3Scope.V3_WRITE}),
        request_id="request-1",
    )
    common = {
        "account_id": "11111111-1111-1111-1111-111111111111",
        "security_id": "22222222-2222-2222-2222-222222222222",
        "adjustment_type": AdjustmentType.CASH_DIVIDEND,
        "effective_time": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "cash_delta": Decimal("10"),
        "known_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }
    pending = PortfolioAdjustmentCreate(**common)
    confirmed = PortfolioAdjustmentCreate(
        **common, confirmation_status=AdjustmentConfirmation.CONFIRMED
    )
    assert bind_v3_principal(pending, principal).confirmed_by is None
    assert bind_v3_principal(confirmed, principal).confirmed_by == "operator-1"
