"""The SearchCandy client — ten lines to a grounded answer and an
honest refusal.

Retry law (customer-path design §3, verbatim):
- reads (query/status) retry on network failure with backoff;
- ingest submits retry with the SAME idempotency key — the server
  dedupes, so double-billing is structurally impossible;
- 409 store-busy means another batch is indexing: the client POLLS
  the store back to ready instead of surfacing our pipeline law as
  the customer's problem;
- budget pauses are NEVER auto-retried (a billing guard, not a
  transient).
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Union

import httpx

from .drafts import Drafts
from .embedding_servers import (DEFAULT_VAULT_URL, ENV_VAULT_URL,
                                EmbeddingServers)
from .errors import (AuthError, BudgetPausedError, DraftBehind,
                     DraftClosed, DraftNotFound,
                     EmbeddingServerRevokedError, GraphNotReadyError,
                     IngestFailedError, NameTaken, RequestError,
                     SearchCandyError, SearchCandyServerError,
                     SearchCandyUnavailable, ServerInUse, ServerNotReady,
                     StoreAccessError, StoreUnversioned, VersionNotFound,
                     EmbeddingModelMismatch, GraphIncomplete,
                     YourEmbeddingEndpointError)
from .types import (GraphSummary, IngestReport, JobInfo, RemoveReport,
                    RetrievalResponse, StoreInfo, UsageDay,
                    graphs_from_wire, job_from_wire, response_from_wire,
                    usage_from_wire)
from .versions import Versions

DEFAULT_BASE_URL = "https://api.searchcandy-labs.com"
ENV_KEY = "SEARCHCANDY_API_KEY"

MAX_DOCS_PER_BATCH = 500
#: the API's own ceiling for one remove() call (§3 caps)
MAX_REMOVE_SOURCE_IDS = 500
#: 2MB under the server's 10MB body cap: JSON overhead headroom,
#: measured in the SDK tests rather than assumed
MAX_BATCH_BYTES = 8 * 1024 * 1024
LARGE_FILE_NOTE_BYTES = 2 * 1024 * 1024

_RETRYABLE_STATUS = (502, 503, 504)


class SearchCandy:
    """client = SearchCandy()          # key from SEARCHCANDY_API_KEY
    r = client.query("...")            # → RetrievalResponse
    client.ingest_folder("./docs")     # .txt/.md, skips loudly
    client.remove(["old.md"])          # → a new version; nothing purged
    client.versions()                  # the timeline; .restore(n) is instant
    client.drafts.open("policy-2026")  # ask a change before it ships
    client.usage()                     # your meter, your invoice"""

    def __init__(self, api_key: Optional[str] = None,
                 base_url: str = DEFAULT_BASE_URL,
                 timeout: float = 75.0,
                 transport: Optional[httpx.BaseTransport] = None,
                 vault_url: Optional[str] = None):
        key = api_key or os.environ.get(ENV_KEY)
        if not key:
            raise AuthError(
                "no API key: pass api_key=... or set the "
                f"{ENV_KEY} environment variable")
        self._http = httpx.Client(
            base_url=base_url, timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {key}",
                     "User-Agent": "searchcandy-python/0.3.0"})
        #: bring-your-own embedding servers (see embedding_servers.py);
        #: the vault hop is the ONE place a provider key travels
        self.embedding_servers = EmbeddingServers(
            self, vault_url or os.environ.get(ENV_VAULT_URL)
            or DEFAULT_VAULT_URL)
        #: the version timeline (see versions.py) — call it for a page,
        #: `.get/.documents/.as_of/.all` to read, `.restore` to repoint
        self.versions = Versions(self)
        #: draft lines (see drafts.py) — open, ask, publish, discard
        self.drafts = Drafts(self)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SearchCandy":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- queries --------------------------------------------------------
    def query(self, query: str, store: Optional[str] = None,
              version: Optional[int] = None,
              draft: Optional[str] = None) -> RetrievalResponse:
        """Ask. Returns a grounded answer (byte-faithful quotes) or an
        honest refusal (`not_covered=True`, zero tokens) — a refusal is
        a RESULT, not an exception. `store` accepts a graph NAME as
        well as an id ("contracts" reads better than "st_9f…").

        By default you get the graph's CURRENT answer. Pin the past
        with `version=n`, or ask an unpublished line with
        `draft="policy-2026"` — one or the other, never both.

            r  = sc.query("What is the notice period?")
            r.version, r.current_version, r.pinned   # 3, 3, False
            old = sc.query("What is the notice period?", version=1)
            old.behind                               # True — yesterday's answer

        Every answer says which version produced it, so
        `query(q, version=r.version)` is the receipt: within one store
        generation it reproduces `r`. A pinned answer is served from
        exactly that version's document set, through the same
        certified pipeline — and its refusal reads "not in your
        documents at v3", not "not in your documents"."""
        if version is not None and draft is not None:
            raise RequestError(
                "version and draft: pass one or the other — a version "
                "number names a sealed point on the graph's own "
                "timeline, a draft names an unpublished line")
        store = self._resolve_store(store)
        path = (f"/v1/stores/{store}/query" if store else "/v1/query")
        payload: dict = {"query": query}
        if version is not None:
            payload["version"] = int(version)
        if draft is not None:
            payload["draft"] = draft
        body = self._request("POST", path, json=payload, retries=2)
        return response_from_wire(body)

    # ---- ingest ---------------------------------------------------------
    def ingest(self, documents: Sequence[dict], *,
               store: Optional[str] = None, draft: Optional[str] = None,
               wait: bool = True,
               poll_timeout: float = 1800.0) -> IngestReport:
        """Upload documents ([{"content", "source_id", "title"?}]).
        Batches automatically; each batch gets ONE idempotency key
        reused across retries (the server replays, never duplicates).
        With wait=True (default) returns after every batch is sealed.

        EVERY SEALED BATCH IS A VERSION: a 1,200-document folder
        becomes v1, v2, v3 — `report.versions` lists them. An existing
        `source_id` is an UPDATE, not a duplicate: its old passages
        stop answering in the same version the new ones start, and
        earlier versions keep both.

        `draft="policy-2026"` writes to a draft line instead of the
        graph's own: the graph keeps answering as it did, and you can
        ask the draft before publishing it.

        One `source_id` may appear at most ONCE per call — refused
        here, before anything is uploaded or billed, because the
        alternative is a silent race over which body survives."""
        _refuse_repeated_source_ids(documents)
        store_id = self._resolve_store(store) or self._default_store_id()
        jobs: List[JobInfo] = []
        total_tokens = 0
        done_docs = 0
        for batch in _batches(documents):
            job = self._submit_batch(store_id, batch, draft=draft,
                                     poll_timeout=poll_timeout)
            if wait:
                job = self._wait_job(job, poll_timeout)
                if job.status == "error":
                    raise IngestFailedError(
                        "ingest batch failed", job.job_id, job.error_code,
                        partial=IngestReport(docs=done_docs, jobs=jobs,
                                             embed_tokens=total_tokens),
                        remaining=len(documents) - done_docs)
                if job.status == "paused_budget":
                    raise BudgetPausedError(
                        "daily embedding budget reached — batch is "
                        "paused; retry after the cap resets or "
                        "contact us")
                total_tokens += job.embed_tokens or 0
                done_docs += len(batch)
            jobs.append(job)
        return IngestReport(docs=len(documents), jobs=jobs,
                            embed_tokens=total_tokens)

    def ingest_folder(self, folder: Union[str, Path], *,
                      store: Optional[str] = None,
                      draft: Optional[str] = None, wait: bool = True,
                      poll_timeout: float = 1800.0) -> IngestReport:
        """Upload every .txt/.md under `folder` (recursive).

        Loud by design: skipped files are NAMED in the report —
        silently eating a file you thought was indexed would be
        worse than any error.

        Each file's path relative to `folder` is its `source_id`, so
        re-running this on a changed folder UPDATES the files that
        changed rather than duplicating them. Every sealed batch is a
        version (`report.versions`). `draft=` writes to a draft line."""
        root = Path(folder)
        if not root.is_dir():
            raise RequestError(f"not a folder: {root}")
        docs, skipped = [], []
        seen_real = set()
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            if any(p.name.startswith(".") for p in
                   path.relative_to(root).parents):
                continue
            real = path.resolve()
            if real in seen_real:
                skipped.append(f"{path}: duplicate (symlink)")
                continue
            seen_real.add(real)
            if path.suffix.lower() not in (".txt", ".md"):
                skipped.append(f"{path}: unsupported type "
                               f"({path.suffix or 'no extension'}) — "
                               f"v1 ingests .txt/.md; we convert other "
                               f"formats during onboarding")
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                skipped.append(f"{path}: not valid UTF-8 — skipped "
                               f"rather than silently mangled")
                continue
            if not content.strip():
                skipped.append(f"{path}: empty")
                continue
            if len(content.encode()) > LARGE_FILE_NOTE_BYTES:
                skipped.append(f"{path}: NOTE ingested but large "
                               f"(> 2MB) — check it isn't an export "
                               f"artifact")
            docs.append({"content": content,
                         "source_id": str(path.relative_to(root))})
        report = self.ingest(docs, store=store, draft=draft, wait=wait,
                             poll_timeout=poll_timeout)
        return IngestReport(docs=report.docs, jobs=report.jobs,
                            embed_tokens=report.embed_tokens,
                            skipped=skipped)

    # ---- remove ---------------------------------------------------------
    def remove(self, source_ids: Sequence[str], *,
               store: Optional[str] = None, draft: Optional[str] = None,
               wait: bool = True,
               poll_timeout: float = 1800.0) -> RemoveReport:
        """Take documents out of the graph's current answers.

        Removing takes these documents out of current answers. Earlier
        versions still contain them, so the version history stays
        complete. To erase documents from our disks, delete the graph.

        The removal is a VERSION like any other, so it is one
        `versions.restore(n)` from undone, and a removed document
        stays readable by pinning an earlier version. A remove that
        removes nothing still seals a version ("0 removed · 1 not
        found") — `not_present` is a fact, not an error.

            rep = sc.remove(["old-addendum.md"], store="contracts")
            rep.removed, rep.not_present, rep.version

        With wait=True (default) the report is built from the SEALED
        version record — the ledger's own truth about what left. With
        wait=False it is provisional (the API's registry pre-check)
        and `version` is None: nothing is sealed yet.

        `draft=` removes from a draft line instead of the graph's own.
        At most 500 source_ids per call."""
        requested = [str(s) for s in source_ids]
        if not requested:
            raise RequestError(
                "source_ids: name at least one document to remove")
        if len(requested) > MAX_REMOVE_SOURCE_IDS:
            raise RequestError(
                f"source_ids: {len(requested)} given, at most "
                f"{MAX_REMOVE_SOURCE_IDS} per call — split it into "
                f"calls (each seals its own version)")
        store_id = self._resolve_store(store) or self._default_store_id()
        payload: dict = {"source_ids": requested,
                         "idempotency_key": uuid.uuid4().hex}
        if draft is not None:
            payload["draft"] = draft
        body = self._submit_with_store_poll(
            f"/v1/stores/{store_id}/docs/remove", payload, poll_timeout)
        job = JobInfo(job_id=body.get("job_id", ""), store_id=store_id,
                      status=body.get("status", ""), kind="remove")
        if not wait:
            # provisional: the 202's registry pre-check, which is not
            # authoritative (and never is for a draft) — the sealed
            # record is. Said plainly by `version=None`.
            not_present = [s for s in (body.get("not_present") or [])]
            missing = set(not_present)
            return RemoveReport(requested=requested,
                                removed=[s for s in requested
                                         if s not in missing],
                                not_present=not_present,
                                version=None, job=job)
        job = self._wait_job(job, poll_timeout)
        if job.status == "error":
            raise IngestFailedError("remove job failed", job.job_id,
                                    job.error_code, remaining=len(requested))
        if job.status == "paused_budget":       # a remove embeds nothing
            raise BudgetPausedError(            # — defensive, never seen
                "the graph's daily budget paused this job; retry after "
                "the cap resets or contact us")
        removed: List[str] = []
        if job.version is not None:
            record = self.versions.get(job.version, store=store_id)
            removed = list(record.changes.removed)
        gone = set(removed)
        return RemoveReport(requested=requested, removed=removed,
                            not_present=[s for s in requested
                                         if s not in gone],
                            version=job.version, job=job)

    def job(self, job_id: str) -> JobInfo:
        return job_from_wire(self._request("GET", f"/v1/jobs/{job_id}",
                                           retries=2))

    # ---- graphs ---------------------------------------------------------
    def create_graph(self, name: str,
                     embedding_server: Optional[str] = None) -> GraphSummary:
        """Create a graph, instantly. One per project, or one per
        end-user of YOUR product: isolation is structural. The new
        graph starts `empty`; the first ingest makes it `ready`.

        `embedding_server`: the id or NAME of one of your registered
        embedding servers (see `client.embedding_servers`); omitted =
        SearchCandy's default Google embeddings. Chosen ONCE — a
        graph's vectors are built by this server for life; to move
        documents to another server, create a new graph and repoint
        anything using the old name."""
        payload = {"name": name}
        if embedding_server and embedding_server != "google-default":
            esid = embedding_server
            if not esid.startswith("es_"):
                esid = self.embedding_servers.get(embedding_server).id
            payload["embedding_server_id"] = esid
        body = self._request("POST", "/v1/stores",
                             json=payload, expected=(201,))
        self._graphs_cache = None      # names changed
        return GraphSummary(store_id=body.get("store_id"),
                            name=body.get("display_name"),
                            status=body.get("status", "empty"),
                            docs=0, nodes=0,
                            created_at=body.get("created_at"),
                            sealed_at=None)

    def graphs(self) -> List[GraphSummary]:
        """Every graph on your account (slim rows)."""
        body = self._request("GET", "/v1/stores", retries=2)
        rows = graphs_from_wire(body)
        self._graphs_cache = {g.name: g.store_id
                              for g in rows if g.name}
        return rows

    def _resolve_store(self, store: Optional[str]) -> Optional[str]:
        """Graph NAME → id (ids pass through untouched). The name
        map is cached; an unknown name refreshes once before failing
        loudly — a just-created graph resolves without any waiting."""
        if store is None or store.startswith("st_"):
            return store
        cache = getattr(self, "_graphs_cache", None)
        if cache is None or store not in cache:
            self.graphs()
            cache = self._graphs_cache or {}
        sid = cache.get(store)
        if sid is None:
            raise StoreAccessError(
                f"no graph named {store!r} on this account — "
                f"client.graphs() lists what exists")
        return sid

    # ---- store + usage --------------------------------------------------
    def store(self, store_id: Optional[str] = None) -> StoreInfo:
        sid = self._resolve_store(store_id) or self._default_store_id()
        body = self._request("GET", f"/v1/stores/{sid}", retries=2)
        return StoreInfo(store_id=body.get("store_id", sid),
                         display_name=body.get("display_name"),
                         status=body.get("status", ""),
                         embedding=body.get("embedding", {}),
                         counts=body.get("counts", {}),
                         current_version=body.get("current_version"),
                         head_version=body.get("head_version"))

    def usage(self, date_from: Optional[str] = None,
              date_to: Optional[str] = None) -> List[UsageDay]:
        params = {}
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to
        body = self._request("GET", "/v1/usage", params=params, retries=2)
        return [usage_from_wire(d) for d in body.get("days", [])]

    # ---- internals ------------------------------------------------------
    def _default_store_id(self) -> str:
        """Resolve the key's default store once (GET /v1/me)."""
        cached = getattr(self, "_default_store_cache", None)
        if cached:
            return cached
        me = self._request("GET", "/v1/me", retries=2)
        sid = me.get("default_store_id")
        if not sid:
            raise RequestError(
                "this key has no default store bound — pass store=... "
                "(your store id is in the portal), or ask us to bind "
                "a default")
        self._default_store_cache = sid
        return sid

    def _submit_batch(self, store_id: str, batch: List[dict],
                      poll_timeout: float,
                      draft: Optional[str] = None) -> JobInfo:
        payload: dict = {"documents": batch,
                         # ONE key per batch, forever
                         "idempotency_key": uuid.uuid4().hex}
        if draft is not None:
            payload["draft"] = draft
        body = self._submit_with_store_poll(
            f"/v1/stores/{store_id}/docs", payload, poll_timeout)
        return JobInfo(job_id=body["job_id"], store_id=store_id,
                       status=body["status"], kind="ingest")

    def _submit_with_store_poll(self, path: str, payload: dict,
                                poll_timeout: float) -> dict:
        """Submit a write job, waiting out the one-pipeline law.

        A 409 while another job indexes (or while the graph wakes up
        after a seal) is OUR pipeline's condition, not the customer's
        problem: the SDK polls the store back to ready with the SAME
        idempotency key, so a replayed submit returns the existing job
        instead of a second one."""
        deadline = time.monotonic() + poll_timeout
        while True:
            try:
                return self._request("POST", path, json=payload,
                                     retries=3, expected=(202,))
            except GraphNotReadyError as exc:
                if time.monotonic() > deadline:
                    raise SearchCandyError(
                        "store stayed busy past the poll timeout — a "
                        "long write is running; retry later or raise "
                        "poll_timeout") from None
                time.sleep(exc.retry_after_s or 5.0)

    def _wait_job(self, job: JobInfo, poll_timeout: float) -> JobInfo:
        deadline = time.monotonic() + poll_timeout
        delay = 2.0
        while time.monotonic() < deadline:
            j = self.job(job.job_id)
            if j.status in ("sealed", "error", "paused_budget"):
                return j
            time.sleep(delay)
            delay = min(delay * 1.5, 30.0)
        raise SearchCandyError(
            f"job {job.job_id} still {job.status} after "
            f"{poll_timeout:.0f}s — it may complete; check "
            f"client.job('{job.job_id}') or contact support")

    def _request(self, method: str, path: str, *, json=None, params=None,
                 retries: int = 0, expected=(200,)) -> dict:
        attempt = 0
        while True:
            try:
                resp = self._http.request(method, path, json=json,
                                          params=params)
            except httpx.HTTPError as exc:
                if attempt < retries:
                    attempt += 1
                    time.sleep(min(2.0 ** attempt, 8.0))
                    continue
                raise SearchCandyUnavailable(
                    f"could not reach the API after {attempt + 1} "
                    f"attempts: {type(exc).__name__}") from None
            if resp.status_code in expected:
                return resp.json()
            if resp.status_code in _RETRYABLE_STATUS and attempt < retries:
                attempt += 1
                time.sleep(min(2.0 ** attempt, 8.0))
                continue
            self._raise_for(resp)

    #: the CODE LADDER (VERSIONING-DESIGN §6 dispatcher rule). The API's
    #: `code` is authoritative and is consulted BEFORE any status
    #: shortcut: `version_not_found` is a 404 that is NOT an access
    #: problem, `draft_behind` is a 409 that is NOT a bad request, and
    #: a status-first dispatcher would tell the customer the wrong
    #: story about both. Status stays the fallback for codes we have
    #: never seen (a newer API than this SDK).
    _CODE_LADDER = {
        "version_not_found": VersionNotFound,
        "draft_not_found": DraftNotFound,
        "draft_closed": DraftClosed,
        "draft_behind": DraftBehind,
        "draft_exists": NameTaken,
        "store_unversioned": StoreUnversioned,
        "embedding_model_mismatch": EmbeddingModelMismatch,
        "graph_incomplete": GraphIncomplete,
        "graph_edges_unavailable": GraphNotReadyError,
        "store_not_ready": GraphNotReadyError,
        "store_warming": GraphNotReadyError,
        "over_embed_budget": BudgetPausedError,
        "byoe_upstream_error": YourEmbeddingEndpointError,
        "name_taken": NameTaken,
        "server_not_ready": ServerNotReady,
        "in_use": ServerInUse,
        "embedding_server_revoked": EmbeddingServerRevokedError,
        "secret_store_error": SearchCandyServerError,     # ours
        "unauthorized": AuthError,
        "forbidden": StoreAccessError,
        "forbidden_store": StoreAccessError,
        "not_found": StoreAccessError,
        "invalid_request": RequestError,
        "payload_too_large": RequestError,
        "limit_reached": RequestError,
        "probe_rate_exceeded": RequestError,
        "internal": SearchCandyServerError,               # ours
    }

    @classmethod
    def _raise_for(cls, resp: httpx.Response) -> None:
        try:
            err = resp.json().get("error", {})
        except Exception:  # noqa: BLE001 — non-JSON error body
            err = {}
        code = err.get("code", "")
        message = err.get("message", f"HTTP {resp.status_code}")
        rid = err.get("request_id")
        exc_type = cls._CODE_LADDER.get(code)
        if exc_type is GraphNotReadyError:
            # `store_warming` says how long in a Retry-After header —
            # read, never parsed out of the message (§5: the envelope
            # has no detail slot)
            raise GraphNotReadyError(message, rid,
                                     _retry_after(resp))
        if exc_type is not None:
            raise exc_type(message, rid)
        # ---- status fallback (an API newer than this SDK) -----------
        if resp.status_code == 401:
            raise AuthError(message, rid)
        if resp.status_code in (403, 404):
            raise StoreAccessError(message, rid)
        if resp.status_code in (400, 409, 413, 422, 429):
            # 429 here = a sanity cap (probe rate, server count, daily
            # version ops) — the message says what to do; never a
            # billing pause (that is over_embed_budget, mapped above)
            raise RequestError(message, rid)
        raise SearchCandyServerError(message, rid)


