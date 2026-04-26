"""FastAPI router registration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from feedgate_fetcher.api.dependencies import require_api_key
from feedgate_fetcher.api.middleware import add_metrics_middleware

_health_router = APIRouter(tags=["health"])


@_health_router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def register_routers(app: FastAPI) -> None:
    """Mount all routers onto ``app``."""
    from feedgate_fetcher.api import entries, feeds

    add_metrics_middleware(app)

    @app.get("/metrics", include_in_schema=False, dependencies=[Depends(require_api_key)])
    async def metrics_endpoint() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(_health_router)
    app.include_router(feeds.router, dependencies=[Depends(require_api_key)])
    app.include_router(entries.router, dependencies=[Depends(require_api_key)])
