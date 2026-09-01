from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from fastapi.responses import JSONResponse


class V3Scope(StrEnum):
    MARKET_READ = "MARKET_READ"
    PORTFOLIO_READ = "PORTFOLIO_READ"
    V3_WRITE = "V3_WRITE"
    STRATEGY_ADMIN = "STRATEGY_ADMIN"


@dataclass(frozen=True, slots=True)
class V3Principal:
    principal_id: str
    principal_type: str
    scopes: frozenset[V3Scope]
    request_id: str


@dataclass(frozen=True, slots=True)
class V3AuthPolicy:
    api_token: str | None
    api_principal_id: str
    strategy_admin_token: str | None
    strategy_admin_principal_id: str
    public_market_read: bool = True

    def required_scope(self, method: str, path: str) -> V3Scope | None:
        if not path.startswith("/api/v3"):
            return None
        normalized_method = method.upper()
        if normalized_method == "OPTIONS":
            return None
        if normalized_method not in {"GET", "HEAD"}:
            if path.startswith("/api/v3/strategies/releases/") and path.endswith(
                ("/activate", "/rollback")
            ):
                return V3Scope.STRATEGY_ADMIN
            return V3Scope.V3_WRITE
        if path == "/api/v3/portfolio" or path.startswith("/api/v3/portfolio/"):
            return V3Scope.PORTFOLIO_READ
        return None if self.public_market_read else V3Scope.MARKET_READ

    def authenticate(self, supplied: str, request_id: str) -> V3Principal | None:
        if self.strategy_admin_token and hmac.compare_digest(
            supplied, f"Bearer {self.strategy_admin_token}"
        ):
            return V3Principal(
                principal_id=self.strategy_admin_principal_id,
                principal_type="HUMAN",
                scopes=frozenset(V3Scope),
                request_id=request_id,
            )
        if self.api_token and hmac.compare_digest(supplied, f"Bearer {self.api_token}"):
            return V3Principal(
                principal_id=self.api_principal_id,
                principal_type="HUMAN",
                scopes=frozenset(
                    {V3Scope.MARKET_READ, V3Scope.PORTFOLIO_READ, V3Scope.V3_WRITE}
                ),
                request_id=request_id,
            )
        return None


class V3AuthMiddleware:
    def __init__(self, app, policy: V3AuthPolicy) -> None:
        self.app = app
        self.policy = policy

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        request_id = headers.get(b"x-request-id", b"").decode("latin-1").strip()
        if not request_id or len(request_id) > 128:
            request_id = uuid4().hex
        required = self.policy.required_scope(
            scope.get("method", "GET"), scope.get("path", "")
        )
        if required is None:
            await self.app(scope, receive, send)
            return

        supplied = headers.get(b"authorization", b"").decode("latin-1")
        principal = self.policy.authenticate(supplied, request_id)
        if principal is None:
            await self._reject(
                scope,
                receive,
                send,
                status_code=401,
                code="V3_UNAUTHORIZED",
                message="missing or invalid bearer token",
                request_id=request_id,
                required_scope=required,
            )
            return
        if required not in principal.scopes:
            await self._reject(
                scope,
                receive,
                send,
                status_code=403,
                code="V3_FORBIDDEN",
                message="authenticated principal lacks required scope",
                request_id=request_id,
                required_scope=required,
            )
            return

        scope.setdefault("state", {})["v3_principal"] = principal
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(
        scope,
        receive,
        send,
        *,
        status_code: int,
        code: str,
        message: str,
        request_id: str,
        required_scope: V3Scope,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content={
                "code": code,
                "message": message,
                "request_id": request_id,
                "details": {"required_scope": required_scope.value},
                "retryable": False,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)
