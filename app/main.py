from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import live_cache, router, v2_dashboard_cache
from app.api.v3 import router as v3_router
from app.api.v3_dashboard import router as v3_dashboard_router
from app.config import get_settings
from app.container import container
from app.mcp.server import mcp
from app.providers.base import ProviderError
from app.utils.time import now_shanghai
from app.v3.security import V3AuthMiddleware, V3AuthPolicy
from app.v3 import errors as v3_errors
from app.v3.errors import register_v3_error_envelope

settings = get_settings()
mcp_app = mcp.http_app(path="/")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await container.start()
    async with mcp_app.lifespan(mcp_app):
        await live_cache.start()
        await v2_dashboard_cache.start()
        try:
            yield
        finally:
            await v2_dashboard_cache.stop()
            await live_cache.stop()
            await container.close()


api = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
api.add_middleware(
    V3AuthMiddleware,
    policy=V3AuthPolicy(
        api_token=settings.v3_api_token,
        api_principal_id=settings.v3_api_principal_id,
        strategy_admin_token=settings.v3_strategy_admin_token,
        strategy_admin_principal_id=settings.v3_strategy_admin_principal_id,
        public_market_read=settings.v3_public_market_read,
    ),
)
api.include_router(router)
api.include_router(v3_router)
api.include_router(v3_dashboard_router)
api.mount("/mcp", mcp_app)


@api.exception_handler(ValueError)
async def validation_error(request: Request, exc: ValueError) -> JSONResponse:
    return v3_errors.value_error_response(request, exc)


@api.exception_handler(ProviderError)
async def provider_error(request: Request, exc: ProviderError) -> JSONResponse:
    return v3_errors.provider_error_response(request, exc)


@api.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return v3_errors.validation_error_response(request, exc)


@api.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    return v3_errors.internal_error_response(request, exc)


register_v3_error_envelope(api)


class BearerAuthMiddleware:
    """Small independent auth boundary; leave MCP_TOKEN empty for local unauthenticated testing."""

    def __init__(self, wrapped, token: str | None) -> None:
        self.wrapped = wrapped
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp") and self.token:
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            supplied = headers.get(b"authorization", b"").decode("latin-1")
            expected = f"Bearer {self.token}"
            if not hmac.compare_digest(supplied, expected):
                response = JSONResponse(
                    status_code=401,
                    content={"ok": False, "error": "missing or invalid bearer token", "server_timestamp": now_shanghai().isoformat()},
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.wrapped(scope, receive, send)


app = BearerAuthMiddleware(api, settings.mcp_token)