def _retry_after(resp: httpx.Response) -> Optional[float]:
    """The Retry-After header as seconds, or None. Never a guess."""
    raw = resp.headers.get("retry-after")
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None            # the HTTP-date form: we do not guess


def _refuse_repeated_source_ids(documents: Sequence[dict]) -> None:
    """One `source_id`, one document — checked BEFORE any HTTP.

    An existing source_id UPDATES a document, so two entries sharing
    one would silently replace each other and only one body would
    survive; which one is a race between batching and the server. The
    server refuses this too, but refusing here costs nothing and
    happens before a single byte is uploaded or embedded."""
    seen: dict = {}
    for i, d in enumerate(documents):
        sid = d.get("source_id")
        if sid is None:
            raise RequestError(
                f"documents[{i}]: source_id is required — it is the "
                f"identity we update a document by")
        if sid in seen:
            raise RequestError(
                f"duplicate source_id {sid!r}: documents[{seen[sid]}] "
                f"and documents[{i}] share it — an existing source_id "
                f"UPDATES the document, so only one body would "
                f"survive. Split one large document into parts with "
                f"DISTINCT source_ids; give a genuine revision the "
                f"same source_id in a LATER call, where it becomes a "
                f"new version.")
        seen[sid] = i


def _batches(documents: Sequence[dict]) -> Iterable[List[dict]]:
    """≤500 docs AND ≤8MB JSON weight per batch. A single oversized
    document is refused HERE with its name — not as a server 413."""
    batch: List[dict] = []
    weight = 0
    for d in documents:
        w = len(d.get("content", "").encode()) + 200
        if w > MAX_BATCH_BYTES:
            raise RequestError(
                f"document {d.get('source_id', '?')!r} is "
                f"{w // (1024 * 1024)}MB — over the "
                f"{MAX_BATCH_BYTES // (1024 * 1024)}MB per-request "
                f"limit; split it into parts with DISTINCT source_ids "
                f"— parts sharing one source_id would replace each "
                f"other, leaving only the last part in the graph — or "
                f"send it to us to convert")
        if batch and (len(batch) >= MAX_DOCS_PER_BATCH
                      or weight + w > MAX_BATCH_BYTES):
            yield batch
            batch, weight = [], 0
        batch.append(d)
        weight += w
    if batch:
        yield batch
