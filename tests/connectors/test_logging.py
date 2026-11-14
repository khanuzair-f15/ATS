"""Tests for structured logging and secret redaction."""

from __future__ import annotations

import logging

import pytest

from jobindex.connectors.context import (
    SENSITIVE_HEADERS,
    SENSITIVE_QUERY_PARAMS,
    LogContext,
    RedactingFilter,
    setup_connector_logging,
)


class TestRedactingFilter:
    """Tests for RedactingFilter log filter."""

    def test_redacts_query_parameter_token(self) -> None:
        filter_ = RedactingFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Request URL: https://api.example.com/jobs?token=abc123&page=1",
            args=(), exc_info=None
        )
        filter_.filter(record)
        assert "token=***REDACTED***" in record.msg
        assert "abc123" not in record.msg
        assert "page=1" in record.msg  # Non-sensitive param preserved

    def test_redacts_query_parameter_api_key(self) -> None:
        filter_ = RedactingFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Calling https://api.example.com?api_key=secret123",
            args=(), exc_info=None
        )
        filter_.filter(record)
        assert "api_key=***REDACTED***" in record.msg
        assert "secret123" not in record.msg

    def test_redacts_query_parameter_secret(self) -> None:
        filter_ = RedactingFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="URL with secret=mysecret",
            args=(), exc_info=None
        )
        filter_.filter(record)
        assert "secret=***REDACTED***" in record.msg

    def test_redacts_multiple_query_params(self) -> None:
        filter_ = RedactingFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="https://example.com?token=t1&key=k1&secret=s1&normal=ok",
            args=(), exc_info=None
        )
        filter_.filter(record)
        assert "token=***REDACTED***" in record.msg
        assert "key=***REDACTED***" in record.msg
        assert "secret=***REDACTED***" in record.msg
        assert "normal=ok" in record.msg

    def test_redacts_authorization_header(self) -> None:
        filter_ = RedactingFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Headers: Authorization: Bearer secret-token",
            args=(), exc_info=None
        )
        filter_.filter(record)
        assert "Authorization: ***REDACTED***" in record.msg
        assert "secret-token" not in record.msg

    def test_redacts_cookie_header(self) -> None:
        filter_ = RedactingFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Cookie: session=abc123; user=john",
            args=(), exc_info=None
        )
        filter_.filter(record)
        assert "Cookie: ***REDACTED***" in record.msg
        assert "session=abc123" not in record.msg

    def test_redacts_set_cookie_header(self) -> None:
        filter_ = RedactingFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Set-Cookie: session=xyz789; HttpOnly",
            args=(), exc_info=None
        )
        filter_.filter(record)
        assert "Set-Cookie: ***REDACTED***" in record.msg

    def test_redacts_x_api_key_header(self) -> None:
        filter_ = RedactingFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="X-Api-Key: my-api-key-123",
            args=(), exc_info=None
        )
        filter_.filter(record)
        assert "X-Api-Key: ***REDACTED***" in record.msg

    def test_redacts_case_insensitive(self) -> None:
        filter_ = RedactingFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="authorization: Bearer token",
            args=(), exc_info=None
        )
        filter_.filter(record)
        assert "authorization: ***REDACTED***" in record.msg

    def test_redacts_in_args(self) -> None:
        filter_ = RedactingFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Request to %s with token %s",
            args=("https://api.example.com?token=secret123", "secret123"),
            exc_info=None
        )
        filter_.filter(record)
        # First arg (URL with param) should be redacted
        assert "token=***REDACTED***" in record.args[0]
        assert "secret123" not in record.args[0]
        # Second arg (bare token value) is not a recognized pattern, so not redacted
        # This is expected behavior - only known patterns (param=value, header: value) are redacted
        assert record.args[1] == "secret123"

    def test_preserves_non_sensitive_data(self) -> None:
        filter_ = RedactingFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="GET https://api.example.com/jobs?page=2&limit=50 duration=150ms",
            args=(), exc_info=None
        )
        filter_.filter(record)
        # Original message should be unchanged (no sensitive params)
        assert "page=2" in record.msg
        assert "limit=50" in record.msg
        assert "duration=150ms" in record.msg

    def test_empty_message_handled(self) -> None:
        filter_ = RedactingFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="",
            args=(), exc_info=None
        )
        assert filter_.filter(record) is True

    def test_non_string_message_handled(self) -> None:
        filter_ = RedactingFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg=123,  # Non-string
            args=(), exc_info=None
        )
        assert filter_.filter(record) is True


