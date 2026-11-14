"""Shared HTTP client and execution context for connectors.

This module provides:
- `ConnectorContext`: Async context manager owning an `httpx.AsyncClient` with
  configured timeouts, connection pooling, retry policies, rate limiting, and
  structured logging.
- `RateLimiter`: Token-bucket rate limiter with per-host concurrency control.
- `classify_error`: Centralized error classification mapping to `ErrorClass`.
- `RedactingFilter`: Logging filter that redacts secrets from log records.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
import tenacity
from tenacity import (
    after_log,
    before_sleep_log,
    stop_after_attempt,
    wait_exponential_jitter,
)

from jobindex.connectors.base import Tenant
from jobindex.db.enums import ErrorClass
from jobindex.settings import get_settings

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

DEFAULT_MAX_CONCURRENCY_PER_HOST = 1
DEFAULT_REQUEST_JITTER_MS = 250
DEFAULT_HTTP_TIMEOUT_S = 20.0
DEFAULT_MAX_RETRIES = 3

# Secret patterns to redact from URLs and headers
SENSITIVE_QUERY_PARAMS = re.compile(
    r"(token|key|secret|api_key|auth|password|access_token|refresh_token)",
    re.IGNORECASE,
)
SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "proxy-authorization",
}

# -----------------------------------------------------------------------------
# Rate Limiter
# -----------------------------------------------------------------------------


@dataclass
class RateLimitConfig:
    """Rate limit configuration for a specific host or connector."""
    requests_per_second: float = 0.5  # Default from source.rate_limit_rps
    max_concurrency: int = 1
    burst_allowance: int = 1  # Token bucket burst capacity


class TokenBucket:
    """Token bucket rate limiter for a single host."""

    def __init__(self, rate_per_second: float, burst: int = 1):
        self.rate = rate_per_second
        self.burst = max(1, burst)
        self._tokens = float(self.burst)
        self._last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        """Take a token, waiting if necessary."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_update
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
            self._last_update = now

            if self._tokens >= 1:
                self._tokens -= 1
                return

            # Wait for next token
            wait_time = (1 - self._tokens) / self.rate
            self._tokens = 0
        await asyncio.sleep(wait_time)


class HostRateLimiter:
    """Per-host rate limiter with concurrency control."""

    def __init__(self, default_config: RateLimitConfig | None = None):
        self.default_config = default_config or RateLimitConfig()
        self._buckets: dict[str, TokenBucket] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    def _get_host(self, url: str) -> str:
        """Extract host from URL."""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    async def _get_bucket(self, host: str, config: RateLimitConfig) -> TokenBucket:
        async with self._lock:
            if host not in self._buckets:
                self._buckets[host] = TokenBucket(
                    config.requests_per_second,
                    config.burst_allowance,
                )
            return self._buckets[host]

    async def _get_semaphore(self, host: str, max_concurrency: int) -> asyncio.Semaphore:
        async with self._lock:
            if host not in self._semaphores:
                self._semaphores[host] = asyncio.Semaphore(max_concurrency)
            return self._semaphores[host]

    @asynccontextmanager
    async def acquire(self, url: str, config: RateLimitConfig | None = None) -> AsyncIterator[None]:
        """Acquire rate limit slot for a request to the given URL."""
        effective_config = config or self.default_config
        host = self._get_host(url)

        bucket = await self._get_bucket(host, effective_config)
        semaphore = await self._get_semaphore(host, effective_config.max_concurrency)

        await bucket.take()
        async with semaphore:
            # Apply jitter after acquiring semaphore but before request
            if effective_config.requests_per_second > 0:
                jitter_ms = get_settings().request_jitter_ms
                if jitter_ms > 0:
                    # Jitter is for politeness, not security; random is acceptable here
                    await asyncio.sleep(random.uniform(0, jitter_ms / 1000.0))  # noqa: S311

            try:
                yield
            finally:
                pass  # Semaphore released automatically


