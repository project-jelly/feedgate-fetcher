"""FastAPI HTTP API package."""

from feedgate_fetcher.api.dependencies import get_session, require_api_key
from feedgate_fetcher.api.errors import (
    PROBLEM_JSON_MEDIA_TYPE,
    register_exception_handlers,
)
from feedgate_fetcher.api.router import register_routers

__all__ = [
    "PROBLEM_JSON_MEDIA_TYPE",
    "get_session",
    "register_exception_handlers",
    "register_routers",
    "require_api_key",
]
