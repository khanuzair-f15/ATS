# Platform inventory and transport classification

> **INVENTORY STATUS: v1.0 — BRIEF-EXACT.**
>
> The 78 rows below are the brief's exact 58 ATS + 20 job-board-platform names, in the
> brief's order. The frozen manifest is `docs/brief-platforms.txt`; a checker
> (`scripts/verify_phase0.py`) asserts this table and that manifest agree **row for row,
> both directions**. Adding, renaming, dropping, or substituting a platform is a scope
> change and requires a `plans/MUTATIONS.md` entry.
>
> **Nothing in this file may be read as "implemented".** Every row is `outstanding` at
> Phase 0. A row becomes `implemented` when connector code merges, `tested` when its
> offline fixture suite passes, and `verified` only when a real run has indexed real jobs
> from a real tenant and a `fetch_run` row exists (S14 emits that evidence). The four
> states are defined in ADR 0005 §A.
>
> **`Coverage` is generated, never hand-edited.** From S2 onward the column is produced
> by `python -m jobindex.cli coverage` from the `platform` table plus `fetch_run`
> evidence. A test asserts this document and the `platform` table agree row-for-row, so a
> row cannot claim `verified` without a run behind it.

---

## 0. How to read the classification

| Class | What it means | Engineering cost per connector | Fragility |
|---|---|---|---|
| **C1** | Documented public structured API (JSON or XML) for the board, usually one request | Low — 1–2 requests, stable shape | Low |
| **C2** | Structured listing endpoint (JSON/XML/XHR) on a tenant-specific path, usually with an HTML detail page | Medium — tenant path templating, pagination contracts, POST bodies | Medium |
| **C3** | No documented structured source; DOM parsing or a real browser required | High — selectors, rendering, screenshots on failure | High |
| **C4** | Job-board software platform; one connector often covers many board instances | Low–medium — shared engine, but per-instance quirks | Medium |

There is **no `EX` class**. All 78 platforms are required (ADR 0005 §A). A platform that
turns out to be technically blocked stays in this table as `outstanding` with a named
reason; it does not leave the inventory.

**Unknown transport is C3.** If no structured path is documented for a platform, it is
classed C3 ("headless unless probing promotes it"). This is deliberate: a guessed C2
endpoint is worse than an honest C3.

`Wave` names the step in `plans/jobindex-blueprint.md` that owns the row. **Every row
has exactly one wave.** A row with no wave is unowned work and is a plan defect; a wave
that claims a row owned by another wave is a double-count. Wave membership is asserted by
`scripts/verify_phase0.py`.

**Request estimate convention:** `1 + ⌈N/100⌉ + N` per tenant per full sync, where `N`
= open roles at that tenant (one listing call, listing pages, one detail call per job).
C1 rows that return full content inline use `1`; board platforms use the engine's own
pagination.

**Coverage states:** `outstanding` (Phase 0 default: not started), `implemented`,
`tested`, `verified`. A row's Phase 0 reason is always "not started"; from S2 the
coverage report carries a specific reason and owner for every `outstanding` row.

---

## 1. ATS platforms (58)

