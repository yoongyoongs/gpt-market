from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import get_settings
from app.container import container
from app.mcp.server import mcp
from app.providers.base import ProviderError
from app.utils.time import now_shanghai

settings = get_settings()
mcp_app = mcp.http_app(path="/")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await container.start()
    async with mcp_app.lifespan(mcp_app):
        yield
    await container.close()


api = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
api.include_router(router)
api.mount("/mcp", mcp_app)


@api.exception_handler(ValueError)
async def validation_error(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"ok": False, "error": str(exc), "source": "eastmoney", "server_timestamp": now_shanghai().isoformat()})


@api.exception_handler(ProviderError)
async def provider_error(_: Request, exc: ProviderError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"ok": False, "error": str(exc), "source": "eastmoney", "server_timestamp": now_shanghai().isoformat()})


@api.exception_handler(RequestValidationError)
async def request_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"ok": False, "error": str(exc), "source": "eastmoney", "server_timestamp": now_shanghai().isoformat()})


@api.exception_handler(Exception)
async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"ok": False, "error": f"internal error: {type(exc).__name__}", "source": "eastmoney", "server_timestamp": now_shanghai().isoformat()})


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
