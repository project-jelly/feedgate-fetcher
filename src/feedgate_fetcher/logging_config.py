"""Structured logging configuration for feedgate-fetcher."""

from __future__ import annotations

import logging

import structlog

_SILENT_PATHS = frozenset({"/healthz", "/metrics"})


class _SilentPathFilter(logging.Filter):
    """Drop uvicorn.access records for probe/scrape endpoints."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(path in msg for path in _SILENT_PATHS)


def configure_logging(log_level: str = "INFO", json_logs: bool = False) -> None:
    """Configure stdlib logging + structlog with one unified pipeline."""
    level_name = log_level.upper()
    level = getattr(logging, level_name, logging.INFO)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for logger_name in ("uvicorn", "uvicorn.error", "fastapi"):
        lg = logging.getLogger(logger_name)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(level)

    # Access log: suppress /healthz probe and /metrics scrape noise
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.filters.clear()
    access_logger.propagate = True
    access_logger.setLevel(level)
    access_logger.addFilter(_SilentPathFilter())

    # SQLAlchemy: WARNING suppresses per-query echo (engine echo=False is not enough
    # when the root logger is at INFO — the engine logger still propagates).
    for logger_name in ("sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool"):
        lg = logging.getLogger(logger_name)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
