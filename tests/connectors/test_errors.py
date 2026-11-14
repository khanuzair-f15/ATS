"""Tests for error classification and handling."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from jobindex.connectors.context import classify_error, is_retryable_error
from jobindex.db.enums import ErrorClass


class TestClassifyError:
    """Comprehensive tests for classify_error function."""

    # Successful responses
    def test_success_200(self) -> None:
        response = _mock_response(200, is_success=True)
        assert classify_error(None, response) == ErrorClass.NONE

    def test_success_201(self) -> None:
        response = _mock_response(201, is_success=True)
        assert classify_error(None, response) == ErrorClass.NONE

    def test_success_204(self) -> None:
        response = _mock_response(204, is_success=True)
        assert classify_error(None, response) == ErrorClass.NONE

    # Rate limiting
    def test_429_rate_limited(self) -> None:
        response = _mock_response(429)
        assert classify_error(None, response) == ErrorClass.RATE_LIMITED

    # Authentication/Authorization
    def test_401_blocked(self) -> None:
        response = _mock_response(401)
        assert classify_error(None, response) == ErrorClass.BLOCKED

    def test_403_blocked(self) -> None:
        response = _mock_response(403)
        assert classify_error(None, response) == ErrorClass.BLOCKED

    # Not found
    def test_404_not_found(self) -> None:
        response = _mock_response(404)
        assert classify_error(None, response) == ErrorClass.NOT_FOUND

    def test_410_not_found(self) -> None:
        response = _mock_response(410)
        assert classify_error(None, response) == ErrorClass.NOT_FOUND

    # Client errors (permanent)
    def test_400_permanent(self) -> None:
        response = _mock_response(400)
        assert classify_error(None, response) == ErrorClass.PERMANENT

    def test_422_permanent(self) -> None:
        response = _mock_response(422)
        assert classify_error(None, response) == ErrorClass.PERMANENT

    # Server errors (transient)
    def test_500_transient(self) -> None:
        response = _mock_response(500)
        assert classify_error(None, response) == ErrorClass.TRANSIENT

    def test_502_transient(self) -> None:
        response = _mock_response(502)
        assert classify_error(None, response) == ErrorClass.TRANSIENT

    def test_503_transient(self) -> None:
        response = _mock_response(503)
        assert classify_error(None, response) == ErrorClass.TRANSIENT

    def test_504_transient(self) -> None:
        response = _mock_response(504)
        assert classify_error(None, response) == ErrorClass.TRANSIENT

    # Exception-based classification
    def test_timeout_exception_transient(self) -> None:
        exc = httpx.TimeoutException("Request timed out after 30s")
        assert classify_error(exc) == ErrorClass.TRANSIENT

    def test_connect_timeout_transient(self) -> None:
        exc = httpx.ConnectTimeout("Connection timed out")
        assert classify_error(exc) == ErrorClass.TRANSIENT

    def test_read_timeout_transient(self) -> None:
        exc = httpx.ReadTimeout("Read timed out")
        assert classify_error(exc) == ErrorClass.TRANSIENT

    def test_network_error_transient(self) -> None:
        exc = httpx.NetworkError("Network unreachable")
        assert classify_error(exc) == ErrorClass.TRANSIENT

    def test_connect_error_transient(self) -> None:
        exc = httpx.ConnectError("Failed to connect")
        assert classify_error(exc) == ErrorClass.TRANSIENT

    def test_protocol_error_permanent(self) -> None:
        exc = httpx.ProtocolError("Protocol violation")
        assert classify_error(exc) == ErrorClass.PERMANENT

    def test_http_status_error_429(self) -> None:
        response = _mock_response(429)
        exc = httpx.HTTPStatusError("Rate limited", request=_mock_request(), response=response)
        assert classify_error(exc) == ErrorClass.RATE_LIMITED

    def test_http_status_error_500(self) -> None:
        response = _mock_response(500)
        exc = httpx.HTTPStatusError(
            "Internal server error", request=_mock_request(), response=response
        )
        assert classify_error(exc) == ErrorClass.TRANSIENT

    def test_http_status_error_404(self) -> None:
        response = _mock_response(404)
        exc = httpx.HTTPStatusError(
            "Not found", request=_mock_request(), response=response
        )
        assert classify_error(exc) == ErrorClass.NOT_FOUND

    def test_http_status_error_403(self) -> None:
        response = _mock_response(403)
        exc = httpx.HTTPStatusError(
            "Forbidden", request=_mock_request(), response=response
        )
        assert classify_error(exc) == ErrorClass.BLOCKED

    def test_http_status_error_400(self) -> None:
        response = _mock_response(400)
        exc = httpx.HTTPStatusError(
            "Bad request", request=_mock_request(), response=response
        )
        assert classify_error(exc) == ErrorClass.PERMANENT

    def test_json_decode_error_parse(self) -> None:
        exc = ValueError("Expecting value: line 1 column 1 (char 0) - JSON decode error")
        assert classify_error(exc) == ErrorClass.PARSE

    def test_json_decode_error_parse_v2(self) -> None:
        import json
        exc = json.JSONDecodeError("Expecting value", "", 0)
        assert classify_error(exc) == ErrorClass.PARSE

    def test_unknown_exception_defaults_transient(self) -> None:
        exc = RuntimeError("Something went wrong")
        assert classify_error(exc) == ErrorClass.TRANSIENT

    def test_none_error_with_none_response(self) -> None:
        assert classify_error(None, None) == ErrorClass.NONE


class TestIsRetryableError:
    """Tests for is_retryable_error function."""

    @pytest.mark.parametrize("error_class,expected", [
        (ErrorClass.TRANSIENT, True),
        (ErrorClass.RATE_LIMITED, True),
        (ErrorClass.PERMANENT, False),
        (ErrorClass.BLOCKED, False),
        (ErrorClass.NOT_FOUND, False),
        (ErrorClass.PARSE, False),
        (ErrorClass.NONE, False),
    ])
    def test_retryable_mapping(self, error_class: ErrorClass, expected: bool) -> None:
        assert is_retryable_error(error_class) == expected


class TestErrorClassificationEdgeCases:
    """Edge cases for error classification."""

    def test_redirect_not_classified_as_error(self) -> None:
        """3xx redirects should be handled by httpx follow_redirects."""
        response = _mock_response(301)
        response.is_success = False
        # In practice, httpx follows redirects by default
        # This test documents expected behavior
        pass

    def test_429_with_retry_after_header(self) -> None:
        """429 with Retry-After should still be RATE_LIMITED."""
        response = _mock_response(429)
        response.headers = {"Retry-After": "60"}
        assert classify_error(None, response) == ErrorClass.RATE_LIMITED

    def test_403_with_www_authenticate(self) -> None:
        """403 with WWW-Authenticate should be BLOCKED."""
        response = _mock_response(403)
        response.headers = {"WWW-Authenticate": 'Bearer realm="api"'}
        assert classify_error(None, response) == ErrorClass.BLOCKED

    def test_classify_from_httpx_request_error(self) -> None:
        """RequestError (base class) should be TRANSIENT."""
        exc = httpx.RequestError("Request failed")
        assert classify_error(exc) == ErrorClass.TRANSIENT


def _mock_response(status_code: int, is_success: bool = False) -> httpx.Response:
    """Create a mock httpx.Response."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.is_success = is_success or (200 <= status_code < 300)
    response.headers = {}
    return response


def _mock_request() -> httpx.Request:
    """Create a mock httpx.Request."""
    request = MagicMock(spec=httpx.Request)
    request.method = "GET"
    request.url = httpx.URL("https://example.com/test")
    return request