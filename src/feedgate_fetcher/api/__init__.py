"""FastAPI HTTP API.

Router registration and the per-request DB session dependency live
here. The router modules themselves (feeds, entries) define the actual
endpoint handlers. The health endpoint is defined inline below.

The session factory is read from ``app.state.session_factory``, which
callers (``main.py`` lifespan for production, the test fixtures for
pytest) must set before the app handles any request.
"""

from __future__ import annotations

import hmac
import time
from collections.abc import AsyncIterator
from http import HTTPStatus
from typing import Any, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import Response
from starlette.types import ExceptionHandler

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

_health_router = APIRouter(tags=["health"])


@_health_router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Session / auth dependencies
# ---------------------------------------------------------------------------


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a request-scoped AsyncSession."""
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def require_api_key(request: Request) -> None:
    """FastAPI dependency that enforces X-Api-Key authentication.

    When ``app.state.api_key`` is empty (the default) auth is disabled
    and every request passes through. When set, the ``x-api-key``
    header must match exactly or the request is rejected with 401.
    """
    configured_key: str = getattr(request.app.state, "api_key", "")
    if not configured_key:
        return  # auth disabled
    provided_key = request.headers.get("x-api-key", "")
    if not provided_key or not hmac.compare_digest(provided_key, configured_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_api_key",
        )


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"


def _status_title(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _problem(
    *,
    request: Request,
    status_code: int,
    title: str,
    detail: str,
) -> dict[str, str | int]:
    return {
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": request.url.path,
    }


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    title = _status_title(exc.status_code)
    detail = title if exc.detail is None else str(exc.detail)
    problem = _problem(
        request=request,
        status_code=exc.status_code,
        title=title,
        detail=detail,
    )
    return JSONResponse(
        problem,
        status_code=exc.status_code,
        media_type=PROBLEM_JSON_MEDIA_TYPE,
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = first.get("loc", ())
        msg = first.get("msg", "validation error")
        detail = f"{loc}: {msg}"
    else:
        detail = "validation error"

    status_code = 422
    title = "Unprocessable Entity"
    problem = _problem(
        request=request,
        status_code=status_code,
        title=title,
        detail=detail,
    )
    return JSONResponse(
        problem,
        status_code=status_code,
        media_type=PROBLEM_JSON_MEDIA_TYPE,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(
        HTTPException,
        cast(ExceptionHandler, http_exception_handler),
    )
    app.add_exception_handler(
        RequestValidationError,
        cast(ExceptionHandler, validation_exception_handler),
    )


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------


def _add_metrics_middleware(app: FastAPI) -> None:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request as StarletteRequest

    class MetricsMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: StarletteRequest, call_next: Any) -> Any:
            if request.url.path == "/metrics":
                return await call_next(request)
            t0 = time.perf_counter()
            response = await call_next(request)
            duration = time.perf_counter() - t0
            route = request.scope.get("route")
            path = route.path if route else request.url.path
            from feedgate_fetcher.metrics import API_DURATION, API_REQUESTS_TOTAL
            API_REQUESTS_TOTAL.labels(
                method=request.method,
                path=path,
                status_code=str(response.status_code),
            ).inc()
            API_DURATION.labels(method=request.method, path=path).observe(duration)
            return response

    app.add_middleware(MetricsMiddleware)


def register_routers(app: FastAPI) -> None:
    """Mount all routers onto ``app``."""
    from feedgate_fetcher.api import entries, feeds

    _add_metrics_middleware(app)

    @app.get("/metrics", include_in_schema=False, dependencies=[Depends(require_api_key)])
    async def metrics_endpoint() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(_health_router)
    app.include_router(feeds.router, dependencies=[Depends(require_api_key)])
    app.include_router(entries.router, dependencies=[Depends(require_api_key)])
