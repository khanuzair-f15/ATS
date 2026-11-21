"""Vercel serverless function: claim and process a batch of extraction tasks.

Integrates with the existing FastAPI application in ``jobindex.api.app``. The
Vercel ``handler`` proxies each incoming request to the ASGI app (which exposes
``POST /api/extract`` and the ``/api/monitoring/*`` endpoints), so there is a
single source of truth for routing, authentication and business logic — both
locally (via uvicorn) and on Vercel.

Supabase Cron (pg_cron) is the single scheduler: it dispatches due sources into
``serverless_task_queue`` and triggers this function via pg_net, so indexing
continues while the local computer is off. No local-computer dependency.

The database URL is read from ``JOBINDEX_DATABASE_URL`` (Supabase pooled
connection string). All protected endpoints require the ``JOBINDEX_CRON_SECRET``
bearer token (fail-closed when the secret is missing or empty).
"""

from __future__ import annotations

from typing import Any

from httpx import ASGITransport, AsyncClient

from jobindex.api.app import app  # re-exported as module-level ASGI entrypoint

_app: Any = app


def _get_app() -> Any:
    """Return the cached FastAPI application (same instance as jobindex.api.app:app)."""
    return _app


def _build_url(request: Any) -> str:
    """Reconstruct an absolute URL from a Vercel-like request object."""
    url = getattr(request, "url", None)
    if url:
        return str(url)

    path = getattr(request, "path", "/") or "/"
    if not path.startswith("/"):
        path = "/" + path
    query = getattr(request, "query", None) or {}
    parts: list[str] = []
    if isinstance(query, dict):
        for key, value in query.items():
            parts.append(f"{key}={value}")
    elif isinstance(query, (list, tuple)):
        for key, value in query:
            parts.append(f"{key}={value}")
    qs = "&".join(parts)
    return "http://testserver" + path + (f"?{qs}" if qs else "")


def _build_headers(request: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    raw = getattr(request, "headers", None) or {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            headers[str(key)] = str(value)
    elif isinstance(raw, (list, tuple)):
        for key, value in raw:
            headers[str(key)] = str(value)
    else:
        get = getattr(raw, "get", None)
        if callable(get):
            for key in ("authorization", "content-type", "x-correlation-id"):
                value = get(key)
                if value is not None:
                    headers[key] = str(value)
    return headers


async def _read_body(request: Any) -> bytes:
    raw = getattr(request, "body", None)
    if raw is None:
        return b""
    if hasattr(raw, "read"):
        try:
            return raw.read() or b""
        except Exception:
            return b""
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        return raw.encode("utf-8")
    return b""


async def handler(request: Any) -> dict[str, Any]:
    """Vercel Python serverless function entry point (async).

    Proxies the request to the cached FastAPI app and returns its response.
    Authentication (cron-secret bearer, fail-closed) is enforced by the app's
    routes, not duplicated here.
    """
    app = _get_app()
    method = getattr(request, "method", "GET") or "GET"
    url = _build_url(request)
    headers = _build_headers(request)
    body = await _read_body(request)

    # Strip hop-by-hop / Vercel-internal headers that should not be forwarded.
    headers.pop("host", None)

    async with AsyncClient(transport=ASGITransport(app=app)) as client:
        response = await client.request(
            method, url, headers=headers, content=body
        )

    return {
        "statusCode": response.status_code,
        "headers": {k: v for k, v in response.headers.items()
                    if k.lower() not in ("content-length", "transfer-encoding")},
        "body": response.text,
    }
