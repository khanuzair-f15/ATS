# Platform inventory and transport classification

> **INVENTORY STATUS: v0.1 — ASSUMED, REQUIRES CONFIRMATION.**
>
> The brief specifies "78 platforms (58 ATS + 20 job boards)" and names a subset
> explicitly. This file reconstructs a defensible 78-row inventory from those named
> families plus widely-used platforms in the same categories. If your actual list
> differs, **edit this table before S1 starts** — it is the cheapest moment to change
> the scope, and every later wave is sequenced off it.
>
> **Nothing in this file may be read as "implemented".** `Verified` is `no` for every
> row until a connector has produced real jobs from a real tenant and a `fetch_run`
> row exists (S14 emits that evidence). A row marked `no (doc)` means the transport
> pattern is publicly documented but has **not been probed** by us.

---

## 0. How to read the classification

| Class | What it means | Engineering cost per connector | Fragility |
|---|---|---|---|
| **C1** | Documented public JSON API for the board | Low — 1–2 requests, stable shape | Low |
| **C2** | Embedded JSON/XML endpoint behind a JS-heavy career site | Medium — tenant path templating, pagination contracts, POST bodies | Medium |
| **C3** | No structured source; DOM parsing or a real browser required | High — selectors, rendering, screenshots on failure | High |
| **C4** | Job-board platform; one connector often covers many boards | Low–medium — shared engine, but per-board quirks | Medium |
| **EX** | Excluded — ToS prohibition, paid-only API, or hard bot defence | — | — |

**Policy (stakeholder-approved):** pursue C1 and C2 relentlessly. Every C3 connector
must carry a written justification in this file. C4 is preferred over bespoke HTML.
EX rows are documented with a reason, never half-implemented.

**Request estimate convention:** `1 + ⌈N/100⌉ + N` per tenant per full sync, where `N`
= open roles at that tenant (one listing call, listing pages, one detail call per job).

---

## 1. ATS platforms (58)

