"""Typed failures — every one carries what to do next.

Design law (customer-path §3): failures must be un-ignorable in
customer code, and every server error carries a request_id the
customer can quote to support.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .versions import Version


class SearchCandyError(Exception):
    """Base for everything this SDK raises."""

    def __init__(self, message: str, request_id: Optional[str] = None):
        self.request_id = request_id
        if request_id:
            message = f"{message} (request_id: {request_id})"
        super().__init__(message)


class AuthError(SearchCandyError):
    """401 — missing or invalid API key. Is SEARCHCANDY_API_KEY set?"""


class StoreAccessError(SearchCandyError):
    """403/404 — the store id isn't one this key can use."""


class RequestError(SearchCandyError):
    """400/413 — the request itself was invalid (field named inside)."""


class GraphNotReadyError(SearchCandyError):
    """409 — the graph exists but cannot answer yet: empty (no
    documents), indexing a batch, waking up after a seal, or inside a
    deletion window. For ingest and remove submits the SDK waits this
    out for you; for queries it surfaces so your code can say
    something honest.

    `retry_after_s` is set when the API said how long to wait (the
    `store_warming` case sends a Retry-After header) — never parsed
    out of the message."""

    def __init__(self, message: str, request_id: Optional[str] = None,
                 retry_after_s: Optional[float] = None):
        self.retry_after_s = retry_after_s
        super().__init__(message, request_id)


class BudgetPausedError(SearchCandyError):
    """Your daily embedding budget is reached; ingest is paused and
    resumes when the cap is raised (contact us) or the next UTC day.
    Deliberately NOT retried automatically — it is a billing guard."""


class YourEmbeddingEndpointError(SearchCandyError):
    """502 — your bring-your-own-embeddings endpoint failed or timed
    out. Check YOUR provider's status; our side is healthy."""


class IngestFailedError(SearchCandyError):
    """An ingest job ended in error server-side. The payload is
    retained for diagnosis — quote the job id to support.

    A multi-batch upload can fail part-way: `partial` is the
    IngestReport of the batches that DID seal (its `.versions` lists
    the version numbers they became) and `remaining` counts the
    documents that did not land. Nothing is half-sealed — a version
    exists for every batch in `partial` and for none of the rest."""

    def __init__(self, message: str, job_id: str,
                 error_code: Optional[str] = None,
                 partial: "Optional[object]" = None,
                 remaining: int = 0):
        self.job_id = job_id
        self.error_code = error_code
        #: IngestReport — typed loosely here to keep errors.py free of
        #: a types.py import cycle
        self.partial = partial
        self.remaining = int(remaining)
        tail = (f", {remaining} document(s) not ingested"
                if remaining else "")
        super().__init__(
            f"{message} (job {job_id}, code {error_code}{tail})")


class SearchCandyServerError(SearchCandyError):
    """5xx — our side. Quote the request_id to support."""


class SearchCandyUnavailable(SearchCandyError):
    """Network-level failure after retries — the API was unreachable."""


# ---- embedding servers (EMBEDDING-SERVERS-DESIGN §7) --------------------------

class NameTaken(SearchCandyError):
    """409 — a graph or embedding server with this name already exists
    on your account (names are unique per account, case-insensitively),
    or an OPEN draft on this graph already carries this name (draft
    names are unique per graph while open; a discarded name is free to
    reuse)."""


class ServerNotReady(SearchCandyError):
    """409 — the embedding server you named is not `ready` (its last
    connection test failed, or it has no credential yet). Run
    `client.embedding_servers.test(id)` and read `status_reason`."""


class ServerInUse(SearchCandyError):
    """409 — the embedding server is still used by graphs (portal/ops
    delete path; the SDK never deletes servers)."""


class EmbeddingServerRevokedError(SearchCandyError):
    """409 — this graph's embedding server credential was revoked; we
    hold no key for it. Rotate a new key in (portal or
    `embedding_servers.rotate`) to resume answering."""


# ---- versions + drafts (VERSIONING-DESIGN §6) ---------------------------

class VersionNotFound(SearchCandyError):
    """404 — this graph has no version with that number (numbers are
    per graph and start at 1). `client.versions(store=...)` lists what
    exists; `versions.as_of(ts)` finds one by date."""


class DraftNotFound(SearchCandyError):
    """404 — no draft with that name on this graph. `client.drafts(
    store=...)` lists the open ones; a DISCARDED draft is gone as a
    name but its versions are still listed and still askable by
    number — reopen with `drafts.open(name, from_version=<its head>)`."""


class DraftClosed(SearchCandyError):
    """409 — this draft was published or discarded, so it cannot be
    written to or asked. Its versions stay in the history and stay
    askable by number; reopening is `drafts.open(name,
    from_version=<its head>)`."""


class DraftBehind(SearchCandyError):
    """409 — publishing this draft would drop versions that reached
    `current` after the draft was opened. Refused by default, exactly
    as git refuses a non-fast-forward push.

    `versions_since_fork` is the list of those versions (fetched with
    ONE follow-up read, never parsed out of the message). To publish
    anyway: `drafts.publish(name, accept_behind=True)` — that is the
    force, and it is what drops them from `current`. To keep them,
    open a NEW draft from current and re-add your files: we do not
    retain the original document bytes, so we cannot rebase for you."""

    def __init__(self, message: str, request_id: Optional[str] = None,
                 versions_since_fork: "Optional[List[Version]]" = None):
        #: current-line versions newer than the draft's fork point
        self.versions_since_fork = list(versions_since_fork or [])
        if self.versions_since_fork:
            numbers = ", ".join(f"v{v.number}"
                                for v in self.versions_since_fork)
            message = (f"{message}: {len(self.versions_since_fork)} "
                       f"version(s) would not be current after publish: "
                       f"{numbers}. Pass accept_behind=True to publish "
                       f"anyway.")
        super().__init__(message, request_id)


class GraphIncomplete(SearchCandyError):
    """409 — this graph cannot be loaded completely: part of its stored
    data is damaged, so some of it is skipped as it is read.

    We refuse rather than answer from a partial graph, because a partial
    graph produces confident answers that quietly omit evidence. Restore
    the graph from a snapshot or re-ingest it. Not retryable — retrying
    reads the same damaged bytes.
    """


class EmbeddingModelMismatch(SearchCandyError):
    """409 — this graph was built with a different embedding model than
    the one configured for it now.

    Every answer would come back as an incorrect "not covered": the
    question and the documents would be in different vector spaces, so
    nothing would match. The graph itself is intact. Restore the
    embedding configuration this graph was built with, or rebuild the
    graph with the model you want to use.
    """


class StoreUnversioned(SearchCandyError):
    """409 — this graph was built before versioning, so no version
    number can be minted for its bytes. It keeps answering `current`;
    re-ingest it into a new graph (or ask us to rebuild it) to start
    its version history."""


class CredentialVaultError(SearchCandyError):
    """The key could not be vaulted (the console vault route was
    unreachable or refused). The server IS registered — it simply has
    no credential yet: retry, or add the key from the portal."""

    def __init__(self, message: str, server_id: Optional[str] = None,
                 request_id: Optional[str] = None):
        self.server_id = server_id
        super().__init__(message, request_id)

