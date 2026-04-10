"""/healthz endpoint (ADR 002)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_healthz_returns_ok(api_client: AsyncClient) -> None:
    resp = await api_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