| # | Platform | Class | Discovery → detail | Auth | Req/sync | Risk | Verified |
|---|---|---|---|---|---|---|---|
| 1 | Workday | C2 | `POST {host}/wday/cxs/{tenant}/{site}/jobs` → `.../job/{path}` | none | 1 + pages + N | M | no (doc) |
| 2 | SAP SuccessFactors | C2 | careers site search → RMK job JSON | none | 1 + pages + N | H | no |
| 3 | Oracle Taleo (TBE) | C2 | `taleo.net/careersection/.../jobsearch.ftl` → REST job JSON | none | 1 + pages + N | M | no (doc) |
| 4 | Oracle Taleo (TEE) | C2 | `tbe.taleo.net/{region}/ats/careers/v2/...` → job JSON | none | 1 + pages + N | H | no |
| 5 | Oracle Recruiting Cloud | C2 | `/hcmRestApi/resources/latest/recruitingCEJobRequisitions` finder | none | 1 + pages + N | M | no (doc) |
| 6 | iCIMS | C2 | `careers-{co}.icims.com/jobs/search` → `/jobs/{id}/job` | none | 1 + pages + N | M | no |
| 7 | Phenom People | C2 | `{host}/api/apply/v2/jobs?domain=...` | none | 1 + pages + N | M | no (doc) |
| 8 | Eightfold AI | C2 | `{host}/api/apply/v2/jobs` | none | 1 + pages + N | M | no (doc) |
| 9 | Avature | C3 | `/search-jobs` HTML (+ XHR JSON on some tenants) | none | 1 + pages + N | H | no |
| 10 | Cornerstone OnDemand | C2 | tenant `/ux/...` + internal services JSON | none | 1 + pages + N | H | no |
| 11 | PageUp | C3 | `{host}/jobs` HTML listing | none | 1 + pages + N | H | no |
| 12 | Dayforce (Ceridian) | C2 | tenant career site JSON/XHR | none | 1 + pages + N | H | no |
| 13 | UKG Pro Recruiting (UltiPro) | C2 | `recruiting.ultipro.com/{co}/JobBoard/.../LoadSearchResults` POST | none | 1 + pages + N | M | no (doc) |
| 14 | ADP Workforce Now | C2 | `workforcenow.adp.com/.../staffing/v1/job-requisitions?cid=` | none | 1 + pages + N | M | no (doc) |
| 15 | Paylocity | C2 | `recruiting.paylocity.com/Recruiting/Jobs/GetJobListings` POST | none | 1 + pages + N | M | no (doc) |
| 16 | Paycom | C2 | `{co}.paycomonline.net/v4/ats/web.php/jobs` | none | 1 + pages + N | M | no |
| 17 | Paycor | C3 | `recruiting.paycor.com` HTML listing | none | 1 + pages + N | H | no |
| 18 | Greenhouse | C1 | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | none | 1 | L | no (doc) |
| 19 | Lever | C1 | `api.lever.co/v0/postings/{site}?mode=json` | none | 1 | L | no (doc) |
| 20 | Ashby | C1 | `api.ashbyhq.com/posting-api/job-board/{org}` | none | 1 | L | no (doc) |
| 21 | SmartRecruiters | C1 | `api.smartrecruiters.com/v1/companies/{co}/postings` | none | 1 + N/pages | L | no (doc) |
| 22 | Workable | C1 | `apply.workable.com/api/v1/widget/accounts/{sub}?details=true` | none | 1 | L | no (doc) |
| 23 | Recruitee | C1 | `{co}.recruitee.com/api/offers/` | none | 1 | L | no (doc) |
| 24 | Teamtailor | C2 | public careers site + embedded job JSON | none | 1 + N | M | no |
| 25 | Personio | C2 | `{co}.jobs.personio.com/xml` feed → detail | none | 2 | L | no (doc) |
| 26 | BambooHR | C1/C2 | `{co}.bamboohr.com/careers/list` JSON → detail HTML | none | 1 + N | M | no |
| 27 | Breezy HR | C1 | `{co}.breezy.hr/json` | none | 1 | L | no |
| 28 | JazzHR | C3 | `{co}.applytojob.com` HTML listing | none | 1 + pages + N | H | no |
| 29 | Jobvite | C3 | `jobs.jobvite.com/{co}/search` HTML | none | 1 + pages + N | M | no |
| 30 | Pinpoint | C1 | `{co}.pinpointhq.com/postings.json` | none | 1 | L | no |
| 31 | HiBob | C2 | `{co}.careers.hibob.com` embedded JSON | none | 1 + N | M | no |
| 32 | Rippling | C2 | `ats.rippling.com/{co}/jobs` embedded JSON | none | 1 + N | M | no |
| 33 | Comeet | C1 | `comeet.co/careers-api/2.0/company/{uid}/positions` | **token** | 1 | M | no (doc) |
| 34 | Trakstar Hire | C3 | `{co}.hire.trakstar.com` HTML | none | 1 + pages + N | H | no |
| 35 | JobScore | C3 | `careers.jobscore.com/careers/{co}` HTML | none | 1 + pages + N | H | no |
| 36 | PCRecruiter | C3 | `{co}.pcrecruiter.net` HTML | none | 1 + pages + N | H | no |
| 37 | CATS | C3 | `{co}.catsone.com/careers` HTML | none | 1 + pages + N | H | no |
| 38 | Zoho Recruit | C2 | careers portal XHR JSON | none | 1 + pages + N | M | no |
| 39 | Freshteam | C3 | `{co}.freshteam.com/jobs` HTML | none | 1 + pages + N | M | no |
| 40 | Manatal | C3 | `{co}.manatal.com` HTML | none | 1 + pages + N | H | no |
| 41 | Bullhorn | C2 | Bullhorn-hosted career portal JSON | varies | 1 + pages + N | H | no |
| 42 | ClearCompany | C3 | `{co}.clearcompany.com` HTML | none | 1 + pages + N | H | no |
| 43 | ApplicantPro | C3 | `{co}.applicantpro.com` HTML | none | 1 + pages + N | H | no |
| 44 | Hireology | C3 | `{co}.hireology.com` HTML | none | 1 + pages + N | H | no |
| 45 | Newton Software | C2 | `{co}.newtonsoftware.com` embedded JSON | none | 1 + N | M | no |
| 46 | TalentReef | C3 | `{co}.talentreef.com` HTML | none | 1 + pages + N | H | no |
| 47 | Harri | C3 | `{co}.harri.com` HTML | none | 1 + pages + N | H | no |
| 48 | Gupy | C2 | `{co}.gupy.io` XHR JSON | none | 1 + pages + N | M | no |
| 49 | Factorial | C2 | `{co}.factorialhr.com` embedded JSON | none | 1 + N | M | no |
| 50 | Kenoby | C2 | `{co}.kenoby.com` XHR JSON | none | 1 + pages + N | M | no |
| 51 | Deel | C2 | `{co}.deel.com` embedded JSON | none | 1 + N | M | no |
| 52 | IBM BrassRing (Kenexa) | C2 | `{co}.brassring.com` career section JSON | none | 1 + pages + N | H | no |
| 53 | SilkRoad | C2 | `{co}.silkroad.com` embedded JSON | none | 1 + pages + N | H | no |
| 54 | Talentsoft (Cegid) | C2 | `{co}.talentsoft.com` XHR JSON | none | 1 + pages + N | M | no |
| 55 | Softgarden | C1 | `{co}.softgarden.io/jobs` JSON feed | none | 1 | L | no |
| 56 | eRecruiter | C2 | `{co}.erecruiter.pl` XHR JSON | none | 1 + pages + N | M | no |
| 57 | Sympa | C1 | `{co}.sympa.ai` public feed | none | 1 + N | M | no |
| 58 | PeopleSoft eRecruit | C2 | `/psc/{site}/EMPLOYEE/HRMS/c/HRS_HRAM...` XHR | none | 1 + pages + N | H | no |

