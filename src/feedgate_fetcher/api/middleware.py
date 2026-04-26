"""FastAPI middleware registration."""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

from feedgate_fetcher.metrics import API_DURATION, API_REQUESTS_TOTAL


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next: Any) -> Any:
        if request.url.path == "/metrics":
            return await call_next(request)
        t0 = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - t0
        route = request.scope.get("route")
        path = route.path if route else request.url.path

        API_REQUESTS_TOTAL.labels(
            method=request.method,
            path=path,
            status_code=str(response.status_code),
        ).inc()
        API_DURATION.labels(method=request.method, path=path).observe(duration)
        return response


def add_metrics_middleware(app: FastAPI) -> None:
    app.add_middleware(MetricsMiddleware)