class RateLimiter:
    """Global rate limiter managing per-host limiters."""

    def __init__(self) -> None:
        self._limiters: dict[str, HostRateLimiter] = {}
        self._default_config = RateLimitConfig()
        self._lock = asyncio.Lock()

    def configure_host(self, host: str, config: RateLimitConfig) -> None:
        """Configure rate limit for a specific host.

        Pre-creates a HostRateLimiter for the given host with the specified
        configuration. Subsequent calls to acquire() for this host will use
        the pre-configured limiter.
        """

        async def _set_limiter() -> None:
            async with self._lock:
                self._limiters[host] = HostRateLimiter(config)

        # Run the async operation synchronously for simplicity
        # In practice, this could be made async if needed
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If we're in an async context, schedule the task
                task = loop.create_task(_set_limiter())
                # Task is intentionally not awaited; it runs in background
                _ = task
            else:
                loop.run_until_complete(_set_limiter())
        except RuntimeError:
            # No event loop, create one
            asyncio.run(_set_limiter())

    def configure_source(
        self,
        source_id: str,
        rate_limit_rps: float,
        max_concurrency: int,
    ) -> RateLimitConfig:
        """Create a RateLimitConfig from source settings."""
        return RateLimitConfig(
            requests_per_second=max(0.1, rate_limit_rps),
            max_concurrency=max(1, max_concurrency),
            burst_allowance=max(1, int(rate_limit_rps * 2)),  # Allow 2 seconds burst
        )

    @asynccontextmanager
    async def acquire(self, url: str, config: RateLimitConfig | None = None) -> AsyncIterator[None]:
        """Acquire rate limit for a URL using the default limiter."""
        host = self._get_host(url)
        async with self._lock:
            if host not in self._limiters:
                self._limiters[host] = HostRateLimiter(config or self._default_config)
            limiter = self._limiters[host]
        async with limiter.acquire(url, config):
            yield

    def _get_host(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"


# -----------------------------------------------------------------------------
# Error Classification
# -----------------------------------------------------------------------------


def classify_error(exc: BaseException | None, response: httpx.Response | None = None) -> ErrorClass:
    """Classify an error or HTTP response into an ErrorClass.

    Args:
        exc: The exception that was raised, if any.
        response: The HTTP response, if available.

    Returns:
        ErrorClass enum value.
    """
    # No error case
    if exc is None and response is not None:
        if response.is_success:
            return ErrorClass.NONE
        if response.status_code == 429:
            return ErrorClass.RATE_LIMITED
        if response.status_code in (403, 401):
            return ErrorClass.BLOCKED
        if response.status_code in (404, 410):
            return ErrorClass.NOT_FOUND
        if 400 <= response.status_code < 500:
            return ErrorClass.PERMANENT
        if 500 <= response.status_code < 600:
            return ErrorClass.TRANSIENT
        return ErrorClass.PERMANENT

    # Exception-based classification
    if exc is None:
        return ErrorClass.NONE

    # httpx exceptions
    if isinstance(exc, httpx.TimeoutException):
        return ErrorClass.TRANSIENT
    if isinstance(exc, httpx.NetworkError):
        return ErrorClass.TRANSIENT
    if isinstance(exc, httpx.ProtocolError):
        return ErrorClass.PERMANENT

    # HTTP status errors (raised by response.raise_for_status())
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return ErrorClass.RATE_LIMITED
        if status in (403, 401):
            return ErrorClass.BLOCKED
        if status in (404, 410):
            return ErrorClass.NOT_FOUND
        if 400 <= status < 500:
            return ErrorClass.PERMANENT
        if 500 <= status < 600:
            return ErrorClass.TRANSIENT
        return ErrorClass.PERMANENT

    # JSON/parsing errors
    if isinstance(exc, (ValueError, TypeError)) and "json" in str(exc).lower():
        return ErrorClass.PARSE
    # json.JSONDecodeError is a subclass of ValueError, but check explicitly
    try:
        import json
        if isinstance(exc, json.JSONDecodeError):
            return ErrorClass.PARSE
    except ImportError:
        pass

    # Default to transient for unknown errors (safe side)
    return ErrorClass.TRANSIENT


def is_retryable_error(error_class: ErrorClass) -> bool:
    """Determine if an error class is retryable."""
    return error_class in (
        ErrorClass.TRANSIENT,
        ErrorClass.RATE_LIMITED,
    )


# -----------------------------------------------------------------------------
# Secret Resolution (ADR 0005)
# -----------------------------------------------------------------------------


def resolve_secret(source_id: str) -> str | None:
    """Resolve a secret for a source by its ID.

    Per ADR 0005, secrets are resolved from:
    1. Environment variable: JOBINDEX_SECRET_<source_id_with_dashes_as_underscores>
    2. File: $JOBINDEX_SECRET_DIR/<source_id> (mode 0600)

    Args:
        source_id: UUID of the source.

    Returns:
        The secret string, or None if not found.
    """
    # Environment variable
    env_key = f"JOBINDEX_SECRET_{source_id.replace('-', '_')}"
    secret = os.environ.get(env_key)
    if secret:
        return secret

    # File-based secret
    secret_dir = os.environ.get("JOBINDEX_SECRET_DIR")
    if secret_dir:
        secret_path = os.path.join(secret_dir, source_id)
        try:
            with open(secret_path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            pass

    return None


def inject_secret_headers(headers: dict[str, str], secret: str | None) -> dict[str, str]:
    """Inject secret into headers if provided.

    This is a simple Bearer token injection. Connectors needing different
    auth schemes should handle it themselves using the resolved secret.
    """
    if secret:
        headers = dict(headers)  # Copy
        headers.setdefault("Authorization", f"Bearer {secret}")
    return headers


# -----------------------------------------------------------------------------
# Structured Logging with Redaction
# -----------------------------------------------------------------------------


class RedactingFilter(logging.Filter):
    """Logging filter that redacts sensitive information from log records."""

    # Patterns to redact in log messages
    # Matches sensitive query parameters: token=value, api_key=value, etc.
    # Handles both ?param=value and &param=value, and also bare param=value at start
    URL_SECRET_PATTERN = re.compile(
        r"([?&]?)(token|key|secret|api_key|auth|password|access_token|refresh_token)=([^&\s]+)",
        re.IGNORECASE,
    )
    # Matches sensitive headers: Authorization: value, Cookie: value, etc.
    # Matches header name, colon, optional whitespace, then the rest of the line/value
    HEADER_SECRET_PATTERN = re.compile(
        r"(Authorization|Cookie|Set-Cookie|X-Api-Key|X-Auth-Token|Proxy-Authorization):\s*(.+?)(?:\s*,\s*|\s*$)",
        re.IGNORECASE,
    )

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact secrets in the log message
        if isinstance(record.msg, str):
            # Redact query parameters in URLs: keep the prefix (?&)param=, replace value
            record.msg = self.URL_SECRET_PATTERN.sub(r"\1\2=***REDACTED***", record.msg)
            # Redact sensitive headers: keep header name, replace value
            record.msg = self.HEADER_SECRET_PATTERN.sub(r"\1: ***REDACTED***", record.msg)

        # Redact in args if present
        if record.args:
            new_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    arg = self.URL_SECRET_PATTERN.sub(r"\1\2=***REDACTED***", arg)
                    arg = self.HEADER_SECRET_PATTERN.sub(r"\1: ***REDACTED***", arg)
                new_args.append(arg)
            record.args = tuple(new_args)

        return True


def setup_connector_logging(logger_name: str = "jobindex.connectors") -> logging.Logger:
    """Set up a logger with redaction filter for connector operations."""
    logger = logging.getLogger(logger_name)
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger


@dataclass
class LogContext:
    """Structured logging context for a connector operation."""
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    platform_key: str = ""
    tenant_key: str = ""
    source_id: str = ""
    operation: str = ""  # "list_page" | "fetch_detail"
    attempt: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "platform_key": self.platform_key,
            "tenant_key": self.tenant_key,
            "source_id": self.source_id,
            "operation": self.operation,
            "attempt": self.attempt,
        }


# -----------------------------------------------------------------------------
# Retry Policy
# -----------------------------------------------------------------------------

# Maximum delay cap for Retry-After header (seconds)
MAX_RETRY_AFTER = 300  # 5 minutes


def parse_retry_after(value: str) -> float | None:
    """Parse HTTP Retry-After header value.

    Supports two formats per RFC 7231:
    1. Integer seconds: "120"
    2. HTTP-date: "Wed, 21 Oct 2015 07:28:00 GMT"

    Args:
        value: The Retry-After header value.

    Returns:
        Delay in seconds, or None if parsing fails.
    """
    value = value.strip()
    if not value:
        return None

    # Try integer seconds first
    try:
        seconds = int(value)
        return max(0.0, float(seconds))
    except ValueError:
        pass

    # Try HTTP-date format
    try:
        from email.utils import parsedate_to_datetime
        retry_date = parsedate_to_datetime(value)
        if retry_date is not None:
            now = datetime.now(UTC)
            delay = (retry_date - now).total_seconds()
            return max(0.0, delay)
    except (ValueError, TypeError):
        pass

    return None


def _get_retry_after_delay(exc: BaseException, max_delay: float = MAX_RETRY_AFTER) -> float | None:
    """Extract Retry-After delay from an HTTPStatusError exception.

    Args:
        exc: The exception to check for Retry-After header.
        max_delay: Maximum allowed delay in seconds.

    Returns:
        Delay in seconds if Retry-After is present and valid, None otherwise.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return None

    if exc.response is None:
        return None

    retry_after = exc.response.headers.get("Retry-After")
    if not retry_after:
        return None

    delay = parse_retry_after(retry_after)
    if delay is None:
        return None

    # Cap at maximum allowed delay
    return min(delay, max_delay)


def create_retry_wait_strategy(
    base_wait: float = 1.0,
    max_wait: float = 30.0,
    jitter: float = 0.5,
) -> Callable[[tenacity.RetryCallState], float]:
    """Create a wait strategy that respects Retry-After header for 429 responses.

    For 429 responses with valid Retry-After header, uses the server-specified delay
    (capped at max_wait). For other retryable errors, uses exponential backoff with jitter.
    """
    def wait_fn(retry_state: tenacity.RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None

        # Check for Retry-After on 429
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            if exc.response.status_code == 429:
                retry_after_delay = _get_retry_after_delay(exc)
                if retry_after_delay is not None:
                    return min(retry_after_delay, max_wait)

        # Default: exponential backoff with jitter
        return wait_exponential_jitter(initial=base_wait, max=max_wait, jitter=jitter)(retry_state)

    return wait_fn


# Custom retry condition that uses our classifier
def retry_if_transient_or_rate_limited(retry_state: tenacity.RetryCallState) -> bool:
    """Tenacity retry condition using our error classifier."""
    if retry_state.outcome is None or retry_state.outcome.exception() is None:
        return False
    exc = retry_state.outcome.exception()
    error_class = classify_error(exc)
    return is_retryable_error(error_class)


def create_connector_retry_policy(
    max_attempts: int = DEFAULT_MAX_RETRIES,
    base_wait: float = 1.0,
    max_wait: float = 30.0,
    jitter: float = 0.5,
) -> tenacity.AsyncRetrying:
    """Create a configured tenacity AsyncRetrying object for connector operations.

    Retries on:
    - Transient network errors (timeout, connection error)
    - HTTP 5xx errors
    - HTTP 429 (rate limited) - respects Retry-After header

    Does not retry on:
    - HTTP 4xx errors (except 429)
    - Parsing errors
    """
    retry_logger = logging.getLogger("jobindex.connectors.retry")
    return tenacity.AsyncRetrying(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=create_retry_wait_strategy(base_wait, max_wait, jitter),
        retry=retry_if_transient_or_rate_limited,
        before_sleep=before_sleep_log(retry_logger, logging.WARNING),
        after=after_log(retry_logger, logging.INFO),
    )


# -----------------------------------------------------------------------------
# ConnectorContext
# -----------------------------------------------------------------------------


@dataclass
class ConnectorConfig:
    """Configuration for a ConnectorContext instance."""
    platform_key: str
    tenant: Tenant
    source_id: str
    rate_limit_config: RateLimitConfig | None = None
    max_retries: int = DEFAULT_MAX_RETRIES
    timeout_s: float | None = None
    custom_headers: dict[str, str] = field(default_factory=dict)
    # Retry policy configuration
    retry_base_wait: float = 1.0
    retry_max_wait: float = 30.0
    retry_jitter: float = 0.5


class ConnectorContext:
    """Async context manager for connector HTTP operations.

    Manages:
    - httpx.AsyncClient lifecycle with connection pooling
    - Rate limiting per host
    - Retry policies with exponential backoff
    - Error classification
    - Structured logging with correlation IDs and secret redaction
    - Secret resolution per ADR 0005
    """

    def __init__(self, config: ConnectorConfig):
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = RateLimiter()
        self._retry_policy = create_connector_retry_policy(
            max_attempts=config.max_retries,
            base_wait=config.retry_base_wait,
            max_wait=config.retry_max_wait,
            jitter=config.retry_jitter,
        )
        self._log_context = LogContext(
            platform_key=config.platform_key,
            tenant_key=config.tenant.tenant_key,
            source_id=config.source_id,
        )
        self._logger = setup_connector_logging(f"jobindex.connectors.{config.platform_key}")

    async def __aenter__(self) -> ConnectorContext:
        settings = get_settings()

        # Resolve secret for this source
        secret = resolve_secret(self.config.source_id)

        # Build default headers
        default_headers = {
            "User-Agent": settings.require_user_agent(),
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        }
        default_headers.update(self.config.custom_headers)
        default_headers = inject_secret_headers(default_headers, secret)

        # Configure timeout
        timeout = httpx.Timeout(
            timeout=self.config.timeout_s or settings.http_timeout_s,
            connect=min(10.0, self.config.timeout_s or settings.http_timeout_s),
            read=self.config.timeout_s or settings.http_timeout_s,
            write=10.0,
            pool=5.0,
        )

        # Configure connection limits
        limits = httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        )

        self._client = httpx.AsyncClient(
            headers=default_headers,
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
            http2=True,
        )

        self._logger.info(
            "ConnectorContext initialized",
            extra=self._log_context.to_dict(),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

        self._logger.info(
            "ConnectorContext closed",
            extra=self._log_context.to_dict(),
        )

    @property
    def client(self) -> httpx.AsyncClient:
        """Get the HTTP client, raising if not initialized."""
        if self._client is None:
            raise RuntimeError(
                "ConnectorContext not initialized. "
                "Use 'async with' or call __aenter__."
            )
        return self._client

    @property
    def log_context(self) -> LogContext:
        return self._log_context

    def with_operation(self, operation: str, attempt: int = 1) -> LogContext:
        """Create a log context for a specific operation."""
        ctx = LogContext(
            correlation_id=self._log_context.correlation_id,
            platform_key=self._log_context.platform_key,
            tenant_key=self._log_context.tenant_key,
            source_id=self._log_context.source_id,
            operation=operation,
            attempt=attempt,
        )
        return ctx

    async def request(
        self,
        method: str,
        url: str,
        *,
        operation: str = "request",
        attempt: int = 1,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute an HTTP request with rate limiting, retries, and logging."""
        log_ctx = self.with_operation(operation, attempt)

        # Rate limiting
        rate_config = self.config.rate_limit_config

        async def _do_request() -> httpx.Response:
            async with self._rate_limiter.acquire(url, rate_config):
                self._logger.debug(
                    "HTTP request: %s %s",
                    method.upper(),
                    url,
                    extra=log_ctx.to_dict(),
                )
                start_time = time.monotonic()
                client = self._client
                assert client is not None, "Client not initialized"
                try:
                    response = await client.request(method, url, **kwargs)
                    duration_ms = int((time.monotonic() - start_time) * 1000)

                    self._logger.info(
                        "HTTP response: %s %s -> %d (%dms)",
                        method.upper(),
                        url,
                        response.status_code,
                        duration_ms,
                        extra={
                            **log_ctx.to_dict(),
                            "http_status": response.status_code,
                            "duration_ms": duration_ms,
                        },
                    )
                    return response
                except Exception as e:
                    duration_ms = int((time.monotonic() - start_time) * 1000)
                    error_class = classify_error(e)
                    self._logger.warning(
                        "HTTP error: %s %s -> %s (%dms) [%s]",
                        method.upper(),
                        url,
                        type(e).__name__,
                        duration_ms,
                        error_class.value,
                        extra={
                            **log_ctx.to_dict(),
                            "error_class": error_class.value,
                            "duration_ms": duration_ms,
                        },
                    )
                    raise

        # Execute with retry
        try:
            return await self._retry_policy(_do_request)
        except tenacity.RetryError as e:
            # Retry exhausted, re-raise the last exception
            if e.last_attempt and e.last_attempt.failed:
                exc = e.last_attempt.exception()
                if exc is not None:
                    raise exc from None
            raise

    async def get(self, url: str, *, operation: str = "get", **kwargs: Any) -> httpx.Response:
        """Convenience method for GET requests."""
        return await self.request("GET", url, operation=operation, **kwargs)

    async def post(self, url: str, *, operation: str = "post", **kwargs: Any) -> httpx.Response:
        """Convenience method for POST requests."""
        return await self.request("POST", url, operation=operation, **kwargs)


# -----------------------------------------------------------------------------
# Helper to create ConnectorContext from source row
# -----------------------------------------------------------------------------


async def create_connector_context(
    platform_key: str,
    tenant: Tenant,
    source_id: str,
    rate_limit_rps: float = 0.5,
    max_concurrency: int = 1,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout_s: float | None = None,
    custom_headers: dict[str, str] | None = None,
    retry_base_wait: float = 1.0,
    retry_max_wait: float = 30.0,
    retry_jitter: float = 0.5,
) -> ConnectorContext:
    """Factory function to create a ConnectorContext with source-derived settings."""
    config = ConnectorConfig(
        platform_key=platform_key,
        tenant=tenant,
        source_id=source_id,
        rate_limit_config=RateLimitConfig(
            requests_per_second=max(0.1, rate_limit_rps),
            max_concurrency=max(1, max_concurrency),
            burst_allowance=max(1, int(rate_limit_rps * 2)),
        ),
        max_retries=max_retries,
        timeout_s=timeout_s,
        custom_headers=custom_headers or {},
        retry_base_wait=retry_base_wait,
        retry_max_wait=retry_max_wait,
        retry_jitter=retry_jitter,
    )
    return ConnectorContext(config)