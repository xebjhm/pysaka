import logging
import os
import sys
from typing import Any

import structlog


def configure_logging() -> None:
    """
    Configure structured logging for PyHako.

    Respects HAKO_ENV environment variable:
    - 'development' (default): Colored, human-readable console output.
    - 'production': JSON output for machine parsing.
    """
    env = os.getenv("HAKO_ENV", "development").lower()

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        _redact_secrets,
    ]

    # Structlog processors
    processors = shared_processors + [
        # Prepare for logging stdlib
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure standard logging to redirect to structlog
    formatter = structlog.stdlib.ProcessorFormatter(
        # These run solely on log entries that originate from stdlib logging messages.
        foreign_pre_chain=shared_processors,
        # These run on all log entries.
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
             structlog.processors.JSONRenderer() if env == "production" else structlog.dev.ConsoleRenderer(),
        ],
    )

    root_logger = logging.getLogger()

    # Avoid adding duplicate handlers if configure_logging is called multiple times
    if root_logger.handlers:
        for h in root_logger.handlers:
             root_logger.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)

    # Silence noisy libraries
    logging.getLogger("parso").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _redact_secrets(
    logger: logging.Logger,
    method_name: str,
    event_dict: dict[str, Any]
) -> dict[str, Any]:
    """
    Processor to redact sensitive keys from log output.
    """
    sensitive_keys = {
        "access_token", "refresh_token", "token", "password", "secret", "cookie", "cookies", "authorization"
    }

    # Redact top-level keys
    for key in event_dict.copy():
        if key.lower() in sensitive_keys:
            event_dict[key] = "***REDACTED***"

    # Shallow redaction for dictionary values (handling headers/cookies dicts)
    for key, value in event_dict.items():
        if isinstance(value, dict):
            for sub_key in value:
                if sub_key.lower() in sensitive_keys:
                    value[sub_key] = "***REDACTED***"

    return event_dict
