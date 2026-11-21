# Connector Registry and Coverage Evidence

Coverage is evidence, not assertion. A connector file on disk proves nothing by itself.
The registry proves that code can be constructed, the CI artifact proves that its offline
tests ran, and PostgreSQL proves that a real tenant produced real jobs.

This document defines the registry contract, the four coverage states, the evidence each
state requires, the CI artifact format, and the `jobindex coverage` CLI.

---

## 1. Registry: explicit registration

`jobindex.connectors.registry.ConnectorRegistry` maps a platform key to a factory that
builds a `Connector` from a `ConnectorContext`:

```python
from jobindex.connectors.base import Connector
from jobindex.connectors.context import ConnectorContext
from jobindex.connectors.registry import register_connector


@register_connector("acme")
def create_acme(context: ConnectorContext) -> Connector:
    return AcmeConnector(context)
```

Rules enforced at runtime:

| Situation | Result |
|---|---|
| Empty, whitespace-padded, or non-string key | `ValueError` |
| Duplicate key | `ValueError` |
| Non-callable factory | `TypeError` |
| Unknown key on `get()` | `KeyError`, listing the registered keys |
| Factory returns an object missing `platform_key`, `transport`, `completeness`, `list_page`, or `fetch_detail` | `TypeError` |
| Connector reports a different `platform_key` than its registration | `ValueError` |
| `list_platforms()` | sorted keys, deterministic |

Registration is **explicit**. The registry never walks `connectors/` and never imports a
package directory wholesale. This is deliberate:

- `_template/` is a scaffold, not a platform.
- `auth.py`, `context.py`, `extraction.py`, `errors.py`, `results.py`, `_utils.py`, and
  `base.py` are shared infrastructure.
- test modules and conftest files live outside the package.

A filesystem scan would register all of them and inflate coverage. Only an import-time
`@register_connector(...)` call creates an entry. Import the module that owns the
decorator from the application bootstrap; coverage membership is runtime state.

---

## 2. Coverage states

Priority is fixed: **`VERIFIED` > `TESTED` > `IMPLEMENTED` > `OUTSTANDING`**.

| State | Meaning | Required evidence |
|---|---|---|
| `verified` | Real postings indexed from a real tenant | Persisted successful, complete `fetch_run`; recorded tenant/source; at least one live job occurrence seen in that run |
| `tested` | Offline fixture suite passed in CI | Registered connector **and** fixture-backed test **and** CI evidence with `failed == 0` and `passed > 0` |
| `implemented` | Connector code exists and tests exist, but no CI pass is recorded | Registered connector **and** a platform-specific test module **and** captured fixture files |
| `outstanding` | Nothing proven yet | No state above; `coverage_reason` must be non-empty and names the blocker and owner |

Consequences of these definitions:

- **A file named `connectors/<key>.py` never establishes `implemented`.** The platform
  must be present in the runtime registry.
- **A registered connector without a fixture-backed test is still `outstanding`.**
  Registration alone proves constructibility, not correctness.
- **CI evidence without a registered, fixture-backed connector is ignored.** A platform
  cannot skip from `outstanding` to `tested` on a test report alone.
- **A failed or incomplete fetch never verifies.** `outcome = failure`, `outcome =
  partial`, `listing_complete = false`, `jobs_found = 0`, or a missing `finished_at`
  all leave the platform below `verified`.
- **A live occurrence verifies only its recording run.** Its stored run ID and source ID
  must match the run. A null run ID leaves legacy occurrences ineligible.

### 2.1 What `verified` queries

`load_db_evidence()` joins `source`, `fetch_run`, and `job_occurrence`, and accepts a row
only when all of the following hold:

- `fetch_run.outcome = 'success'`
- `fetch_run.finished_at IS NOT NULL`
- `fetch_run.listing_complete = true`
- `fetch_run.jobs_found > 0`
- `job_occurrence.is_live = true`
- `job_occurrence.fetch_run_id = fetch_run.id`
- `job_occurrence.source_id = fetch_run.source_id`

The listing timestamp describes when the posting was seen. Coverage uses the stored
liveness flag and imposes no additional time window on `last_seen_at`.

The source gives the platform key and tenant identifier. The qualifying run and
occurrence give `last_success_at` and `sample_job_id`. Tenants are counted by distinct
`source.tenant_key`.

### 2.2 Open failures

Failures do not block `verified` on their own, but they are always reported:

- `source.health` is `open` or `half_open`; or
- `source.consecutive_failures > 0`.

The report lists each affected tenant with its health value and failure count. A platform
can therefore be `verified` while a second tenant is failing, and the report says so.

---

## 3. CI test evidence

`tested` consumes a JSON artifact produced by CI, not a database table. Per-build test
results are build artifacts; adding a `test_result` table would couple coverage to schema
churn and duplicate the CI system of record.

Recommended storage:

- CI publishes `coverage-test-evidence.json` as a build artifact.
- The coverage job downloads that artifact and passes it with `--test-evidence`.
- The job summary links the CI run through the `report` field.

Schema version 1.0:

```json
{
  "schema_version": "2.0",
  "provenance": {
    "repository": "owner/repo",
    "commit_sha": "abc123def456",
    "workflow_run_id": 123456789,
    "workflow_name": "CI",
    "test_suite": "connectors"
  },
  "platforms": {
    "greenhouse": {
      "passed": 12,
      "failed": 0,
      "skipped": 1,
      "report": "https://github.com/owner/repo/actions/runs/123456789"
    }
  }
}
```

