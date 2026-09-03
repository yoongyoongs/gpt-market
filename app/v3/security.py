from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any
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


def bind_v3_principal(command: Any, principal: V3Principal):
    """Replace caller-declared identity fields with the authenticated principal.

    NEW-SEC-002：actor_id/actor_type/confirmed_by/created_by/corrected_by
    全部以服务端 principal 为唯一可信来源，客户端声明一律覆盖。
    """
    model_fields = getattr(type(command), "model_fields", {})
    updates: dict[str, Any] = {}
    if "actor_id" in model_fields:
        updates["actor_id"] = principal.principal_id
    if "actor_type" in model_fields:
        current = getattr(command, "actor_type")
        updates["actor_type"] = (
            type(current)(principal.principal_type)
            if isinstance(current, Enum)
            else principal.principal_type
        )
    if "confirmed_by" in model_fields:
        confirmed_by = getattr(command, "confirmed_by")
        confirmation_status = getattr(command, "confirmation_status", None)
        status_value = getattr(confirmation_status, "value", confirmation_status)
        if confirmed_by is not None or status_value == "CONFIRMED":
            updates["confirmed_by"] = principal.principal_id
    if "created_by" in model_fields:
        updates["created_by"] = principal.principal_id
    if "corrected_by" in model_fields:
        updates["corrected_by"] = principal.principal_id
    return command.model_copy(update=updates) if updates else command


# NEW-SEC-001：公开 READ 不再是“除 /portfolio 外全公开”，而是明确的
# Public Market READ Allowlist —— 只允许纯市场事实匿名读取。
# Watchlist/Decision/EntryPlan/Task/Strategy/Release/Performance/Context 决策面
# 等 GET 一律需要认证（有效 token 全部持有 MARKET_READ）。
PUBLIC_MARKET_READ_EXACT: frozenset[str] = frozenset(
    {
        "/universe/features",
        "/universe/query",  # include_in_schema=False 的别名
        "/market-regime",
        "/market-overview",
        "/candidates/comparison-pack",
        "/recalls",
        "/recalls/misses",
        "/raw-opportunities",
        "/market-reviews",
        "/market/intraday-status",
        "/health/data-quality",
    }
)
PUBLIC_MARKET_READ_PREFIXES: tuple[str, ...] = (
    "/evidence/",  # /evidence/{subject_type}/{subject_id}
    "/context-packs/",  # /context-packs/{context_pack_id}
)
PUBLIC_MARKET_READ_TEMPLATES: tuple[tuple[str, ...], ...] = (
    ("stocks", "*", "evidence"),
    ("stocks", "*", "context-pack"),
)


def is_public_market_read(path: str) -> bool:
    """path 为去掉 /api/v3 前缀后的路由（如 /universe/features）。"""
    if path in PUBLIC_MARKET_READ_EXACT:
        return True
    if path.startswith(PUBLIC_MARKET_READ_PREFIXES):
        return True
    segments = tuple(segment for segment in path.split("/") if segment)
    for template in PUBLIC_MARKET_READ_TEMPLATES:
        if len(segments) == len(template) and all(
            part == "*" or part == segment
            for part, segment in zip(template, segments)
        ):
            return True
    return False


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
        if path == "/api/v3/portfolio-preferences":
            # 别名与 /portfolio/preferences 同源，不得绕开 PORTFOLIO_READ
            return V3Scope.PORTFOLIO_READ
        relative = path[len("/api/v3"):]
        if self.public_market_read and is_public_market_read(relative):
            return None
        return V3Scope.MARKET_READ

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
