from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from feedgate.api import PROBLEM_JSON_MEDIA_TYPE, register_exception_handlers


@pytest.fixture(autouse=True)
def _register_problem_handlers(api_app: FastAPI) -> None:
    register_exception_handlers(api_app)


@pytest.mark.asyncio
async def test_blocked_url_returns_problem_details(api_client: AsyncClient) -> None:
    resp = await api_client.post("/v1/feeds", json={"url": "http://10.0.0.1/feed"})
    body = resp.json()

    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert {"type", "title", "status", "detail", "instance"}.issubset(body.keys())
    assert body["status"] == 400
    assert body["type"] == "about:blank"
    assert body["title"] == "Bad Request"
    assert "blocked_url" in body["detail"]
    assert body["instance"] == "/v1/feeds"


@pytest.mark.asyncio
async def test_feed_not_found_returns_problem_details(api_client: AsyncClient) -> None:
    resp = await api_client.get("/v1/feeds/99999")
    body = resp.json()

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert body["status"] == 404
    assert body["title"] == "Not Found"
    assert body["detail"] == "feed not found"
    assert body["instance"] == "/v1/feeds/99999"


@pytest.mark.asyncio
async def test_invalid_cursor_on_entries_returns_problem_details(api_client: AsyncClient) -> None:
    resp = await api_client.get("/v1/entries?feed_ids=1&cursor=!!!bogus!!!")
    body = resp.json()

    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert body["detail"] == "invalid cursor"


@pytest.mark.asyncio
async def test_validation_error_returns_problem_details(api_client: AsyncClient) -> None:
    resp = await api_client.post("/v1/feeds", json={"url": ""})
    body = resp.json()

    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith(PROBLEM_JSON_MEDIA_TYPE)
    assert body["title"] == "Unprocessable Entity"
    assert body["status"] == 422
    assert "url" in body["detail"]
