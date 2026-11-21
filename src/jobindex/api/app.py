"""FastAPI application: liveness and readiness only in S1.

`/healthz` answers "is this process alive" and never touches the database — a liveness
probe that fails on a database blip restarts a healthy process.

`/readyz` round-trips the database and returns 503 when it cannot. The probe is a
dependency so tests can substitute a failure without a database.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Response, status
from fastapi.responses import JSONResponse

from jobindex import __version__
from jobindex.api.monitoring import router as monitoring_router
from jobindex.db.session import check_database
from jobindex.settings import get_settings

logger = logging.getLogger(__name__)

DbProbe = Callable[[], Awaitable[None]]


def get_db_probe() -> DbProbe:
    return check_database


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title="JobIndex",
        version=__version__,
        description="Deduplicated, current, auditable job index.",
    )

    app.include_router(monitoring_router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", tags=["ops"])
    async def readyz(
        response: Response,
        probe: Annotated[DbProbe, Depends(get_db_probe)],
    ) -> Any:
        try:
            await probe()
        except Exception as exc:
            logger.warning("readiness probe failed: %s", exc)
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "detail": type(exc).__name__},
            )
        return {"status": "ready", "database": "ok"}

    return app


app = create_app()
