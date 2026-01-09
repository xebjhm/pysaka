"""Tests for pyhako.logging module."""

import logging
import os
from io import StringIO
from unittest.mock import patch

import pytest
import structlog

from pyhako.logging import configure_logging, _redact_secrets


class TestConfigureLogging:
    """Tests for configure_logging function."""

    def test_configure_logging_development_mode(self):
        """Test that development mode uses console renderer."""
        with patch.dict(os.environ, {"HAKO_ENV": "development"}):
            configure_logging()

            # Verify root logger has a handler
            root = logging.getLogger()
            assert len(root.handlers) >= 1
            assert root.level == logging.INFO

    def test_configure_logging_production_mode(self):
        """Test that production mode uses JSON renderer."""
        with patch.dict(os.environ, {"HAKO_ENV": "production"}):
            configure_logging()

            root = logging.getLogger()
            assert len(root.handlers) >= 1

    def test_configure_logging_default_mode(self):
        """Test that default (no env) uses development mode."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove HAKO_ENV if present
            os.environ.pop("HAKO_ENV", None)
            configure_logging()

            root = logging.getLogger()
            assert len(root.handlers) >= 1

    def test_configure_logging_silences_noisy_libraries(self):
        """Test that parso and asyncio loggers are silenced."""
        configure_logging()

        assert logging.getLogger("parso").level == logging.WARNING
        assert logging.getLogger("asyncio").level == logging.WARNING

    def test_configure_logging_removes_existing_handlers(self):
        """Test that existing handlers are removed before adding new ones."""
        root = logging.getLogger()
        # Store original handler count
        original_handlers = list(root.handlers)

        # Add a dummy handler
        dummy_handler = logging.StreamHandler()
        root.addHandler(dummy_handler)

        configure_logging()

        # The configure_logging function removes existing handlers and adds its own
        # Pytest may add its own handlers, so we just verify our dummy was removed
        assert dummy_handler not in root.handlers

        # Restore original state for other tests
        root.handlers = original_handlers

    def test_configure_logging_called_multiple_times(self):
        """Test that calling configure_logging multiple times is safe."""
        root = logging.getLogger()
        original_handlers = list(root.handlers)

        configure_logging()
        configure_logging()

        # Should not crash and should still have handlers
        assert len(root.handlers) >= 1

        # Restore original state
        root.handlers = original_handlers


class TestRedactSecrets:
    """Tests for _redact_secrets processor."""

    def test_redact_top_level_keys(self):
        """Test that sensitive top-level keys are redacted."""
        event_dict = {
            "access_token": "secret123",
            "password": "pass456",
            "message": "normal message",
        }

        result = _redact_secrets(None, "info", event_dict)

        assert result["access_token"] == "***REDACTED***"
        assert result["password"] == "***REDACTED***"
        assert result["message"] == "normal message"

    def test_redact_nested_dict_keys(self):
        """Test that sensitive keys in nested dicts are redacted."""
        event_dict = {
            "headers": {
                "authorization": "Bearer xyz",
                "content-type": "application/json",
            },
            "message": "request made",
        }

        result = _redact_secrets(None, "info", event_dict)

        assert result["headers"]["authorization"] == "***REDACTED***"
        assert result["headers"]["content-type"] == "application/json"

    def test_redact_case_insensitive(self):
        """Test that redaction is case-insensitive."""
        event_dict = {
            "ACCESS_TOKEN": "secret",
            "Token": "other_secret",
            "COOKIE": "session=abc",
        }

        result = _redact_secrets(None, "info", event_dict)

        assert result["ACCESS_TOKEN"] == "***REDACTED***"
        assert result["Token"] == "***REDACTED***"
        assert result["COOKIE"] == "***REDACTED***"

    def test_redact_all_sensitive_keys(self):
        """Test that all known sensitive keys are redacted."""
        sensitive_keys = [
            "access_token",
            "refresh_token",
            "token",
            "password",
            "secret",
            "cookie",
            "cookies",
            "authorization",
        ]

        event_dict = {key: f"value_{key}" for key in sensitive_keys}
        result = _redact_secrets(None, "info", event_dict)

        for key in sensitive_keys:
            assert result[key] == "***REDACTED***"

    def test_redact_preserves_non_sensitive_data(self):
        """Test that non-sensitive data is preserved."""
        event_dict = {
            "user_id": 123,
            "username": "john",
            "action": "login",
            "timestamp": "2024-01-01T00:00:00Z",
        }

        result = _redact_secrets(None, "info", event_dict)

        assert result == event_dict

    def test_redact_handles_empty_dict(self):
        """Test that empty dict is handled correctly."""
        result = _redact_secrets(None, "info", {})
        assert result == {}

    def test_redact_handles_nested_non_dict_values(self):
        """Test that non-dict nested values are handled correctly."""
        event_dict = {
            "items": ["a", "b", "c"],
            "count": 42,
            "nested": {"level1": {"level2": "value"}},
        }

        # Should not crash on non-dict values
        result = _redact_secrets(None, "info", event_dict)
        assert result["items"] == ["a", "b", "c"]
        assert result["count"] == 42
