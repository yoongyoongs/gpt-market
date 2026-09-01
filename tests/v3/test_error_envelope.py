"""RC-08C V3 Error Envelope 测试（API-004）。

整改方案 §11.4：/api/v3 路径所有错误返回统一 envelope
{code, message, request_id, details, retryable}；
内部错误绝不标注 source=eastmoney；V2 路径行为不变。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import api
from app.providers.base import ProviderError
from app.v3.repositories.errors import (
    RepositoryConflictError,
    RepositoryNotFoundError,
)


def _client() -> TestClient:
    return TestClient(api, raise_server_exceptions=False)


@api.get("/api/v3/_test/not-found")
async def _not_found():
    raise RepositoryNotFoundError("regression case not found")


@api.get("/api/v3/_test/conflict")
async def _conflict():
    raise RepositoryConflictError("release state changed")


@api.get("/api/v3/_test/value")
async def _value():
    raise ValueError("bad point in time")


@api.get("/api/v3/_test/provider")
async def _provider():
    raise ProviderError("eastmoney timeout")


@api.get("/api/v3/_test/internal")
async def _internal():
    raise ZeroDivisionError("boom")


@api.get("/_test/legacy")
async def _legacy():
    raise ValueError("legacy path")


@pytest.mark.parametrize("path,status,code,retryable", [
    ("/api/v3/_test/not-found", 404, "V3_NOT_FOUND", False),
    ("/api/v3/_test/conflict", 409, "V3_CONFLICT", False),
    ("/api/v3/_test/value", 400, "V3_VALIDATION", False),
    ("/api/v3/_test/provider", 503, "V3_PROVIDER_UNAVAILABLE", True),
    ("/api/v3/_test/internal", 500, "V3_INTERNAL", False),
])
def test_v3_errors_use_unified_envelope(path, status, code, retryable) -> None:
    response = _client().get(path, headers={"X-Request-ID": "req-77"})
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    assert body["message"]
    assert body["request_id"] == "req-77"
    assert body["retryable"] is retryable
    # 内部错误绝不伪造上游来源
    assert "source" not in body
    assert "eastmoney" not in body["message"].lower() or path == "/api/v3/_test/provider"


def test_v2_paths_keep_legacy_error_shape() -> None:
    response = _client().get("/_test/legacy")
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert "code" not in body


def test_v3_validation_error_from_pydantic_body() -> None:
    client = _client()
    response = client.get("/api/v3/watchlist/changes", params={"limit": "abc"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "V3_VALIDATION"
    assert body["details"]
    assert "source" not in body


async def test_unknown_v3_route_returns_envelope_404() -> None:
    """路由级 404（starlette HTTPException 父类）也必须走 V3 envelope。"""
    from httpx import ASGITransport, AsyncClient

    from app.main import api

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v3/definitely-not-a-route")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "V3_NOT_FOUND"
    assert "detail" not in body
