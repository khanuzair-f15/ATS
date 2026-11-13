"""JobIndex — deduplicated, current, auditable job index.

Package boundaries (S1):

    connectors/  fetch + parse only. No database access, no closure decisions.
    normalize/   raw posting -> canonical fields. Pure functions.
    db/          SQLAlchemy models, session, migrations support.
    api/         FastAPI read API.
    scheduler/   queue claim/complete/reclaim (S5).
    dedup/       clustering (S9).
    web/         server-rendered search UI (S11+).
"""

__version__ = "0.1.0"