**Distribution:** C1 = 11, C2 = 29, C3 = 18. The C3 count is the plan's main risk and
the reason S13 exists as a separate, justified wave rather than an afterthought.

---

## 2. Job boards (20)

| # | Platform | Class | Discovery → detail | Auth | Req/sync | Risk | Verified |
|---|---|---|---|---|---|---|---|
| 59 | Remote OK | C4 | `remoteok.com/api` (single JSON document) | none | 1 | L | no (doc) |
| 60 | Remotive | C4 | `remotive.com/api/remote-jobs?limit=` | none | ~5 | L | no (doc) |
| 61 | We Work Remotely | C4 | category RSS feeds → detail HTML | none | ~10 + N | L | no (doc) |
| 62 | Himalayas | C4 | public jobs API / JSON | none | 1 + pages | M | no |
| 63 | Jobicy | C4 | `jobicy.com/api/v2/remote-jobs` | none | ~4 | L | no (doc) |
| 64 | Working Nomads | C4 | `workingnomads.com/api/exposed_jobs/` | none | 1 | L | no |
| 65 | Hacker News "Who is hiring" | C4 | Algolia HN API (`hn.algolia.com/api/v1`) | none | 1 + comments | M | no (doc) |
| 66 | WP Job Manager | C4 | `{board}/wp-json/wp/v2/job_listing` | none | 1 + pages | L | no (doc) |
| 67 | Niceboard | C4 | `{board}.niceboard.co/api/...` | none | 1 + pages | M | no |
| 68 | Built In | C3 | HTML listing + `JobPosting` JSON-LD | none | 1 + pages + N | H | no |
| 69 | Wellfound (AngelList) | EX | GraphQL with anti-bot + ToS restriction | none | — | X | no |
| 70 | Otta / Welcome to the Jungle | C3 | HTML listing | none | 1 + pages + N | H | no |
| 71 | YC Work at a Startup | EX | Requires account session | session | — | X | no |
| 72 | LinkedIn Jobs | EX | Prohibited by ToS; no public API | — | — | X | no |
| 73 | Indeed | EX | Publisher API restricted; scraping prohibited | key | — | X | no |
| 74 | Glassdoor | EX | Prohibited by ToS; hard bot defence | — | — | X | no |
| 75 | ZipRecruiter | EX | Partner-only API | — | — | X | no |
| 76 | Monster | EX | Prohibited by ToS | — | — | X | no |
| 77 | Dice | EX | Partner API only | — | — | X | no |
| 78 | SimplyHired | EX | Aggregator; prohibited by ToS | — | — | X | no |

