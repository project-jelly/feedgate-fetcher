#!/usr/bin/env python3
"""Register every seed feed from the catalog into the running service.

Idempotent: reposting a URL that is already registered returns HTTP
200 and the existing row (ADR 002). Exits 0 if every request
completed, 1 if any request failed (HTTP 5xx, 4xx other than 200,
or network error).

The catalog is hard-coded here so this script has zero dependencies
outside the stdlib and can run in any Python 3.11+ interpreter.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = os.environ.get("FEEDGATE_API_BASE", "http://127.0.0.1:8765")

# (category, url). Kept in the order the user's catalog specified so the
# POST order is deterministic and the DB layout is reproducible.
SEED_FEEDS: list[tuple[str, str]] = [
    ("ai-research", "https://huggingface.co/blog/feed.xml"),
    ("ai-research", "https://openai.com/news/rss.xml"),
    ("ai-research", "https://deepmind.google/blog/rss.xml"),
    ("ai-research", "https://engineering.fb.com/feed/"),
    ("ai-infra", "https://developer.nvidia.com/blog/feed"),
    ("ai-infra", "https://pytorch.org/blog/feed.xml"),
    ("eng-blogs", "https://netflixtechblog.com/feed"),
    ("eng-blogs", "https://www.uber.com/blog/engineering/rss/"),
    ("eng-blogs", "https://blog.cloudflare.com/rss/"),
    ("eng-blogs", "https://stripe.com/blog/feed.rss"),
    ("eng-blogs", "https://tailscale.com/blog/index.xml"),
    ("eng-blogs", "https://dropbox.tech/feed"),
    ("eng-blogs", "https://fly.io/blog/feed.xml"),
    ("k8s-cloudnative", "https://www.cncf.io/feed/"),
    ("k8s-cloudnative", "https://kubernetes.io/feed.xml"),
    ("lowlevel", "https://simonwillison.net/atom/everything/"),
    ("lowlevel", "https://lwn.net/headlines/newrss"),
    ("hardware", "https://blogs.nvidia.com/feed/"),
    ("hardware", "https://www.nextplatform.com/feed/"),
    ("hardware", "https://chipsandcheese.com/feed/"),
    ("hardware", "https://nvidianews.nvidia.com/releases.xml"),
    ("misc", "https://blog.python.org/feeds/posts/default"),
]


def post_feed(url: str) -> tuple[int, object]:
    req = urllib.request.Request(
        f"{API_BASE}/v1/feeds",
        data=json.dumps({"url": url}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except urllib.error.URLError as e:
        return 0, f"network error: {e}"


def main() -> int:
    created = 0
    existing = 0
    failed: list[tuple[str, str, int, object]] = []
    for category, url in SEED_FEEDS:
        status, body = post_feed(url)
        if status == 201:
            created += 1
            fid = body["id"] if isinstance(body, dict) else "?"
            print(f"  +  [{category:17}] id={fid:<4} {url}")
        elif status == 200:
            existing += 1
            fid = body["id"] if isinstance(body, dict) else "?"
            print(f"  =  [{category:17}] id={fid:<4} {url}")
        else:
            failed.append((category, url, status, body))
            print(f"  !  [{category:17}] HTTP {status}: {url}")

    total = len(SEED_FEEDS)
    print(
        f"\nTotal {total} | created {created} | existing {existing} | "
        f"failed {len(failed)}"
    )
    if failed:
        print("\nFailed URLs:")
        for cat, url, status, body in failed:
            print(f"  - [{cat}] {url} -> {status}: {str(body)[:200]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
