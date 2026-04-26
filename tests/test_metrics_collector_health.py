from __future__ import annotations

import asyncio
from typing import Any

import pytest

from feedgate_fetcher import metrics


@pytest.mark.asyncio
async def test_run_collector_sets_last_success_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    metrics.METRICS_COLLECTOR_LAST_SUCCESS_UNIXTIME.set(0)

    async def collect_success(*_args: Any, **_kwargs: Any) -> None:
        stop.set()

    monkeypatch.setattr(metrics, "_collect_state", collect_success)

    await metrics.run_collector(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        interval_seconds=60,
        stop_event=stop,
    )

    assert metrics.METRICS_COLLECTOR_LAST_SUCCESS_UNIXTIME._value.get() > 0


@pytest.mark.asyncio
async def test_run_collector_increments_errors_without_touching_last_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    before_errors = metrics.METRICS_COLLECTOR_ERRORS_TOTAL._value.get()
    metrics.METRICS_COLLECTOR_LAST_SUCCESS_UNIXTIME.set(123)

    async def collect_failure(*_args: Any, **_kwargs: Any) -> None:
        stop.set()
        raise RuntimeError("collector failure")

    monkeypatch.setattr(metrics, "_collect_state", collect_failure)

    await metrics.run_collector(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        interval_seconds=60,
        stop_event=stop,
    )

    assert metrics.METRICS_COLLECTOR_ERRORS_TOTAL._value.get() - before_errors == 1
    assert metrics.METRICS_COLLECTOR_LAST_SUCCESS_UNIXTIME._value.get() == 123