Validation is strict:

| Invalid input | Result |
|---|---|
| Unknown `schema_version` | `ValueError` |
| Missing `provenance` object | `ValueError` |
| Missing provenance fields (`repository`, `commit_sha`, `workflow_run_id`, `workflow_name`, `test_suite`) | `ValueError` |
| Invalid `workflow_run_id` (not positive integer) | `ValueError` |
| Missing `platforms` object | `ValueError` |
| Unknown platform key | `ValueError` |
| Negative or boolean count | `ValueError` |
| Non-string `report` | `ValueError` |
| Malformed JSON or unreadable file | `ValueError` |

A platform is `tested` only when `failed == 0` and `passed > 0`. Skipped tests are
reported but do not promote a platform.

**Provenance requirements**: The `provenance` object ties the evidence to a specific
CI execution. A manually fabricated JSON file without valid provenance will be rejected.
The `report` field should be a URL to the GitHub Actions run (or equivalent CI system)
for auditability.

---

## 4. CLI

```bash
python -m jobindex.cli coverage [options]
```

| Flag | Meaning |
|---|---|
| `--output PATH`, `-o PATH` | Markdown report path. Without `--wave`, the default is `docs/coverage.md`. With `--wave` and no `--output`, the report prints to stdout. |
| `--json PATH` | Write the versioned JSON report (`schema_version` `1.0`). |
| `--test-evidence PATH` | Load CI test evidence (schema v2.0 with provenance) used for the `tested` state. |
| `--wave WAVE` | Filter to one wave (`S3`, `S7`, `S8`, `S10`, `S11`, `S12`, `S13`). |
| `--update-platforms-md` | Regenerate only the `Coverage` column of `docs/platforms.md`. Requires database evidence; fails if database unavailable. |
| `--dry-run` | Print proposed content; write no file. |
| `--no-db` | Skip PostgreSQL evidence. Live-verification states remain below `verified`. Cannot be combined with `--update-platforms-md`. |

Examples:

```bash
# Regenerate the full report without a database (all unverified rows stay outstanding)
python -m jobindex.cli coverage --no-db

# Wave report with CI test evidence, printed to stdout
python -m jobindex.cli coverage --no-db --wave S3 \
  --test-evidence artifacts/coverage-test-evidence.json

# Write both reports and refresh the inventory Coverage column
python -m jobindex.cli coverage \
  --test-evidence artifacts/coverage-test-evidence.json \
  --json docs/coverage.json \
  --update-platforms-md

# Inspect changes without touching the working tree
python -m jobindex.cli coverage --update-platforms-md --dry-run
```

If PostgreSQL is unreachable and `--update-platforms-md` is **not** used, the command
warns on stderr and continues without database evidence (all platforms stay below
`verified`). Passing `--no-db` skips the connection attempt entirely and is the correct
mode for offline CI and local documentation regeneration.

**Fail-closed behavior**: If `--update-platforms-md` is used and PostgreSQL is
unreachable, the command exits with code 3 and does **not** modify
`docs/platforms.md`. This prevents a transient database failure from silently
downgrading previously `verified` platforms. Use `--no-db` explicitly for offline
reporting; it cannot be combined with `--update-platforms-md`.

---

## 5. Determinism and inventory regeneration

Coverage output must be reproducible:

- The Markdown report contains no wall-clock timestamp. `Data as of` is the newest
  qualifying success, or the literal text `no successful live fetch recorded`.
- Platforms appear in inventory order; waves render in `S3, S7, S8, S10, S11, S12, S13`
  order.
- JSON is emitted with sorted keys and a stable platform order.
- `generated_at` is `null` unless a caller supplies it. The CLI does not invent one.
- Running the same command twice over the same evidence produces byte-identical files.

`--update-platforms-md` reads the existing file with newline translation disabled and
rewrites **only the final cell of numeric inventory rows**. Column widths, spacing, prose,
row order, line endings (LF or CRLF), and the trailing newline are preserved byte for
byte. The command fails instead of guessing when:

- an inventory row has no supplied coverage;
- coverage is supplied for a platform absent from the inventory;
- the same platform appears twice in the supplied coverage.

This prevents an accidentally filtered `--wave` run from rewriting the full inventory.

---

## 6. From outstanding to verified

1. Implement the connector against the `Connector` protocol.
2. Register it explicitly with `@register_connector("<platform-key>")`.
3. Add `tests/connectors/test_<platform-key>.py` and captured fixtures under
   `tests/fixtures/<platform-key>/`.
4. Run the offline suite in CI and publish `coverage-test-evidence.json` (schema v2.0
   with provenance).
5. Run the connector against at least two real tenants so a successful, complete
   `fetch_run` and live `job_occurrence` rows **with `fetch_run_id` set** exist.
6. Run `python -m jobindex.cli coverage --test-evidence ... --update-platforms-md`.
   The row moves to `verified` only when the database evidence satisfies §2.1:
   a `fetch_run` with `outcome = 'success'`, `listing_complete = true`,
   `jobs_found > 0`, `finished_at IS NOT NULL`, and at least one live
   `job_occurrence` whose `fetch_run_id` points to that run.

A platform that cannot reach `verified` stays `outstanding` with a reason that names the
blocker and the owner. Excluding it is not an option (ADR 0005 §A).