| # | Platform | Family | Class | Wave | Discovery → detail | Auth | Req/sync | Risk | Coverage |
|---|---|---|---|---|---|---|---|---|---|
| 1 | ADP | ATS | C2 | S11 | `workforcenow.adp.com/.../staffing/v1/job-requisitions?cid=` → detail JSON | none | 1 + pages + N | M | outstanding |
| 2 | ApplicantPro | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 3 | Ashby | ATS | C1 | S7 | `api.ashbyhq.com/posting-api/job-board/{org}` | none | 1 | L | outstanding |
| 4 | Avature | ATS | C3 | S13 | `/search-jobs` HTML; some tenants expose XHR JSON — probe required | none | 1 + pages + N | H | outstanding |
| 5 | BambooHR | ATS | C2 | S8 | `{co}.bamboohr.com/careers/list` JSON → detail HTML | none | 1 + N | M | outstanding |
| 6 | BrassRing | ATS | C2 | S10 | `{co}.brassring.com` career-section JSON → detail | none | 1 + pages + N | H | outstanding |
| 7 | Breezy HR | ATS | C1 | S7 | `{co}.breezy.hr/json` | none | 1 | L | outstanding |
| 8 | CareerPlug | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 9 | Comeet | ATS | C1 | S7 | `comeet.co/careers-api/2.0/company/{uid}/positions` | token | 1 | M | outstanding |
| 10 | CSOD | ATS | C2 | S10 | tenant `/ux/...` + internal services JSON; probe required | none | 1 + pages + N | H | outstanding |
| 11 | Dayforce | ATS | C2 | S11 | tenant career-site JSON/XHR → detail | none | 1 + pages + N | H | outstanding |
| 12 | Deel | ATS | C2 | S8 | `{co}.deel.com` embedded JSON → detail | none | 1 + N | M | outstanding |
| 13 | Dover | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 14 | Eightfold AI | ATS | C3 | S13 | **TBD — no documented public path.** Probe per tenant before writing the connector | none | 1 + pages + N | H | outstanding |
| 15 | FirstStage | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 16 | Freshteam | ATS | C2 | S11 | documented API (`api.freshteam.com/api/v3/job_postings`, token); public page HTML | token | 1 + pages | M | outstanding |
| 17 | Gem.com | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 18 | GoHire | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 19 | Greenhouse | ATS | C1 | S3 | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | none | 1 | L | outstanding |
| 20 | HiBob | ATS | C2 | S8 | `{co}.careers.hibob.com` embedded JSON → detail | none | 1 + N | M | outstanding |
| 21 | HireBridge | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 22 | HireHive | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 23 | Hireology | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 24 | HiringThing | ATS | C2 | S11 | documented API (`api.hiringthing.com/v1/jobs`, token); public page HTML | token | 1 + pages | M | outstanding |
| 25 | iCIMS | ATS | C2 | S10 | `careers-{co}.icims.com/jobs/search` → `/jobs/{id}/job` JSON | none | 1 + pages + N | M | outstanding |
| 26 | iSolvedHire | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 27 | JazzHR | ATS | C3 | S13 | `{co}.applytojob.com` HTML listing → detail | none | 1 + pages + N | H | outstanding |
| 28 | Jobvite | ATS | C3 | S13 | `jobs.jobvite.com/{co}/search` HTML → detail | none | 1 + pages + N | M | outstanding |
| 29 | JOIN.com | ATS | C3 | S13 | `join.com/companies/{co}` HTML + JSON-LD → detail | none | 1 + pages + N | M | outstanding |
| 30 | Kula.ai | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 31 | Lever.co | ATS | C1 | S7 | `api.lever.co/v0/postings/{site}?mode=json` | none | 1 | L | outstanding |
| 32 | Manatal | ATS | C1 | S7 | `api.manatal.com/open/v3/career-page/{org}/jobs/` | none | 1 | M | outstanding |
| 33 | Oracle Recruiting | ATS | C2 | S10 | `/hcmRestApi/resources/latest/recruitingCEJobRequisitions` finder → detail | none | 1 + pages + N | M | outstanding |
| 34 | PageUp | ATS | C3 | S13 | `{host}/jobs` HTML listing → detail | none | 1 + pages + N | H | outstanding |
| 35 | Paradox | ATS | C3 | S13 | tenant career-site HTML listing → detail; probe required | none | 1 + pages + N | H | outstanding |
| 36 | Paycom | ATS | C2 | S11 | `{co}.paycomonline.net/v4/ats/web.php/jobs` JSON/XHR → detail | none | 1 + pages + N | M | outstanding |
| 37 | Paycor | ATS | C3 | S13 | `recruiting.paycor.com` HTML listing → detail | none | 1 + pages + N | H | outstanding |
| 38 | Paylocity | ATS | C2 | S11 | `recruiting.paylocity.com/Recruiting/Jobs/GetJobListings` POST → detail | none | 1 + pages + N | M | outstanding |
| 39 | Personio | ATS | C1 | S7 | `{co}.jobs.personio.com/xml` (full feed, includes descriptions) | none | 1 | L | outstanding |
| 40 | Phenom People | ATS | C2 | S10 | `{host}/api/apply/v2/jobs?domain=...` → detail | none | 1 + pages + N | M | outstanding |
| 41 | Pinpoint | ATS | C1 | S7 | `{co}.pinpointhq.com/postings.json` | none | 1 | L | outstanding |
| 42 | Polymer | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 43 | Recooty | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 44 | Recruitee | ATS | C1 | S7 | `{co}.recruitee.com/api/offers/` | none | 1 | L | outstanding |
| 45 | Rippling | ATS | C2 | S8 | `ats.rippling.com/{co}/jobs` embedded JSON → detail | none | 1 + N | M | outstanding |
| 46 | Rival | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 47 | SAP SuccessFactors | ATS | C2 | S10 | careers-site search → RMK job JSON → detail | none | 1 + pages + N | H | outstanding |
| 48 | SmartRecruiters | ATS | C1 | S7 | `api.smartrecruiters.com/v1/companies/{co}/postings` → detail | none | 1 + N/pages | L | outstanding |
| 49 | talentReef | ATS | C3 | S13 | `{co}.talentreef.com` HTML listing → detail | none | 1 + pages + N | H | outstanding |
| 50 | Taleo | ATS | C2 | S10 | `{tenant}.taleo.net/careersection/{site}/jobsearch.ftl` → REST job JSON; TBE/TEE variants probe per tenant | none | 1 + pages + N | H | outstanding |
| 51 | Teamtailor | ATS | C2 | S8 | public careers site embedded JSON → detail; documented API is token-gated | none | 1 + N | M | outstanding |
| 52 | Trakstar | ATS | C3 | S13 | `{co}.hire.trakstar.com` HTML listing → detail | none | 1 + pages + N | H | outstanding |
| 53 | TriNet | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 54 | UltiPro | ATS | C2 | S11 | `recruiting.ultipro.com/{co}/JobBoard/.../LoadSearchResults` POST → detail | none | 1 + pages + N | M | outstanding |
| 55 | WeRecruit | ATS | C3 | S13 | tenant careers HTML listing → detail HTML; probe required | none | 1 + pages + N | H | outstanding |
| 56 | Workable | ATS | C1 | S7 | `apply.workable.com/api/v1/widget/accounts/{sub}?details=true` | none | 1 | L | outstanding |
| 57 | Workday | ATS | C2 | S10 | `POST {host}/wday/cxs/{tenant}/{site}/jobs` → `.../job/{path}` | none | 1 + pages + N | M | outstanding |
| 58 | Zoho Recruit | ATS | C2 | S8 | careers-portal XHR JSON → detail | none | 1 + pages + N | M | outstanding |

