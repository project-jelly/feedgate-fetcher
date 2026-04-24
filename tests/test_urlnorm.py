"""``normalize_url`` unit tests.

``normalize_url`` runs on the stringified ``HttpUrl`` from ``FeedCreate``
and is now a thin layer on top of it — scheme/host/port/IDN/whitespace
handling lives in ``HttpUrl`` (see ``tests/test_feed_create_schema.py``).
What remains here: trailing-slash collapsing and fragment removal.
"""

from __future__ import annotations

from feedgate_fetcher.api.feeds import normalize_url


def test_trailing_slash_removed_from_path() -> None:
    assert normalize_url("http://example.com/path/") == "http://example.com/path"


def test_root_trailing_slash_collapsed() -> None:
    assert normalize_url("http://example.com/") == "http://example.com"


def test_fragment_removed() -> None:
    assert normalize_url("http://example.com/page#anchor") == "http://example.com/page"


def test_query_preserved() -> None:
    url = "http://example.com/feed?cat=tech&sort=new"
    assert normalize_url(url) == url


def test_bare_path_unchanged() -> None:
    assert normalize_url("http://example.com/feed") == "http://example.com/feed"
