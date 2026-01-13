import logging
import os
import sys
from pathlib import Path
from typing import Any

import structlog


def configure_logging(
    log_file: str | Path | None = None,
    log_level: int = logging.INFO,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> None:
    """
    Configure structured logging for PyHako.

    Args:
        log_file: Optional path to log file. If provided, adds FileHandler.
        log_level: Root logger level (default: INFO)
        console_level: Console handler level (default: INFO)
        file_level: File handler level (default: DEBUG)

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
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Determine renderer based on environment
    if env == "production":
        console_renderer = structlog.processors.JSONRenderer()
        file_renderer = structlog.processors.JSONRenderer()
    else:
        console_renderer = structlog.dev.ConsoleRenderer()
        # File gets plain text for readability in log files
        file_renderer = structlog.dev.ConsoleRenderer(colors=False)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers to avoid duplicates
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)

    # Console Handler (stdout)
    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            console_renderer,
        ],
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(console_level)
    root_logger.addHandler(console_handler)

    # File Handler (optional)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                file_renderer,
            ],
        )
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(file_level)
        root_logger.addHandler(file_handler)

    # Silence noisy libraries - these are very verbose at DEBUG level
    noisy_loggers = [
        "parso", "asyncio", "httpcore", "httpx",
        "aiohttp", "urllib3", "charset_normalizer",
        "chardet", "PIL", "aiohttp.access",
        "urllib3.connectionpool",
    ]
    for lib in noisy_loggers:
        logging.getLogger(lib).setLevel(logging.WARNING)


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
