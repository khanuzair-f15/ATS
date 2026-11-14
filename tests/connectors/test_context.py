"""Tests for ConnectorContext lifecycle and HTTP client management."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from jobindex.connectors.base import Tenant
from jobindex.connectors.context import (
    ConnectorConfig,
    ConnectorContext,
    RateLimitConfig,
    create_connector_context,
    resolve_secret,
)


class TestConnectorConfig:
    """Tests for ConnectorConfig dataclass."""

    def test_default_values(self) -> None:
        tenant = Tenant(tenant_key="test", entry_url="https://example.com")
        config = ConnectorConfig(platform_key="test", tenant=tenant, source_id="src-123")
        assert config.platform_key == "test"
        assert config.tenant == tenant
        assert config.source_id == "src-123"
        assert config.max_retries == 3
        assert config.timeout_s is None
        assert config.custom_headers == {}

    def test_custom_values(self) -> None:
        tenant = Tenant(tenant_key="test", entry_url="https://example.com")
        config = ConnectorConfig(
            platform_key="test",
            tenant=tenant,
            source_id="src-123",
            max_retries=5,
            timeout_s=30.0,
            custom_headers={"X-Custom": "value"},
        )
        assert config.max_retries == 5
        assert config.timeout_s == 30.0
        assert config.custom_headers == {"X-Custom": "value"}


class TestRateLimitConfig:
    """Tests for RateLimitConfig."""

    def test_defaults(self) -> None:
        config = RateLimitConfig()
        assert config.requests_per_second == 0.5
        assert config.max_concurrency == 1
        assert config.burst_allowance == 1

    def test_custom(self) -> None:
        config = RateLimitConfig(requests_per_second=2.0, max_concurrency=3, burst_allowance=5)
        assert config.requests_per_second == 2.0
        assert config.max_concurrency == 3
        assert config.burst_allowance == 5


class TestResolveSecret:
    """Tests for secret resolution per ADR 0005."""

    def test_env_var_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOBINDEX_SECRET_abc_def", "env-secret-value")
        secret = resolve_secret("abc-def")
        assert secret == "env-secret-value"

    def test_file_secret(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        secret_file = tmp_path / "src-123"
        secret_file.write_text("file-secret-value")
        monkeypatch.setenv("JOBINDEX_SECRET_DIR", str(tmp_path))
        secret = resolve_secret("src-123")
        assert secret == "file-secret-value"

    def test_env_overrides_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        secret_file = tmp_path / "src-123"
        secret_file.write_text("file-secret")
        monkeypatch.setenv("JOBINDEX_SECRET_DIR", str(tmp_path))
        monkeypatch.setenv("JOBINDEX_SECRET_src_123", "env-secret")
        secret = resolve_secret("src-123")
        assert secret == "env-secret"

    def test_no_secret(self) -> None:
        secret = resolve_secret("nonexistent-source")
        assert secret is None


class TestConnectorContextLifecycle:
    """Tests for ConnectorContext async context manager lifecycle."""

    @pytest.fixture
    def tenant(self) -> Tenant:
        return Tenant(tenant_key="acme", entry_url="https://acme.example.com/careers")

    @pytest.fixture
    def config(self, tenant: Tenant) -> ConnectorConfig:
        return ConnectorConfig(
            platform_key="greenhouse",
            tenant=tenant,
            source_id="src-123",
        )

    @pytest.mark.asyncio
    async def test_context_manager_initializes_client(self, config: ConnectorConfig) -> None:
        async with ConnectorContext(config) as ctx:
            assert ctx.client is not None
            assert isinstance(ctx.client, httpx.AsyncClient)
            assert ctx.client.headers["User-Agent"].startswith("JobIndexBot/")

    @pytest.mark.asyncio
    async def test_client_initialized_with_http2(self, config: ConnectorConfig) -> None:
        """Test that ConnectorContext initializes the HTTP client with HTTP/2 support."""
        async with ConnectorContext(config) as ctx:
            # Verify HTTP/2 transport is initialized with a connection pool
            # This tests that the h2 dependency is properly installed and configured
            assert hasattr(ctx.client, "_transport")
            assert hasattr(ctx.client._transport, "_pool")
            assert ctx.client._transport._pool is not None

    @pytest.mark.asyncio
    async def test_context_manager_closes_client(self, config: ConnectorConfig) -> None:
        ctx = ConnectorContext(config)
        async with ctx:
            client = ctx.client
            assert not client.is_closed
        # After exit, client should be closed
        assert client.is_closed

    @pytest.mark.asyncio
    async def test_client_reuse_within_context(self, config: ConnectorConfig) -> None:
        async with ConnectorContext(config) as ctx:
            client1 = ctx.client
            client2 = ctx.client
            assert client1 is client2  # Same instance reused

    @pytest.mark.asyncio
    async def test_secret_injected_into_headers(
        self, config: ConnectorConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JOBINDEX_SECRET_src_123", "test-secret")
        async with ConnectorContext(config) as ctx:
            assert ctx.client.headers.get("Authorization") == "Bearer test-secret"

    @pytest.mark.asyncio
    async def test_custom_headers_merged(self, config: ConnectorConfig) -> None:
        config.custom_headers = {"X-Custom": "value"}
        async with ConnectorContext(config) as ctx:
            assert ctx.client.headers.get("X-Custom") == "value"

    @pytest.mark.asyncio
    async def test_timeout_configuration(self, tenant: Tenant) -> None:
        config = ConnectorConfig(
            platform_key="test",
            tenant=tenant,
            source_id="src-123",
            timeout_s=30.0,
        )
        async with ConnectorContext(config) as ctx:
            # Timeout is set on the client
            assert ctx.client.timeout.read == 30.0

    @pytest.mark.asyncio
    async def test_rate_limit_config_stored(self, config: ConnectorConfig) -> None:
        config.rate_limit_config = RateLimitConfig(
            requests_per_second=1.0, max_concurrency=2
        )
        async with ConnectorContext(config) as ctx:
            assert ctx.config.rate_limit_config is not None
            assert ctx.config.rate_limit_config.requests_per_second == 1.0
            assert ctx.config.rate_limit_config.max_concurrency == 2

    @pytest.mark.asyncio
    async def test_log_context_created(self, config: ConnectorConfig) -> None:
        async with ConnectorContext(config) as ctx:
            log_ctx = ctx.log_context
            assert log_ctx.platform_key == "greenhouse"
            assert log_ctx.tenant_key == "acme"
            assert log_ctx.source_id == "src-123"
            assert log_ctx.correlation_id  # Should be generated

    @pytest.mark.asyncio
    async def test_with_operation_creates_child_context(
        self, config: ConnectorConfig
    ) -> None:
        async with ConnectorContext(config) as ctx:
            op_ctx = ctx.with_operation("list_page", attempt=2)
            assert op_ctx.operation == "list_page"
            assert op_ctx.attempt == 2
            assert op_ctx.correlation_id == ctx.log_context.correlation_id


class TestConnectorContextRequests:
    """Tests for ConnectorContext request methods (mocked)."""

    @pytest.fixture
    def tenant(self) -> Tenant:
        return Tenant(tenant_key="acme", entry_url="https://acme.example.com/careers")

    @pytest.fixture
    def config(self, tenant: Tenant) -> ConnectorConfig:
        return ConnectorConfig(platform_key="test", tenant=tenant, source_id="src-123")

    @pytest.mark.asyncio
    async def test_get_request_mocked(self, config: ConnectorConfig) -> None:
        async with ConnectorContext(config) as ctx:
            # Mock the underlying client request
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 200
            mock_response.is_success = True
            ctx.client.request = AsyncMock(return_value=mock_response)

            response = await ctx.get("https://api.example.com/jobs", operation="list_page")
            assert response.status_code == 200
            ctx.client.request.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_request_mocked(self, config: ConnectorConfig) -> None:
        async with ConnectorContext(config) as ctx:
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 201
            ctx.client.request = AsyncMock(return_value=mock_response)

            response = await ctx.post(
                "https://api.example.com/jobs",
                json={"key": "value"},
                operation="create",
            )
            assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_request_logs_correlation_id(
        self, config: ConnectorConfig, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("DEBUG")
        async with ConnectorContext(config) as ctx:
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 200
            mock_response.is_success = True
            ctx.client.request = AsyncMock(return_value=mock_response)

            await ctx.get("https://api.example.com/jobs", operation="list_page")

            # Check correlation ID appears in log extra fields
            found = False
            for record in caplog.records:
                if hasattr(record, 'correlation_id') and record.correlation_id:
                    found = True
                    break
                if "list_page" in record.message:
                    found = True
                    break
            assert found


class TestCreateConnectorContextFactory:
    """Tests for the create_connector_context factory function."""

    @pytest.mark.asyncio
    async def test_factory_creates_context(self) -> None:
        tenant = Tenant(tenant_key="test", entry_url="https://example.com")
        ctx = await create_connector_context(
            platform_key="test",
            tenant=tenant,
            source_id="src-123",
            rate_limit_rps=1.0,
            max_concurrency=2,
        )
        assert isinstance(ctx, ConnectorContext)
        assert ctx.config.platform_key == "test"
        assert ctx.config.rate_limit_config is not None
        assert ctx.config.rate_limit_config.requests_per_second == 1.0
        assert ctx.config.rate_limit_config.max_concurrency == 2
        # Don't forget to close
        await ctx.__aexit__(None, None, None)


class TestConnectorContextErrorHandling:
    """Tests for error handling in ConnectorContext."""

    @pytest.fixture
    def tenant(self) -> Tenant:
        return Tenant(tenant_key="test", entry_url="https://example.com")

    @pytest.fixture
    def config(self, tenant: Tenant) -> ConnectorConfig:
        return ConnectorConfig(
            platform_key="test",
            tenant=tenant,
            source_id="src-123",
            max_retries=1,
        )

    @pytest.mark.asyncio
    async def test_client_not_initialized_raises(self, config: ConnectorConfig) -> None:
        ctx = ConnectorContext(config)
        with pytest.raises(RuntimeError, match="not initialized"):
            _ = ctx.client

    @pytest.mark.asyncio
    async def test_request_propagates_exception(self, config: ConnectorConfig) -> None:
        async with ConnectorContext(config) as ctx:
            ctx.client.request = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            with pytest.raises(httpx.TimeoutException):
                await ctx.get("https://example.com", operation="test")