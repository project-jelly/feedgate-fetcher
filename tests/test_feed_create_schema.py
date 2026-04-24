"""``FeedCreate`` schema validation.

Covers the ``HttpUrl`` + custom ``_reject_internal_whitespace`` validator
layer. End-to-end API behavior (422 responses, trailing-slash merging)
is in ``tests/test_api_feeds.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from feedgate_fetcher.schemas import FeedCreate


class TestAccepted:
    def test_basic_http(self) -> None:
        assert str(FeedCreate(url="http://example.com/feed").url) == "http://example.com/feed"

    def test_scheme_lowercased(self) -> None:
        assert str(FeedCreate(url="HTTP://example.com/feed").url) == "http://example.com/feed"

    def test_host_lowercased(self) -> None:
        assert str(FeedCreate(url="http://EXAMPLE.com/feed").url) == "http://example.com/feed"

    def test_default_http_port_stripped(self) -> None:
        assert str(FeedCreate(url="http://example.com:80/feed").url) == "http://example.com/feed"

    def test_default_https_port_stripped(self) -> None:
        assert str(FeedCreate(url="https://example.com:443/feed").url) == "https://example.com/feed"

    def test_non_default_port_kept(self) -> None:
        assert (
            str(FeedCreate(url="http://example.com:8080/feed").url)
            == "http://example.com:8080/feed"
        )

    def test_idn_host_to_punycode(self) -> None:
        result = str(FeedCreate(url="http://한글.kr/feed").url)
        assert result.startswith("http://xn--") and result.endswith(".kr/feed")

    def test_leading_trailing_whitespace_stripped(self) -> None:
        assert str(FeedCreate(url="  http://example.com/feed  ").url) == "http://example.com/feed"


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
            FeedCreate(url=bad)
