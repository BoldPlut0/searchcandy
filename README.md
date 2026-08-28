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
print(t.not_covered)   # True — zero tokens, no invention
```

Docs: https://docs.searchcandy-labs.com

> **0.2.0 → 0.3.0**: the honest-refusal field is now `not_covered`.
> `refused` still works as a deprecated alias, so existing code keeps
> running unchanged.

---

## What this package is — and isn't

This is the **complete** client: typed, honest plumbing over the
SearchCandy REST API. The retrieval engine itself never leaves our
servers — which means this repo can be fully public, fully
inspectable, and contains nothing to hide. What you see is exactly
what runs in your process.

- Typed responses — `not_covered` is a **field, not an exception**
- The retry law: reads retry safely · ingest submits retry with the
  same idempotency key (double-billing is structurally impossible) ·
  billing pauses never auto-retry
- `ingest_folder` skips loudly by name — nothing is silently dropped
- Every server error carries a `request_id` you can quote to support

## Versions

Every sealed change to a graph is a numbered, immutable snapshot of
its document set. Answers say which version produced them, any answer
can be reproduced by asking that version again, and `current` is a
pointer you can move.

```python
sc = SearchCandy()

sc.ingest_folder("./contracts", store="client-contracts")      # → v1
sc.ingest([{"source_id": "policy.md", "content": new_text}],
          store="client-contracts")                            # → v2: policy.md UPDATED, not duplicated
rep = sc.remove(["old-addendum.md"], store="client-contracts") # → v3
rep.removed, rep.not_present, rep.version                      # ["old-addendum.md"], [], 3

r  = sc.query("What is the notice period?", store="client-contracts")
r.version, r.current_version, r.pinned, r.store_generation     # 3, 3, False, "…"
r0 = sc.query("What is the notice period?", store="client-contracts", version=1)
r0.version, r0.behind                                          # 1, True — yesterday's answer
```

Re-ingesting an existing `source_id` is an **update**, not a
duplicate: in the same version its old passages stop answering and the
new ones start. `remove()` takes documents out of current answers —
earlier versions still contain them, so the history stays complete. To
erase documents from our disks, delete the graph.

### Reading the timeline

```python
page = sc.versions(store="client-contracts")                 # list-like, .has_more, .next_before
for v in page:
    print(v.number, v.kind, v.created_at, v.changes.added, v.changes.removed, v.restored_from)

for v in sc.versions.all(store="client-contracts"): ...       # every page (audit export)
sc.versions.get(3, store="client-contracts")
for sid in sc.versions.documents(3, store="client-contracts"): ...
sc.versions.as_of("2026-08-19T10:00:00Z", store="client-contracts")
```

### Restore

```python
sc.versions.restore(1, store="client-contracts")   # → v4 (restore of v1); instant (a pointer move)
```

Restore creates a new version and moves the pointer. Nothing is
rewritten and nothing is lost: the versions you restored past stay in
the history and stay askable by number. There is no re-embedding and
no reseal, so there is nothing to wait for.

## Drafts

A draft is an unpublished line of your graph that you can ask
questions of before it becomes current.

```python
d = sc.drafts.open("policy-2026", store="client-contracts")  # forks from current
sc.ingest([...], store="client-contracts", draft="policy-2026")
sc.query("…", store="client-contracts", draft="policy-2026") # the draft's answer
sc.drafts.publish("policy-2026", store="client-contracts")   # → v5; DraftBehind if behind
```

Publish is never a merge: what you tested is what ships. If versions
reached `current` after the draft was opened, publish is refused the
way git refuses a non-fast-forward push — `DraftBehind` lists exactly
which versions would stop being current, and `accept_behind=True` is
the force.

## Errors you can branch on

A "not in your data" answer is **not** an error: it is
`r.not_covered == True`, with zero served tokens.

| Exception | When |
|---|---|
| `AuthError` | 401 — missing/invalid key (`SEARCHCANDY_API_KEY` set?) |
| `StoreAccessError` | 403/404 — not your graph |
| `RequestError` | 400/413 — the request itself; message names the field |
| `BudgetPausedError` | daily embedding budget reached — never auto-retried |
| `YourEmbeddingEndpointError` | 502 — your BYOE endpoint failed |
| `IngestFailedError` | a job ended in error; carries `job_id`, `.partial`, `.remaining` |
| `GraphNotReadyError` | a query or mutation while a write job runs — the SDK polls submits for you |
| `SearchCandyServerError` | 5xx — our side; quote the `request_id` |
| `SearchCandyUnavailable` | network failure after retries |

**Versioning**: `VersionNotFound` · `StoreUnversioned` ·
`DraftNotFound` · `DraftBehind` (carries `versions_since_fork`) ·
`DraftClosed` · `NameTaken`

**Graph integrity** — a graph refuses to open rather than answer from
a damaged or mismatched one:

| Exception | When |
|---|---|
| `GraphIncomplete` | the graph loaded less than it sealed — damage. Not retryable; restore from a snapshot |
| `EmbeddingModelMismatch` | the graph is being read by a different embedding model than built it |

**Bring-your-own embeddings**: `CredentialVaultError` ·
`EmbeddingServerRevokedError` · `ServerInUse` · `ServerNotReady`

## Getting access

Pilots are concierge-onboarded — no self-serve signup yet, by design.
Email **hello@searchcandy-labs.com** and you'll be querying your own
documents within a day of a yes.

MIT licensed. Issues and PRs welcome — this client is developed in
the open.
