"""``FeedCreate`` schema validation.

Covers the ``HttpUrl`` + custom ``_reject_internal_whitespace`` validator
layer. End-to-end API behavior (422 responses, trailing-slash merging)
is in ``tests/test_api_feeds.py``.

We use ``model_validate({"url": ...})`` rather than the keyword form so
mypy doesn't flag the raw ``str`` input against the declared ``HttpUrl``
type — Pydantic coerces at validation time, but the static signature
expects ``HttpUrl``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from feedgate_fetcher.schemas import FeedCreate


def _build(url: str) -> FeedCreate:
    return FeedCreate.model_validate({"url": url})


class TestAccepted:
    def test_basic_http(self) -> None:
        assert str(_build("http://example.com/feed").url) == "http://example.com/feed"

    def test_scheme_lowercased(self) -> None:
        assert str(_build("HTTP://example.com/feed").url) == "http://example.com/feed"

    def test_host_lowercased(self) -> None:
        assert str(_build("http://EXAMPLE.com/feed").url) == "http://example.com/feed"

    def test_default_http_port_stripped(self) -> None:
        assert str(_build("http://example.com:80/feed").url) == "http://example.com/feed"

    def test_default_https_port_stripped(self) -> None:
        assert str(_build("https://example.com:443/feed").url) == "https://example.com/feed"

    def test_non_default_port_kept(self) -> None:
        assert str(_build("http://example.com:8080/feed").url) == "http://example.com:8080/feed"

    def test_idn_host_to_punycode(self) -> None:
        result = str(_build("http://한글.kr/feed").url)
        assert result.startswith("http://xn--") and result.endswith(".kr/feed")

    def test_leading_trailing_whitespace_stripped(self) -> None:
        assert str(_build("  http://example.com/feed  ").url) == "http://example.com/feed"


class TestRejected:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "not-a-url",
            "ftp://example.com/feed",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://example.com/ feed",
            "http://example.com/feed\ttab",
            "http:// example.com/feed",
            "http://example.com/feed\nwithnewline",
        ],
    )
    def test_rejects(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            _build(bad)