**Distribution:** C1 = 11, C2 = 21, C3 = 26. (C4 is the board table.)

`BambooHR` is C2, not C1: its listing is a JSON document but the description requires the
HTML detail page. `Personio` is C1 because its documented XML feed carries the full
description in one request.

`Eightfold AI` carries no endpoint claim. Phenom and Eightfold were previously given the
same path; at most one of those is correct and we have verified neither. The
probe-before-build rule (§4) applies: capture a tenant request first.

`Freshteam` and `HiringThing` are C2 because a documented API exists; both are
credential-gated, so without a publisher token the row stays `outstanding` (ADR 0005 §D).
They are not silently downgraded to HTML — that is a probe decision recorded in §4.

---

## 1a. Wave ownership (must equal 78)

| Wave | Step | Rows | Count |
|---|---|---|---|
| Slice 1 | S3 | Greenhouse | 1 |
| Wave A — C1 ATS residual | S7 | Lever.co, Ashby, SmartRecruiters, Workable, Recruitee, Breezy HR, Pinpoint, Comeet, Personio, Manatal | 10 |
| Wave B — mid-market structured | S8 | Teamtailor, BambooHR, HiBob, Rippling, Zoho Recruit, Deel | 6 |
| Wave C — enterprise suites | S10 | Workday, SAP SuccessFactors, Taleo, Oracle Recruiting, iCIMS, Phenom People, CSOD, BrassRing | 8 |
| Wave D — payroll/PEO and token-gated APIs | S11 | ADP, Dayforce, UltiPro, Paycom, Paylocity, Freshteam, HiringThing | 7 |
| Wave E — board software platforms | S12 | all 20 board platforms (rows 59–78) | 20 |
| Wave F — HTML/headless residual | S13 | 26 C3 ATS rows | 26 |
| **Total** | | | **78** |

