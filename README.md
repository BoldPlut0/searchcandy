# searchcandy

Grounded answers from your documents — or an honest "not in your
data." Never an invention. Proven.

```python
# export SEARCHCANDY_API_KEY=sc_live_...
from searchcandy import SearchCandy

sc = SearchCandy()
sc.ingest_folder("./contracts")

r = sc.query("What is the termination notice period?")
print(r.quotes[0].text, "—", r.quotes[0].source_id)

t = sc.query("What does clause 99.9 say?")   # not in your data
print(t.refused)   # True — zero tokens, no invention
```

Docs: https://docs.searchcandy-labs.com

---

## What this package is — and isn't

This is the **complete** client: ~500 lines of typed, honest plumbing
over the SearchCandy REST API. The retrieval engine itself never
leaves our servers — which means this repo can be fully public, fully
inspectable, and contains nothing to hide. What you see is exactly
what runs in your process.

- Typed responses — `refused` is a **field, not an exception**
- The retry law: reads retry safely · ingest submits retry with the
  same idempotency key (double-billing is structurally impossible) ·
  billing pauses never auto-retry
- `ingest_folder` skips loudly by name — nothing is silently dropped
- Every server error carries a `request_id` you can quote to support

## Errors you can branch on

| Exception | When |
|---|---|
| `AuthError` | 401 — missing/invalid key (`SEARCHCANDY_API_KEY` set?) |
| `StoreAccessError` | 403/404 — not your graph |
| `RequestError` | 400/413 — the request itself; message names the field |
| `BudgetPausedError` | daily embedding budget reached — never auto-retried |
| `YourEmbeddingEndpointError` | 502 — your BYOE endpoint failed |
| `IngestFailedError` | a job ended in error; carries `job_id` |
| `SearchCandyServerError` | 5xx — our side; quote the `request_id` |
| `SearchCandyUnavailable` | network failure after retries |

## Getting access

Pilots are concierge-onboarded — no self-serve signup yet, by design.
Email **hello@searchcandy-labs.com** and you'll be querying your own
documents within a day of a yes.

MIT licensed. Issues and PRs welcome — this client is developed in
the open.
