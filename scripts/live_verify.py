#!/usr/bin/env python3
"""Live verification battery for feedgate-fetcher.

Runs one end-to-end check of the running service plus direct DB
invariants. Writes a structured JSON report to
``var/verify-runs/<timestamp>.json`` and prints a one-line summary
on stdout.

Intended to be called every 10 minutes from a bash loop (see
``scripts/live_verify_loop.sh``). Fully stateless — each invocation
is self-contained so a prior failed run does not poison the next
one.

Exit code:
  0 — every check passed
  1 — one or more checks failed (report file still written)
  2 — verifier itself crashed before finishing (unreachable API,
      DB down, etc.)

Environment variables:
  FEEDGATE_API_BASE     — base URL of the running uvicorn
                          (default: http://127.0.0.1:8765)
  FEEDGATE_VERIFY_DSN   — asyncpg-compatible DSN for direct DB peek
                          (default: postgresql://postgres:postgres@localhost:55432/feedgate)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx

API_BASE = os.environ.get("FEEDGATE_API_BASE", "http://127.0.0.1:8765")
DB_DSN = os.environ.get(
    "FEEDGATE_VERIFY_DSN",
    "postgresql://postgres:postgres@localhost:55432/feedgate",
)
REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_DIR = REPO_ROOT / "var" / "verify-runs"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunReport:
    timestamp: str
    api_base: str
    duration_ms: int
    checks: list[CheckResult] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


# --- HTTP API checks --------------------------------------------------------


async def check_healthz(client: httpx.AsyncClient) -> CheckResult:
    try:
        r = await client.get("/healthz", timeout=5)
    except Exception as e:
        return CheckResult("healthz", False, f"exception: {e!r}")
    body = {}
    try:
        body = r.json()
    except Exception:
        pass
    ok = r.status_code == 200 and body.get("status") == "ok"
    return CheckResult("healthz", ok, f"HTTP {r.status_code}")


async def check_feeds_list(
    client: httpx.AsyncClient,
) -> tuple[CheckResult, list[dict[str, Any]]]:
    try:
        r = await client.get("/v1/feeds", params={"limit": 200}, timeout=10)
    except Exception as e:
        return CheckResult("feeds_list", False, f"exception: {e!r}"), []
    if r.status_code != 200:
        return CheckResult("feeds_list", False, f"HTTP {r.status_code}"), []
    items = r.json().get("items", [])
    return (
        CheckResult("feeds_list", True, f"{len(items)} feeds", {"n": len(items)}),
        items,
    )


async def check_feed_get(client: httpx.AsyncClient, feed_id: int) -> CheckResult:
    try:
        r = await client.get(f"/v1/feeds/{feed_id}", timeout=5)
    except Exception as e:
        return CheckResult(f"feed_get_{feed_id}", False, f"exception: {e!r}")
    if r.status_code != 200:
        return CheckResult(f"feed_get_{feed_id}", False, f"HTTP {r.status_code}")
    body = r.json()
    required = {
        "id",
        "url",
        "effective_url",
        "title",
        "status",
        "last_successful_fetch_at",
        "last_attempt_at",
        "last_error_code",
        "created_at",
    }
    missing = required - set(body)
    ok = not missing
    return CheckResult(
        f"feed_get_{feed_id}",
        ok,
        "OK" if ok else f"missing fields: {sorted(missing)}",
    )


async def check_missing_feed_404(client: httpx.AsyncClient) -> CheckResult:
    try:
        r = await client.get("/v1/feeds/9999999", timeout=5)
    except Exception as e:
        return CheckResult("feed_missing_404", False, f"exception: {e!r}")
    return CheckResult(
        "feed_missing_404", r.status_code == 404, f"HTTP {r.status_code}"
    )


async def check_idempotent_post(client: httpx.AsyncClient, url: str) -> CheckResult:
    try:
        r = await client.post("/v1/feeds", json={"url": url}, timeout=10)
    except Exception as e:
        return CheckResult("idempotent_post", False, f"exception: {e!r}")
    return CheckResult(
        "idempotent_post",
        r.status_code == 200,
        f"HTTP {r.status_code} (expected 200 for duplicate)",
    )


async def check_entries_feed_ids_required(
    client: httpx.AsyncClient,
) -> CheckResult:
    try:
        r = await client.get("/v1/entries", timeout=5)
    except Exception as e:
        return CheckResult("entries_feed_ids_required", False, f"exception: {e!r}")
    return CheckResult(
        "entries_feed_ids_required",
        r.status_code == 422,
        f"HTTP {r.status_code}",
    )


async def check_entries_invalid_cursor(
    client: httpx.AsyncClient,
) -> CheckResult:
    try:
        r = await client.get(
            "/v1/entries",
            params={"feed_ids": "1", "cursor": "!!!bogus!!!"},
            timeout=5,
        )
    except Exception as e:
        return CheckResult("entries_invalid_cursor_400", False, f"exception: {e!r}")
    return CheckResult(
        "entries_invalid_cursor_400",
        r.status_code == 400,
        f"HTTP {r.status_code}",
    )


async def check_entries_aggregate(
    client: httpx.AsyncClient, feed_ids: list[int]
) -> tuple[CheckResult, int]:
    if not feed_ids:
        return CheckResult("entries_aggregate", False, "no feeds"), 0
    ids_csv = ",".join(str(i) for i in feed_ids)
    try:
        r = await client.get(
            "/v1/entries",
            params={"feed_ids": ids_csv, "limit": 200},
            timeout=10,
        )
    except Exception as e:
        return CheckResult("entries_aggregate", False, f"exception: {e!r}"), 0
    if r.status_code != 200:
        return CheckResult("entries_aggregate", False, f"HTTP {r.status_code}"), 0
    items = r.json().get("items", [])
    return CheckResult("entries_aggregate", True, f"{len(items)} entries"), len(items)


async def check_cursor_walk(
    client: httpx.AsyncClient, feed_id: int
) -> CheckResult:
    """Page through one feed with limit=2, assert uniqueness and monotonicity."""
    seen: set[str] = set()
    cursor: str | None = None
    pages = 0
    prev_published: str | None = None
    while pages < 5:
        params: dict[str, str] = {"feed_ids": str(feed_id), "limit": "2"}
        if cursor is not None:
            params["cursor"] = cursor
        try:
            r = await client.get("/v1/entries", params=params, timeout=10)
        except Exception as e:
            return CheckResult(
                "cursor_walk", False, f"exception on page {pages}: {e!r}"
            )
        if r.status_code != 200:
            return CheckResult(
                "cursor_walk", False, f"HTTP {r.status_code} on page {pages}"
            )
        body = r.json()
        for e in body.get("items", []):
            if e["guid"] in seen:
                return CheckResult(
                    "cursor_walk", False, f"duplicate guid {e['guid']}"
                )
            seen.add(e["guid"])
            cur_published = e.get("published_at")
            if (
                prev_published is not None
                and cur_published is not None
                and cur_published > prev_published
            ):
                return CheckResult(
                    "cursor_walk",
                    False,
                    f"sort order broken: {cur_published} > {prev_published}",
                )
            prev_published = cur_published
        cursor = body.get("next_cursor")
        pages += 1
        if not cursor:
            break
    return CheckResult(
        "cursor_walk", True, f"{pages} pages, {len(seen)} unique entries"
    )


# --- DB invariant checks ----------------------------------------------------


async def check_db_invariants() -> list[CheckResult]:
    results: list[CheckResult] = []
    conn = await asyncpg.connect(DB_DSN)
    try:
        n_feeds = await conn.fetchval("SELECT COUNT(*) FROM feeds")
        n_entries = await conn.fetchval("SELECT COUNT(*) FROM entries")

        ts_violations = await conn.fetchval(
            "SELECT COUNT(*) FROM entries WHERE content_updated_at < fetched_at"
        )
        orphans = await conn.fetchval(
            "SELECT COUNT(*) FROM entries e "
            "LEFT JOIN feeds f ON f.id = e.feed_id "
            "WHERE f.id IS NULL"
        )
        dup_rows = await conn.fetch(
            "SELECT feed_id, guid, COUNT(*) AS n FROM entries "
            "GROUP BY feed_id, guid HAVING COUNT(*) > 1 LIMIT 5"
        )
        status_hist_rows = await conn.fetch(
            "SELECT status, COUNT(*) AS n FROM feeds GROUP BY status"
        )
        status_hist = {r["status"]: r["n"] for r in status_hist_rows}
        error_hist_rows = await conn.fetch(
            "SELECT last_error_code, COUNT(*) AS n FROM feeds "
            "WHERE last_error_code IS NOT NULL GROUP BY last_error_code "
            "ORDER BY n DESC"
        )
        error_hist = {r["last_error_code"]: r["n"] for r in error_hist_rows}
        with_success = await conn.fetchval(
            "SELECT COUNT(*) FROM feeds WHERE last_successful_fetch_at IS NOT NULL"
        )
        without_success = await conn.fetchval(
            "SELECT COUNT(*) FROM feeds WHERE last_successful_fetch_at IS NULL"
        )
        cf_rows = await conn.fetch(
            "SELECT consecutive_failures, COUNT(*) AS n FROM feeds "
            "GROUP BY consecutive_failures ORDER BY consecutive_failures DESC"
        )
        cf_hist = {str(r["consecutive_failures"]): r["n"] for r in cf_rows}
    finally:
        await conn.close()

    results.append(
        CheckResult(
            "db_timestamp_invariant",
            ts_violations == 0,
            f"{ts_violations} rows with content_updated_at < fetched_at",
        )
    )
    results.append(
        CheckResult(
            "db_no_orphan_entries",
            orphans == 0,
            f"{orphans} orphan entries",
        )
    )
    results.append(
        CheckResult(
            "db_no_duplicate_guids",
            len(dup_rows) == 0,
            f"{len(dup_rows)} duplicate (feed_id, guid) groups",
            {"samples": [dict(r) for r in dup_rows]},
        )
    )
    results.append(
        CheckResult(
            "db_summary",
            True,  # informational
            f"{n_feeds} feeds, {n_entries} entries",
            {
                "feeds_total": n_feeds,
                "entries_total": n_entries,
                "status_histogram": status_hist,
                "error_histogram": error_hist,
                "with_success": with_success,
                "without_success": without_success,
                "consecutive_failures_histogram": cf_hist,
            },
        )
    )
    return results


# --- Orchestration ----------------------------------------------------------


async def run_report() -> RunReport:
    t0 = time.monotonic()
    ts = (
        dt.datetime.now(dt.UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    report = RunReport(timestamp=ts, api_base=API_BASE, duration_ms=0)

    async with httpx.AsyncClient(base_url=API_BASE) as client:
        health = await check_healthz(client)
        report.checks.append(health)
        if not health.ok:
            report.duration_ms = int((time.monotonic() - t0) * 1000)
            return report

        feeds_check, feeds = await check_feeds_list(client)
        report.checks.append(feeds_check)
        feed_ids = [f["id"] for f in feeds]

        for fid in feed_ids[:5]:
            report.checks.append(await check_feed_get(client, fid))

        report.checks.append(await check_missing_feed_404(client))
        if feeds:
            report.checks.append(await check_idempotent_post(client, feeds[0]["url"]))
        report.checks.append(await check_entries_feed_ids_required(client))
        report.checks.append(await check_entries_invalid_cursor(client))

        agg, entries_visible = await check_entries_aggregate(client, feed_ids)
        report.checks.append(agg)
        report.stats["entries_visible_via_api"] = entries_visible

        # Cursor walk on the first feed that actually has entries
        for fid in feed_ids:
            try:
                r = await client.get(
                    "/v1/entries",
                    params={"feed_ids": str(fid), "limit": 1},
                    timeout=5,
                )
                if r.status_code == 200 and r.json().get("items"):
                    report.checks.append(await check_cursor_walk(client, fid))
                    break
            except Exception:
                continue

    try:
        report.checks.extend(await check_db_invariants())
    except Exception as e:
        report.checks.append(CheckResult("db_invariants", False, f"exception: {e!r}"))

    report.stats["feeds_total"] = len(feed_ids)
    report.stats["feeds_with_success"] = sum(
        1 for f in feeds if f.get("last_successful_fetch_at")
    )
    report.stats["feeds_with_error"] = sum(
        1 for f in feeds if f.get("last_error_code")
    )

    report.duration_ms = int((time.monotonic() - t0) * 1000)
    return report


def write_report(report: RunReport) -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    ts_slug = report.timestamp.replace(":", "").replace("-", "")
    out = RUN_DIR / f"{ts_slug}.json"
    data = asdict(report)
    data["ok"] = report.ok
    with out.open("w") as f:
        json.dump(data, f, indent=2, default=str)
    return out


def print_summary(report: RunReport, path: Path) -> None:
    passed = sum(1 for c in report.checks if c.ok)
    total = len(report.checks)
    status_char = "OK" if report.ok else "FAIL"
    stats = report.stats
    print(
        f"[{report.timestamp}] {status_char} {passed}/{total} "
        f"feeds={stats.get('feeds_total', 0)} "
        f"with_success={stats.get('feeds_with_success', 0)} "
        f"with_error={stats.get('feeds_with_error', 0)} "
        f"entries_api={stats.get('entries_visible_via_api', 0)} "
        f"{report.duration_ms}ms -> {path.name}"
    )
    if not report.ok:
        for c in report.checks:
            if not c.ok:
                print(f"    FAIL {c.name}: {c.detail}")


async def main_async() -> int:
    try:
        report = await run_report()
    except Exception:
        print("verifier crashed:", file=sys.stderr)
        traceback.print_exc()
        return 2
    path = write_report(report)
    print_summary(report, path)
    return 0 if report.ok else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
