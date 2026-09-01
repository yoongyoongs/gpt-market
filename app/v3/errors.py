"""V3 统一 Error Envelope（RC-08C / API-004）。

整改方案 §11.4：/api/v3 路径所有错误统一返回

    {"code", "message", "request_id", "details", "retryable"}

映射：Validation(400/422)、Not Found(404)、Conflict(409)、
Provider unavailable(503, retryable)、Unauthorized/Forbidden(由
V3AuthMiddleware 直接产出同构 envelope)、Internal error(500)。
内部错误绝不标注 source=eastmoney 或伪造上游来源；V2 路径保持旧格式。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.utils.time import now_shanghai
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_503_SERVICE_UNAVAILABLE,
    HTTP_422_UNPROCESSABLE_ENTITY,
)

from app.providers.base import ProviderError
from app.v3.repositories.errors import (
    RepositoryConflictError,
    RepositoryNotFoundError,
)


def is_v3_path(request: Request) -> bool:
    return request.url.path.startswith("/api/v3")


def _request_id(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "")
    return supplied[:128] if supplied else ""


def _envelope(
    request: Request, *, status_code: int, code: str, message: str,
    details: dict | None = None, retryable: bool = False,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "request_id": _request_id(request),
            "details": details or {},
            "retryable": retryable,
        },
    )


def register_v3_error_envelope(api: FastAPI) -> None:
    @api.exception_handler(RepositoryNotFoundError)
    async def _not_found(request: Request, exc: RepositoryNotFoundError):
        if not is_v3_path(request):
            return None
        return _envelope(
            request, status_code=HTTP_404_NOT_FOUND, code="V3_NOT_FOUND",
            message=str(exc) or "resource not found",
        )

    @api.exception_handler(RepositoryConflictError)
    async def _conflict(request: Request, exc: RepositoryConflictError):
        if not is_v3_path(request):
            return None
        return _envelope(
            request, status_code=HTTP_409_CONFLICT, code="V3_CONFLICT",
            message=str(exc) or "conflict",
        )

    @api.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException):
        if not is_v3_path(request):
            # 覆盖了 FastAPI 默认 handler，legacy 路径必须自己补回默认行为，
            # 返回 None 会变成 500
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=getattr(exc, "headers", None),
            )
        code = {
            404: "V3_NOT_FOUND", 401: "V3_UNAUTHORIZED", 403: "V3_FORBIDDEN",
            409: "V3_CONFLICT", 503: "V3_UNAVAILABLE",
        }.get(exc.status_code, "V3_ERROR")
        return _envelope(
            request, status_code=exc.status_code, code=code,
            message=str(exc.detail), details={},
        )



def _legacy(request: Request, *, status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "error": message, "source": "eastmoney",
                 "server_timestamp": now_shanghai().isoformat()},
    )


def value_error_response(request: Request, exc: ValueError) -> JSONResponse:
    if is_v3_path(request):
        return _envelope(
            request, status_code=HTTP_400_BAD_REQUEST, code="V3_VALIDATION",
            message=str(exc) or "invalid request",
        )
    return _legacy(request, status_code=HTTP_400_BAD_REQUEST, message=str(exc))


def validation_error_response(
    request: Request, exc: RequestValidationError,
) -> JSONResponse:
    if is_v3_path(request):
        return _envelope(
            request, status_code=HTTP_422_UNPROCESSABLE_ENTITY,
            code="V3_VALIDATION", message="request validation failed",
            details={"errors": exc.errors()},
        )
    return _legacy(request, status_code=HTTP_422_UNPROCESSABLE_ENTITY,
                   message=str(exc))


def provider_error_response(request: Request, exc: ProviderError) -> JSONResponse:
    if is_v3_path(request):
        return _envelope(
            request, status_code=HTTP_503_SERVICE_UNAVAILABLE,
            code="V3_PROVIDER_UNAVAILABLE",
            message=str(exc) or "provider unavailable", retryable=True,
        )
    return _legacy(request, status_code=HTTP_503_SERVICE_UNAVAILABLE, message=str(exc))


def internal_error_response(request: Request, exc: Exception) -> JSONResponse:
    if is_v3_path(request):
        return _envelope(
            request, status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            code="V3_INTERNAL", message=f"internal error: {type(exc).__name__}",
        )
    return _legacy(
        request, status_code=HTTP_500_INTERNAL_SERVER_ERROR,
        message=f"internal error: {type(exc).__name__}",
    )