Wave counts: `S3 = 1, S7 = 10, S8 = 6, S10 = 8, S11 = 7, S12 = 20, S13 = 26`.
Class counts: `C1 = 11, C2 = 21, C3 = 26, C4 = 20`.

---

## 2. Job-board software platforms (20)

These are **multi-tenant job-board software platforms**, not consumer job boards. One
connector per platform should serve many board instances (tenants). LinkedIn, Indeed,
Glassdoor, ZipRecruiter, Monster, Dice, SimplyHired, Wellfound and YC are **not in the
brief** and are not rows here.

| # | Platform | Family | Class | Wave | Discovery → detail | Auth | Req/sync | Risk | Coverage |
|---|---|---|---|---|---|---|---|---|---|
| 59 | Niceboard | BOARD | C4 | S12 | `{board}.niceboard.co` public board; API path TBD — probe required | none | 1 + pages + N | M | outstanding |
| 60 | WP Job Manager | BOARD | C4 | S12 | `{board}/wp-json/wp/v2/job_listing` (WP REST; open when the site exposes it) → detail | none | 1 + pages + N | L | outstanding |
| 61 | JobKit | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 62 | Jobboardly | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 63 | Jobiqo | BOARD | C4 | S12 | TBD — enterprise board platform; probe per instance | none | TBD | M | outstanding |
| 64 | JBoard.io | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 65 | JobBoardFire | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 66 | SmartJobBoard | BOARD | C4 | S12 | TBD — public board HTML; customer API is key-gated | none | TBD | M | outstanding |
| 67 | JobBoard.io | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 68 | Wiley Madgex | BOARD | C4 | S12 | TBD — enterprise board platform; probe per instance | none | TBD | M | outstanding |
| 69 | Talenetic | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 70 | JobMount | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 71 | CareerSiteCloud | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 72 | JobBoard.com | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 73 | Artha | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 74 | jobboardsoftware.com | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 75 | jobBoardHQ | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 76 | Nodex | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 77 | eJobBoard | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |
| 78 | ejobsitesoftware.com | BOARD | C4 | S12 | TBD — probe the board engine before writing the connector | none | TBD | M | outstanding |

**Distribution:** C4 = 20.

`TBD — probe required` is a deliberate, honest value. The brief names the platform; it
does not document the transport. S12's first task is to capture one live board instance
per platform and replace `TBD` with the observed engine, in one commit per platform.

---

## 3. Projected deliverability (to be replaced by measured coverage in S14)

This table is a **forecast**, and it is deliberately pessimistic. It is the number the
approval gate is asking you to accept. It is not a delivery promise.

| Outcome | Count | Note |
|---|---|---|
| Structured sources expected working (C1/C2/C4) | 52 of 78 | 11 C1 + 21 C2 + 20 C4; some C2 resolve to no usable structured path |
| C2 demoted to C3 after probing | up to 4 | Probe-before-build rule (§4); they join Wave F rather than being hacked |
| C3 (HTML/headless) expected working | 0–6 of 26 | Expensive; each needs a written justification and a fragility note |
| Excluded on legal/technical grounds | **0** | No required platform may be excluded (ADR 0005 §A) |
| **Point forecast: platforms with live evidence** | **52 of 78** | Range **48–58**; zero exclusions |
| Residual, named but undelivered | 26 (range 20–30) | Every one listed explicitly in the coverage report with a reason |

The honest acquisition framing is: **78 platforms required, 52 expected with live
evidence, 26 named outstanding, 0 excluded.** Claiming 78/78 working would fail diligence
on the first spot-check; claiming a smaller scope would contradict the brief.

The forecast is read from this document by the coverage generator and compared with
measured output in S14; a large divergence is a planning finding, not a surprise.

---

## 4. Verification protocol (how a row changes state)

A row moves through the four states in order. It is promoted to `verified` only when all
five hold:

1. `connectors/<platform>.py` exists and implements the full Protocol (`implemented`).
2. Fixtures captured live for **≥2 distinct tenants** (or 1 with a recorded reason), and
   the offline replay suite passes (`tested`).
