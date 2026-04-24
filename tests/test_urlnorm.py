"""URL normalization cases per docs/spec/feed.md."""

from __future__ import annotations

from feedgate.api.feeds import normalize_url


def test_scheme_lowercased() -> None:
    assert normalize_url("HTTP://example.com/feed") == "http://example.com/feed"


def test_host_lowercased() -> None:
    assert normalize_url("http://EXAMPLE.COM/feed") == "http://example.com/feed"


def test_default_http_port_stripped() -> None:
    assert normalize_url("http://example.com:80/feed") == "http://example.com/feed"


def test_default_https_port_stripped() -> None:
    assert normalize_url("https://example.com:443/feed") == "https://example.com/feed"


def test_non_default_port_kept() -> None:
    assert normalize_url("http://example.com:8080/feed") == "http://example.com:8080/feed"


def test_trailing_slash_removed_from_path() -> None:
    assert normalize_url("http://example.com/path/") == "http://example.com/path"


def test_root_trailing_slash_collapsed() -> None:
    assert normalize_url("http://example.com/") == "http://example.com"


def test_fragment_removed() -> None:
    assert normalize_url("http://example.com/page#anchor") == "http://example.com/page"


def test_query_preserved() -> None:
    url = "http://example.com/feed?cat=tech&sort=new"
    assert normalize_url(url) == url


def test_idn_host_to_punycode() -> None:
    # 한글.kr -> xn--bj0bj06e.kr (standard IDNA for that label)
    normalized = normalize_url("http://한글.kr/feed")
    assert normalized.startswith("http://xn--")
    assert normalized.endswith(".kr/feed")


def test_whitespace_stripped() -> None:
    assert normalize_url("  http://example.com/feed  ") == "http://example.com/feed"
