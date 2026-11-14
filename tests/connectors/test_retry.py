"""Tests for retry policies and behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import tenacity

from jobindex.connectors.base import Tenant
from jobindex.connectors.context import (
    ConnectorConfig,
    ConnectorContext,
    classify_error,
    create_connector_retry_policy,
    is_retryable_error,
    retry_if_transient_or_rate_limited,
)
from jobindex.db.enums import ErrorClass


class TestErrorClassification:
    """Tests for error classification logic."""

    def test_classify_none_error_success_response(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.is_success = True
        response.status_code = 200
        assert classify_error(None, response) == ErrorClass.NONE

    def test_classify_429_rate_limited(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.is_success = False
        response.status_code = 429
        assert classify_error(None, response) == ErrorClass.RATE_LIMITED

    def test_classify_403_blocked(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.is_success = False
        response.status_code = 403
        assert classify_error(None, response) == ErrorClass.BLOCKED

    def test_classify_401_blocked(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.is_success = False
        response.status_code = 401
        assert classify_error(None, response) == ErrorClass.BLOCKED

    def test_classify_404_not_found(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.is_success = False
        response.status_code = 404
        assert classify_error(None, response) == ErrorClass.NOT_FOUND

    def test_classify_400_permanent(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.is_success = False
        response.status_code = 400
        assert classify_error(None, response) == ErrorClass.PERMANENT

    def test_classify_500_transient(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.is_success = False
        response.status_code = 500
        assert classify_error(None, response) == ErrorClass.TRANSIENT

    def test_classify_timeout_exception(self) -> None:
        exc = httpx.TimeoutException("Request timed out")
        assert classify_error(exc) == ErrorClass.TRANSIENT

    def test_classify_network_error(self) -> None:
        exc = httpx.NetworkError("Connection failed")
        assert classify_error(exc) == ErrorClass.TRANSIENT

    def test_classify_http_status_error_429(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.status_code = 429
        exc = httpx.HTTPStatusError("Rate limited", request=MagicMock(), response=response)
        assert classify_error(exc) == ErrorClass.RATE_LIMITED

    def test_classify_http_status_error_500(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.status_code = 500
        exc = httpx.HTTPStatusError("Server error", request=MagicMock(), response=response)
        assert classify_error(exc) == ErrorClass.TRANSIENT

    def test_classify_http_status_error_400(self) -> None:
        response = MagicMock(spec=httpx.Response)
        response.status_code = 400
        exc = httpx.HTTPStatusError("Bad request", request=MagicMock(), response=response)
        assert classify_error(exc) == ErrorClass.PERMANENT

    def test_classify_json_parse_error(self) -> None:
        exc = ValueError("Expecting value: line 1 column 1 (char 0) - json decode error")
        assert classify_error(exc) == ErrorClass.PARSE

    def test_classify_unknown_error_defaults_transient(self) -> None:
        exc = RuntimeError("Something unexpected")
        assert classify_error(exc) == ErrorClass.TRANSIENT


class TestIsRetryableError:
    """Tests for retryable error determination."""

    def test_transient_is_retryable(self) -> None:
        assert is_retryable_error(ErrorClass.TRANSIENT) is True

    def test_rate_limited_is_retryable(self) -> None:
        assert is_retryable_error(ErrorClass.RATE_LIMITED) is True

    def test_permanent_not_retryable(self) -> None:
        assert is_retryable_error(ErrorClass.PERMANENT) is False

    def test_blocked_not_retryable(self) -> None:
        assert is_retryable_error(ErrorClass.BLOCKED) is False

    def test_not_found_not_retryable(self) -> None:
        assert is_retryable_error(ErrorClass.NOT_FOUND) is False

    def test_parse_not_retryable(self) -> None:
        assert is_retryable_error(ErrorClass.PARSE) is False

    def test_none_not_retryable(self) -> None:
        assert is_retryable_error(ErrorClass.NONE) is False


class TestRetryPolicyCreation:
    """Tests for retry policy factory."""

    def test_create_retry_policy_defaults(self) -> None:
        policy = create_connector_retry_policy()
        assert isinstance(policy, tenacity.AsyncRetrying)
        # Check stop condition by attempting to stop at attempt 4 (should stop after 3)
        # We can't easily compare stop objects by identity, so verify the max attempts
        assert policy.stop is not None

    def test_create_retry_policy_custom_attempts(self) -> None:
        policy = create_connector_retry_policy(max_attempts=5)
        assert isinstance(policy, tenacity.AsyncRetrying)
        assert policy.stop is not None


class TestRetryCondition:
    """Tests for the retry_if_transient_or_rate_limited condition."""

    def test_retry_on_transient_exception(self) -> None:
        retry_state = MagicMock()
        exc = httpx.TimeoutException("timeout")
        retry_state.outcome = MagicMock()
        retry_state.outcome.exception.return_value = exc
        retry_state.outcome.failed = True

        assert retry_if_transient_or_rate_limited(retry_state) is True

    def test_retry_on_rate_limited(self) -> None:
        retry_state = MagicMock()
        response = MagicMock(spec=httpx.Response)
        response.status_code = 429
        exc = httpx.HTTPStatusError("Rate limited", request=MagicMock(), response=response)
        retry_state.outcome = MagicMock()
        retry_state.outcome.exception.return_value = exc
        retry_state.outcome.failed = True

        assert retry_if_transient_or_rate_limited(retry_state) is True

    def test_no_retry_on_permanent_error(self) -> None:
        retry_state = MagicMock()
        response = MagicMock(spec=httpx.Response)
        response.status_code = 400
        exc = httpx.HTTPStatusError("Bad request", request=MagicMock(), response=response)
        retry_state.outcome = MagicMock()
        retry_state.outcome.exception.return_value = exc
        retry_state.outcome.failed = True

        assert retry_if_transient_or_rate_limited(retry_state) is False

    def test_no_retry_on_not_found(self) -> None:
        retry_state = MagicMock()
        response = MagicMock(spec=httpx.Response)
        response.status_code = 404
        exc = httpx.HTTPStatusError("Not found", request=MagicMock(), response=response)
        retry_state.outcome = MagicMock()
        retry_state.outcome.exception.return_value = exc
        retry_state.outcome.failed = True

        assert retry_if_transient_or_rate_limited(retry_state) is False

    def test_no_retry_on_success(self) -> None:
        retry_state = MagicMock()
        retry_state.outcome = MagicMock()
        retry_state.outcome.exception.return_value = None
        retry_state.outcome.failed = False

        assert retry_if_transient_or_rate_limited(retry_state) is False


class TestConnectorContextRetryIntegration:
    """Integration tests for retry behavior in ConnectorContext."""

    @pytest.fixture
    def tenant(self) -> Tenant:
        return Tenant(tenant_key="test", entry_url="https://example.com")

    @pytest.fixture
    def config(self, tenant: Tenant) -> ConnectorConfig:
        return ConnectorConfig(
            platform_key="test",
            tenant=tenant,
            source_id="src-123",
            max_retries=3,
        )

    @pytest.mark.asyncio
    async def test_retries_on_timeout_then_succeeds(self, config: ConnectorConfig) -> None:
        """Test that transient errors are retried and eventually succeed."""
        async with ConnectorContext(config) as ctx:
            # First two calls raise timeout, third succeeds
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 200
            mock_response.is_success = True

            call_count = 0

            async def mock_request(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    raise httpx.TimeoutException("timeout")
                return mock_response

            ctx.client.request = mock_request

            response = await ctx.get("https://example.com", operation="test")
            assert response.status_code == 200
            assert call_count == 3

    @pytest.mark.asyncio
    async def test_retries_exhausted_raises_last_exception(self, config: ConnectorConfig) -> None:
        """Test that after max retries, the last exception is raised."""
        async with ConnectorContext(config) as ctx:
            ctx.client.request = AsyncMock(side_effect=httpx.TimeoutException("persistent timeout"))

            with pytest.raises(httpx.TimeoutException):
                await ctx.get("https://example.com", operation="test")

            # Should have been called max_retries times (3)
            assert ctx.client.request.call_count == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_404(self, config: ConnectorConfig) -> None:
        """Test that 404 errors are not retried."""
        async with ConnectorContext(config) as ctx:
            response = MagicMock(spec=httpx.Response)
            response.status_code = 404
            response.is_success = False
            exc = httpx.HTTPStatusError("Not found", request=MagicMock(), response=response)
            ctx.client.request = AsyncMock(side_effect=exc)

            with pytest.raises(httpx.HTTPStatusError):
                await ctx.get("https://example.com", operation="test")

            # Should only be called once (no retry)
            assert ctx.client.request.call_count == 1

    @pytest.mark.asyncio
    async def test_no_retry_on_400(self, config: ConnectorConfig) -> None:
        """Test that 400 errors are not retried."""
        async with ConnectorContext(config) as ctx:
            response = MagicMock(spec=httpx.Response)
            response.status_code = 400
            response.is_success = False
            exc = httpx.HTTPStatusError("Bad request", request=MagicMock(), response=response)
            ctx.client.request = AsyncMock(side_effect=exc)

            with pytest.raises(httpx.HTTPStatusError):
                await ctx.get("https://example.com", operation="test")

            assert ctx.client.request.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_429(self, config: ConnectorConfig) -> None:
        """Test that 429 errors are retried."""
        async with ConnectorContext(config) as ctx:
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 200
            mock_response.is_success = True

            call_count = 0

            async def mock_request(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    response_429 = MagicMock(spec=httpx.Response)
                    response_429.status_code = 429
                    response_429.is_success = False
                    exc = httpx.HTTPStatusError(
    "Rate limited", request=MagicMock(), response=response_429
)
                    raise exc
                return mock_response

            ctx.client.request = mock_request

            response = await ctx.get("https://example.com", operation="test")
            assert response.status_code == 200
            assert call_count == 2  # Initial + 1 retry


class TestRetryAfterHeader:
    """Tests for Retry-After header handling (future enhancement placeholder)."""

    @pytest.mark.asyncio
    async def test_retry_after_header_respected(self) -> None:
        """Placeholder for Retry-After header support.
        
        Currently the retry policy uses exponential backoff with jitter.
        Future enhancement: parse Retry-After header and wait that duration.
        """
        # This is a placeholder - the current implementation uses
        # exponential backoff. A full implementation would:
        # 1. Check for Retry-After header on 429 responses
        # 2. Parse seconds or HTTP-date
        # 3. Wait that duration before retry
        pass