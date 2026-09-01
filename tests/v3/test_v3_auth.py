from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.v3.security import V3AuthMiddleware, V3AuthPolicy


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