class TestSensitivePatterns:
    """Tests for the sensitive pattern constants."""

    def test_sensitive_query_params_pattern(self) -> None:
        test_cases = [
            ("token=abc", True),
            ("TOKEN=abc", True),
            ("api_key=xyz", True),
            ("API_KEY=xyz", True),
            ("secret=123", True),
            ("password=pass", True),
            ("auth=token", True),
            ("access_token=abc", True),
            ("refresh_token=xyz", True),
            ("page=2", False),
            ("limit=50", False),
            ("sort=date", False),
        ]
        for param, should_match in test_cases:
            match = SENSITIVE_QUERY_PARAMS.search(param)
            if should_match:
                assert match is not None, f"Expected {param!r} to match"
            else:
                assert match is None, f"Expected {param!r} not to match"

    def test_sensitive_headers_set(self) -> None:
        expected = {
            "authorization",
            "cookie",
            "set-cookie",
            "x-api-key",
            "x-auth-token",
            "proxy-authorization",
        }
        assert SENSITIVE_HEADERS == expected


class TestSetupConnectorLogging:
    """Tests for setup_connector_logging function."""

    def test_creates_logger_with_filter(self) -> None:
        logger = setup_connector_logging("jobindex.connectors.test")
        assert logger.name == "jobindex.connectors.test"
        assert any(isinstance(f, RedactingFilter) for f in logger.filters)

    def test_idempotent_multiple_calls(self) -> None:
        logger1 = setup_connector_logging("jobindex.connectors.test2")
        logger2 = setup_connector_logging("jobindex.connectors.test2")
        assert logger1 is logger2
        # Should only have one RedactingFilter
        redacting_filters = [f for f in logger1.filters if isinstance(f, RedactingFilter)]
        assert len(redacting_filters) == 1


class TestLogContext:
    """Tests for LogContext dataclass."""

    def test_default_correlation_id(self) -> None:
        ctx = LogContext(platform_key="test", tenant_key="acme", source_id="src-123")
        assert ctx.correlation_id
        assert len(ctx.correlation_id) == 8  # UUID prefix

    def test_custom_correlation_id(self) -> None:
        ctx = LogContext(
            correlation_id="custom-123",
            platform_key="test",
            tenant_key="acme",
            source_id="src-123",
        )
        assert ctx.correlation_id == "custom-123"

    def test_to_dict(self) -> None:
        ctx = LogContext(
            correlation_id="corr-123",
            platform_key="greenhouse",
            tenant_key="acme",
            source_id="src-456",
            operation="list_page",
            attempt=2,
        )
        d = ctx.to_dict()
        assert d == {
            "correlation_id": "corr-123",
            "platform_key": "greenhouse",
            "tenant_key": "acme",
            "source_id": "src-456",
            "operation": "list_page",
            "attempt": 2,
        }

    def test_partial_context(self) -> None:
        ctx = LogContext(platform_key="test", tenant_key="acme", source_id="src-123")
        d = ctx.to_dict()
        assert d["operation"] == ""
        assert d["attempt"] == 1


class TestStructuredLoggingIntegration:
    """Integration tests for structured logging in ConnectorContext."""

    @pytest.mark.asyncio
    async def test_logs_contain_correlation_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from jobindex.connectors.base import Tenant
        from jobindex.connectors.context import ConnectorConfig, ConnectorContext

        caplog.set_level(logging.DEBUG)
        tenant = Tenant(tenant_key="test", entry_url="https://example.com")
        config = ConnectorConfig(platform_key="test", tenant=tenant, source_id="src-123")

        async with ConnectorContext(config):
            # The context logs initialization
            pass

        # Check that correlation_id appears in log extra fields
        found = False
        for record in caplog.records:
            if hasattr(record, 'correlation_id') and record.correlation_id:
                found = True
                break
            if "Initialized" in record.message:
                found = True
                break
        assert found

    @pytest.mark.asyncio
    async def test_secret_redacted_in_logs(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from jobindex.connectors.base import Tenant
        from jobindex.connectors.context import ConnectorConfig, ConnectorContext

        caplog.set_level(logging.DEBUG)
        monkeypatch.setenv("JOBINDEX_SECRET_src_123", "super-secret-token")

        tenant = Tenant(tenant_key="test", entry_url="https://example.com")
        config = ConnectorConfig(platform_key="test", tenant=tenant, source_id="src-123")

        async with ConnectorContext(config):
            pass

        # Check that secret doesn't appear in logs
        log_text = "\n".join(record.message for record in caplog.records)
        assert "super-secret-token" not in log_text
        # Authorization header should be redacted if logged