**Distribution:** C4 = 9, C3 = 2, EX = 9.

---

## 3. Projected deliverability (to be replaced by measured coverage in S14)

| Outcome | Count | Note |
|---|---|---|
| Structured (C1/C2/C4), expected working | 49 | The realistic "78 platforms" headline |
| HTML/headless (C3), expected working | 11 of 20 | Expensive; each needs justification |
| Excluded on legal/technical grounds (EX) | 9 | Documented, not attempted |
| **Total with a defensible position** | **69** | |
| Genuinely unverifiable after effort | ≤ 9 | Will be listed explicitly |

This is the honest framing for an acquisition story: **78 platforms addressed, 69 with
live evidence or a documented exclusion, 9 residual and named.** Claiming 78/78 working
would fail diligence on the first spot-check.

---

## 4. Verification protocol (how a row becomes `yes`)

A row is promoted to `Verified = yes` only when all five hold:

1. `connectors/<platform>.py` exists and implements the full Protocol.
2. Fixtures captured live for **≥2 distinct tenants** (or 1 with a recorded reason).
3. Golden replay test passes offline: `pytest tests/connectors/test_<platform>.py`.
4. A real run produced ≥1 job with all required fields, linked to a `fetch_run` id.
5. `docs/coverage.md` regenerated by `jobindex.cli coverage` and shows the row.

**Probe-before-build rule:** before writing a connector, capture one request and
inspect it. If the payload shape is not what this table claims, update this table
first. A connector built on a guessed endpoint is deleted, not patched.

**Demotion rule:** if a platform turns out to have no structured source, it moves to
C3 and is deferred to S13 — it is not hacked into the C1 wave.

---

## 5. Legal, robots, and operational posture

### Absolute constraints (no exceptions)

- No authentication bypass, no paywalls, no logged-in sessions.
- No circumvention of CAPTCHAs, bot defences, or rate limits.
- Identifiable `User-Agent` with a contact URL on every request.
- `robots.txt` honoured for C3 sources; disallowed paths are not fetched.
- Public, unauthenticated job listings only.

### Excluded platforms and why

Nine board rows are marked EX. The reasons fall into three buckets:

1. **Contractual prohibition** — the ToS forbids automated collection (LinkedIn,
   Glassdoor, Monster, SimplyHired).
2. **Paid/partner-only access** — an API exists but is not available to this project
   (Indeed, ZipRecruiter, Dice).
3. **Session-gated** — content requires an account (YC Work at a Startup, Wellfound).

For an acquisition deliverable these are **documented exclusions, not failures**. The
coverage report must show them in their own section with the reason, so diligence sees
a deliberate boundary rather than a gap.

### Operational exposure

| Risk | Mitigation |
|---|---|
| IP blocking from C3 volume | Per-host concurrency 1, jittered delays, tiered refresh, browser pool reuse |
| Tenant path drift on C2 | Per-tenant config in `source.config`, contract tests per tenant |
| Selector rot on C3 | Screenshot + DOM snapshot on failure, captured into `fetch_run` |
| Takedown request | Per-source disable flag; documented in runbook; no re-enable without review |
| A source dying silently | Circuit breaker surfaces `degraded` in `/v1/sources`; coverage report flags stale |

### What this posture costs

Choosing structured-first means several C2/C3 platforms (Paycor, Avature, PageUp,
BrassRing, SilkRoad, PeopleSoft) may resolve to "no usable structured endpoint" and end
up in the residual list. That is the accepted trade: a smaller number of defensible,
maintainable connectors beats 78 brittle ones, and the brief's success signal is
"another team can run it", not "the list is full".

---

## 6. Action items before S1

1. **Confirm or correct the 78 rows.** Replace any platform that is not on your list.
2. **Confirm the EX set.** If you have licensed access to any excluded board, say so and
   it moves to C4 with a key.
3. **Confirm the C3 count is acceptable.** 18 ATS rows plus 2 boards is the bulk of the
   engineering risk; the alternative is to declare some of them out of scope now.
