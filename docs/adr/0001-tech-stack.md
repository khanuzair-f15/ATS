# ADR 0001 — Technology stack

- **Status:** Accepted (stakeholder decision, evidence recorded below)
- **Date:** 2026-11-13
- **Deciders:** project owner (stack preference), engineering (evaluation)
- **Supersedes:** none
- **Affects:** every step in `plans/jobindex-blueprint.md`

## Context

The system must implement **78 source connectors**, each of which is a small,
hostile-input parser: an HTTP call, a payload shape that changes without notice, and
a normalization step into one canonical record. Around that sit a scheduler, a
dedup engine, a search API and a minimal UI.

The dominant cost is therefore **connector authoring and maintenance volume**, not
request throughput. At the target of ~500k active jobs refreshed every 6–24h, the
system performs roughly 1–3 million HTTP requests per day and serves a low-QPS,
read-heavy search API. Nothing here is CPU-bound, concurrency-extreme, or
latency-critical at the microsecond level.

Two constraints from the brief shape the decision:

1. **Single region, one Postgres, one worker fleet.** No distributed systems.
2. **Handoff matters.** A new team must be able to add a connector in an afternoon.

## Options considered

### A — TypeScript / Node
- Fastify or Express, Postgres via `pg`/Kysely, Playwright, Postgres-backed queue.
- **Strengths:** one language across connectors, API and UI; excellent Playwright
  integration (same runtime as the browser driver); huge hiring pool for web work;
  JSON manipulation is ergonomic.
- **Weaknesses:** HTML parsing is the weakest part of the Node ecosystem
  (cheerio is fine, but malformed real-world markup is handled better by
  libxml2-based parsers); no first-class equivalent to `lxml`/`selectolax`;
  type gymnastics around heterogeneous scraped payloads add noise per connector;
  async error surfaces are easy to leave unhandled at 78 call sites.

### B — Python / FastAPI
- FastAPI + SQLAlchemy 2 (async) + Alembic; `httpx`; `selectolax`/`lxml` for DOM;
  Playwright for headless-only sources; Postgres-backed queue via
  `FOR UPDATE SKIP LOCKED` (see ADR 0003).
- **Strengths:** best-in-class HTML/XML parsing and encoding repair, which is
  exactly what 78 connectors do; the scraping ecosystem (`httpx`, `tenacity`,
  `rapidfuzz`, `dateparser`, `price-parser`) removes a lot of per-connector code;
  FastAPI gives OpenAPI generation for free, matching the API-first requirement;
  `rapidfuzz` is a strong fit for the dedup evaluation in ADR 0002; readable
  connectors lower the handoff barrier for a maintenance team.
- **Weaknesses:** two languages if the UI ever becomes a SPA (mitigated by
  server-rendered Jinja, per S4); GIL means CPU-heavy dedup work needs process
  workers (acceptable at this scale); Python packaging discipline is required to
  keep 78 connectors from drifting (mitigated by the SDK in S2).

### C — Go
- `net/http` + `colly`/`chromedp`, Postgres via `pgx`.
- **Strengths:** single static binary; best operational story; trivial concurrency;
  no runtime version drift on the handoff host.
- **Weaknesses:** highest per-connector line count for heterogeneous JSON/HTML
  payloads; no dynamic typing means a struct per platform and a compile cycle per
  iteration; `chromedp` is harder to operate than Playwright; smallest pool of
  engineers who will happily add the 79th connector.

## Decision

**Option B — Python 3.12 + FastAPI + Postgres 16 + Playwright (headless-only).**

The stakeholder selected Python; the evaluation above supports it. The deciding
factor is that 78 connectors are parsing problems, and the Python parsing and
scraping ecosystem is materially better than the alternatives. FastAPI's automatic
OpenAPI output also satisfies the API-contract-first requirement without extra
tooling.

Explicitly **not** chosen:
- **A (TypeScript)** — the one-language and Playwright advantages are real, but they
  buy less than superior HTML parsing across 78 connectors.
- **C (Go)** — operationally the best and economically the worst for this workload.

## Supporting decisions

| Concern | Choice | ADR |
|---|---|---|
| Job queue | Postgres `FOR UPDATE SKIP LOCKED` + `asyncio` workers | 0003 |
| Search | Postgres FTS (`tsvector` + GIN) + `pg_trgm` | 0004 |
| Dedup | Blocking keys + `rapidfuzz` + trigram/MinHash shingles | 0002 |
| HTTP | `httpx` (async, HTTP/2, timeouts, retries via `tenacity`) | — |
| HTML | `selectolax` primary, `lxml` fallback | — |
| Migrations | Alembic, forward-only in production | — |
| UI | Jinja server-rendered, no SPA build step | S4 |
| Tests | `pytest` + `pytest-asyncio`, fixtures replayed offline | S1, S2 |

## Consequences

**Positive**
- Connector code is short and reviewable; the SDK template does most of the work.
- OpenAPI is generated from the same code that serves traffic — no drift.
- One datastore (Postgres) covers data, queue, and search: nothing extra to operate.
- Dedup evaluation tooling (`rapidfuzz`, `datasketch`) is readily available.

**Negative**
- CPU-bound dedup passes need a process pool or a batch job; they must not run in
  the request path.
- Two-language risk if the UI outgrows Jinja. Accepted: the brief asks for a minimal
  working search UI, not a product frontend.
- Python's async footguns (unawaited coroutines, blocking calls in the loop) are a
  real hazard across 78 connectors; mitigated by a lint rule set and the shared
  `ConnectorContext` that owns all I/O.

## Reversal cost

**Moderate, and bounded by design.** The connector interface (S2) is a `Protocol`
with transport injected via `ConnectorContext`. Connector logic touches no database
and no framework. A move to TypeScript or Go would rewrite:

- the connector implementations (~78 files, mechanical),
- the scheduler and queue claim logic (~1 module),
- the API layer (~4 routers).

It would **not** require changes to the schema, the dedup strategy, the search
index design, the fixture format, or the evaluation methodology — those decisions
are recorded independently in ADRs 0002–0004 precisely so a runtime change does not
reopen them.

Estimated cost: 2–3 weeks for a small team, dominated by re-capturing connector
goldens. This is acceptable because the alternative — deferring the decision — costs
more than making it.

## Verification

- `docs/schema.md` is written in Postgres-native terms, not ORM terms, so it survives
  a runtime swap.
- `docs/platforms.md` classifies platforms by transport, not by library.
- A stack change would invalidate S1–S4 only; S5 onward are runtime-specific but
  thin. The plan's step boundaries were drawn with this in mind.
