# searchcandy

The Python client for **TGM** by SearchCandy Labs. TGM hands your AI the
exact passages from your own text and Markdown files, each with its file
and version. Nothing to chunk or tune, and every version is kept, so you
can ask about any past date.

- Docs: https://searchcandy-labs.com/docs
- Your API key: https://searchcandy-labs.com/docs/api-key

## Quickstart

You need Python 3.9 or newer and an API key (see
[Getting access](#getting-access)).

```bash
pip install searchcandy
export SEARCHCANDY_API_KEY=sc_live_...
```

```python
from pathlib import Path
from searchcandy import SearchCandy

# A sample file in a folder of its own
folder = Path("searchcandy-quickstart")
folder.mkdir(exist_ok=True)
(folder / "services-agreement.md").write_text(
    "# Services Agreement\n\n"
    "## Termination\n\n"
    "Either party may terminate this agreement with sixty days' written notice.\n",
    encoding="utf-8",
)

sc = SearchCandy()                      # reads SEARCHCANDY_API_KEY
report = sc.ingest_folder(folder)       # returns once the new version is ready
print("versions:", report.versions, "skipped:", report.skipped)

r = sc.query("How much notice do we need to cancel?")
for p in r.context_atoms:               # every passage your AI receives, in order
    print(p.source_id, "|", p.text.strip())
print("answered from version", r.version)
```

`r.context_atoms` is what your AI reads: every passage TGM returns, word
for word, the file TGM ranks first coming first and each file's passages
in that file's own reading order. `r.quotes` keeps one passage per file
(the first one served from it), handy for listing which files were used.

`ingest_folder` takes `.txt` and `.md` files and names every file it
skips, with the reason. Run it again after you change a file: the file
is updated in a new version, never duplicated. The example uses your
key's default graph; pass `store="name"` to use another one.

### Ask about a past date

```python
v = sc.versions.as_of("2026-10-01T09:00:00Z")   # the version answering at that instant
old = sc.query("How much notice do we need to cancel?", version=v.number)
old.version, old.behind                          # e.g. 1, True: an earlier version, on purpose
```

The timestamp needs a time zone (`Z` or `-07:00`); a time before your
first upload raises `VersionNotFound`.

### When no passage is returned

`r.not_covered` is a field, not an exception. `True` means no
supporting passage was returned. It is not proof that no relevant
passage exists, and the flag can be wrong both ways, so your code
decides what happens next, for example asking the user to add a
document.

---

## What this package is, and is not

This is the **complete** client: typed plumbing over the TGM REST API.
The engine itself runs on our servers, which is why this repo can be
fully public: what you see is exactly what runs in your process.

- Typed responses: `not_covered` is a **field, not an exception**
- Retries: reads retry safely; ingest submits retry with the same
  idempotency key, so a retried upload is never billed twice; billing
  pauses never retry on their own
- `ingest_folder` skips loudly, by name: nothing is dropped silently
- Every server error carries a `request_id` you can quote to support

## Versions

Every sealed change to a graph is a numbered, immutable snapshot of
its document set. Answers say which version produced them, any answer
can be reproduced by asking that version again, and `current` is a
pointer you can move.

```python
sc = SearchCandy()

sc.create_graph("client-contracts")                             # once
sc.ingest_folder("./contracts", store="client-contracts")       # → v1
sc.ingest([{"source_id": "policy.md", "content": new_text}],
          store="client-contracts")                             # → v2: policy.md UPDATED, not duplicated
rep = sc.remove(["old-addendum.md"], store="client-contracts")  # → v3
rep.removed, rep.not_present, rep.version                       # ["old-addendum.md"], [], 3

r  = sc.query("What is the notice period?", store="client-contracts")
r.version, r.current_version, r.pinned, r.store_generation      # 3, 3, False, "…"
r0 = sc.query("What is the notice period?", store="client-contracts", version=1)
r0.version, r0.behind                                           # 1, True: version 1's answer
```

Re-ingesting an existing `source_id` is an **update**, not a
duplicate: in the same version its old passages stop answering and the
new ones start. `remove()` takes documents out of current answers;
earlier versions still contain them, so the history stays complete. To
erase documents from our disks, delete the graph.

### Reading the timeline

```python
page = sc.versions(store="client-contracts")                 # list-like, .has_more, .next_before
for v in page:
    print(v.number, v.kind, v.created_at, v.changes.added, v.changes.removed, v.restored_from)

for v in sc.versions.all(store="client-contracts"): ...       # every page (audit export)
sc.versions.get(3, store="client-contracts")
for d in sc.versions.documents(3, store="client-contracts"): ...
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
way git refuses a non-fast-forward push: `DraftBehind` lists exactly
which versions would stop being current, and `accept_behind=True` is
the force.

## Errors you can branch on

A question with no supporting passage is **not** an error: it is
`r.not_covered == True`.

| Exception | When |
|---|---|
| `AuthError` | 401: missing or invalid key (`SEARCHCANDY_API_KEY` set?) |
| `StoreAccessError` | 403/404: not your graph |
| `RequestError` | 400/413: the request itself; the message names the field |
| `BudgetPausedError` | daily embedding budget reached; never retried on its own |
| `YourEmbeddingEndpointError` | 502: your own embedding endpoint failed |
| `IngestFailedError` | a job ended in error; carries `job_id`, `.partial`, `.remaining` |
| `GraphNotReadyError` | a query or change while a write job runs; the SDK waits out submits for you |
| `SearchCandyServerError` | 5xx: our side; quote the `request_id` |
| `SearchCandyUnavailable` | the API could not be reached after retries |

**Versioning**: `VersionNotFound` · `StoreUnversioned` ·
`DraftNotFound` · `DraftBehind` (carries `versions_since_fork`) ·
`DraftClosed` · `NameTaken`

**Graph integrity**: a graph refuses to open rather than answer from
a damaged or mismatched one.

| Exception | When |
|---|---|
| `GraphIncomplete` | the graph loaded less than it sealed (damage). Not retryable; restore from a snapshot |
| `EmbeddingModelMismatch` | the graph is being read by a different embedding model than built it |

**Bring your own embeddings**: `CredentialVaultError` ·
`EmbeddingServerRevokedError` · `ServerInUse` · `ServerNotReady`

## Getting access

We set up every account after a short call:
[book one](https://cal.com/searchcandy-labs/30min) or email
**hello@searchcandy-labs.com**. You then get a log-in link for the
console (https://searchcandy-labs.com/login), and the console shows
your API key once. Details: https://searchcandy-labs.com/docs/api-key

## Version notes

See [CHANGELOG.md](CHANGELOG.md). In short: 0.3.1 fixes two dead links
(the docs, and the vault used to register your own embedding server)
and gives up faster when the API cannot be reached. 0.3.0 renamed
`refused` to `not_covered`; `refused` still works as a deprecated
alias, so code written for 0.1.0 keeps running.

MIT licensed. Issues and PRs welcome: this client is developed in the
open.