3. Golden replay test passes offline: `pytest tests/connectors/test_<platform>.py`.
4. A real run produced ≥1 job with all required fields, linked to a `fetch_run` id, from a
   named tenant (`verified`).
5. `docs/coverage.md` regenerated by `jobindex.cli coverage` and shows the row.

Steps 4 and 5 are why the coverage generator ships in **S2**, not S14: from the first
connector onward, `verified` has to be a generated fact. A human editing this table is
the failure mode the review named C9.

**Probe-before-build rule:** before writing a connector, capture one request and inspect
it. If the payload shape is not what this table claims, update this table first. A
connector built on a guessed endpoint is deleted, not patched.

**Demotion rule (C2 → C3):** if a platform turns out to have no usable structured source,
it moves to C3 and is deferred to S13 — it is not hacked into the C2 wave. The row's
`class` and `wave` change together, in one commit, with the probe evidence attached.

**Promotion rule (C3 → C2):** if a probe finds a documented, stable structured endpoint
for a C3 row, it is promoted to C2 and moves to the owning structured wave. Class and
wave change together, in one commit, with the captured request attached. A promotion
without captured evidence is not a promotion; it is a guess.

**Blocked-platform rule:** a platform that cannot be fetched without auth bypass,
paywall circumvention, or bot-defence evasion is **not** excluded. It stays in the
inventory as `outstanding` with the specific reason, a revisit date, and an owner. The
coverage report shows `verified + outstanding = 78`.

---

## 5. Legal, robots, and operational posture

### Absolute constraints (no exceptions)

- No authentication bypass, no paywalls, no logged-in sessions.
- No circumvention of CAPTCHAs, bot defences, or rate limits.
- Identifiable `User-Agent` with a contact URL on every request.
- `robots.txt` honoured for HTML/C3 sources; disallowed paths are not fetched.
- Public, unauthenticated job listings only.

Three rows carry `auth: token` (Comeet, Freshteam, HiringThing). Those tokens are the
publisher's own API credentials, resolved per ADR 0005 §D — they are not scraped from a
logged-in session, and a row whose token is unavailable stays `outstanding` rather than
being worked around.

### No exclusions

There is no excluded set. The brief requires all 78 platforms, and ADR 0005 makes
`outstanding` the only alternative to `verified`. A platform that is technically blocked
is reported as `outstanding` with its reason; it is never removed from the inventory and
never counted as delivered.

### Operational exposure

| Risk | Mitigation |
|---|---|
| IP blocking from C3 volume | Per-host concurrency 1, jittered delays, tiered refresh, browser pool reuse |
| Tenant path drift on C2 | Per-tenant config in `source.config` (never secrets), contract tests per tenant |
| Selector rot on C3 | Screenshot + DOM snapshot on failure, captured into `fetch_run` |
| Takedown request | Per-source disable flag; documented in runbook; no re-enable without review |
| A source dying silently | Circuit breaker surfaces `degraded` in `/v1/sources`; coverage report flags stale |
| A board platform with no documented engine | Probe first; if no structured path, the row is honestly C3 or stays `outstanding` |

### What this posture costs

Choosing structured-first means several C2/C3 platforms (Paycor, Avature, PageUp,
BrassRing, TriNet, CSOD) may resolve to "no usable structured endpoint" and end up in
the residual list. That is the accepted trade: a smaller number of defensible,
maintainable connectors beats 78 brittle ones, and the brief's success signal is
"another team can run it", not "the list is full".

---

## 6. Action items before S1

1. **Inventory is frozen.** `docs/brief-platforms.txt` is the authority; do not edit this
   table's platform names without a `plans/MUTATIONS.md` entry.
2. **Confirm the wave split in §1a.** The counts are asserted to sum to 78.
3. **Confirm the class split.** C1 = 11, C2 = 21, C3 = 26, C4 = 20. Class changes after
   probing are normal; they go through §4's demotion/promotion rules.
4. **Confirm the C3 count is acceptable.** 26 ATS rows are the bulk of the engineering
   risk; the alternative is not exclusion (forbidden) but a longer residual list.
