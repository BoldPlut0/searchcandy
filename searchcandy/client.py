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

from .errors import (AuthError, BudgetPausedError, IngestFailedError,
                     RequestError, SearchCandyError, SearchCandyServerError,
                     SearchCandyUnavailable, StoreAccessError,
                     YourEmbeddingEndpointError)
from .types import (IngestReport, JobInfo, RetrievalResponse, StoreInfo,
                    UsageDay, job_from_wire, response_from_wire,
                    usage_from_wire)

DEFAULT_BASE_URL = "https://api.searchcandy-labs.com"
ENV_KEY = "SEARCHCANDY_API_KEY"

MAX_DOCS_PER_BATCH = 500
#: 2MB under the server's 10MB body cap: JSON overhead headroom,
#: measured in the SDK tests rather than assumed
MAX_BATCH_BYTES = 8 * 1024 * 1024
LARGE_FILE_NOTE_BYTES = 2 * 1024 * 1024

_RETRYABLE_STATUS = (502, 503, 504)


class SearchCandy:
    """client = SearchCandy()          # key from SEARCHCANDY_API_KEY
    r = client.query("...")            # → RetrievalResponse
    client.ingest_folder("./docs")     # .txt/.md, skips loudly
    client.usage()                     # your meter, your invoice"""

    def __init__(self, api_key: Optional[str] = None,
                 base_url: str = DEFAULT_BASE_URL,
                 timeout: float = 75.0,
                 transport: Optional[httpx.BaseTransport] = None):
        key = api_key or os.environ.get(ENV_KEY)
        if not key:
            raise AuthError(
                "no API key: pass api_key=... or set the "
                f"{ENV_KEY} environment variable")
        self._http = httpx.Client(
            base_url=base_url, timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {key}",
                     "User-Agent": "searchcandy-python/0.1.0"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SearchCandy":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- queries --------------------------------------------------------
    def query(self, query: str,
              store: Optional[str] = None) -> RetrievalResponse:
        """Ask. Returns a grounded answer (byte-faithful quotes) or an
        honest refusal (`refused=True`, zero tokens) — refusal is a
        RESULT, not an exception."""
        path = (f"/v1/stores/{store}/query" if store else "/v1/query")
        body = self._request("POST", path, json={"query": query},
                             retries=2)
        return response_from_wire(body)

    # ---- ingest ---------------------------------------------------------
    def ingest(self, documents: Sequence[dict], *,
               store: Optional[str] = None, wait: bool = True,
               poll_timeout: float = 1800.0) -> IngestReport:
        """Upload documents ([{"content", "source_id", "title"?}]).
        Batches automatically; each batch gets ONE idempotency key
        reused across retries (the server replays, never duplicates).
        With wait=True (default) returns after every batch is sealed."""
        store_id = store or self._default_store_id()
        jobs: List[JobInfo] = []
        total_tokens = 0
        for batch in _batches(documents):
            job = self._submit_batch(store_id, batch,
                                     poll_timeout=poll_timeout)
            if wait:
                job = self._wait_job(job, poll_timeout)
                if job.status == "error":
                    raise IngestFailedError("ingest batch failed",
                                            job.job_id, job.error_code)
                if job.status == "paused_budget":
                    raise BudgetPausedError(
                        "daily embedding budget reached — batch is "
                        "paused; retry after the cap resets or "
                        "contact us")
                total_tokens += job.embed_tokens or 0
            jobs.append(job)
        return IngestReport(docs=len(documents), jobs=jobs,
                            embed_tokens=total_tokens)

    def ingest_folder(self, folder: Union[str, Path], *,
                      store: Optional[str] = None, wait: bool = True,
                      poll_timeout: float = 1800.0) -> IngestReport:
        """Upload every .txt/.md under `folder` (recursive).

        Loud by design: skipped files are NAMED in the report —
        silently eating a file you thought was indexed would be
        worse than any error."""
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
        report = self.ingest(docs, store=store, wait=wait,
                             poll_timeout=poll_timeout)
        return IngestReport(docs=report.docs, jobs=report.jobs,
                            embed_tokens=report.embed_tokens,
                            skipped=skipped)

    def job(self, job_id: str) -> JobInfo:
        return job_from_wire(self._request("GET", f"/v1/jobs/{job_id}",
                                           retries=2))

    # ---- store + usage --------------------------------------------------
    def store(self, store_id: Optional[str] = None) -> StoreInfo:
        sid = store_id or self._default_store_id()
        body = self._request("GET", f"/v1/stores/{sid}", retries=2)
        return StoreInfo(store_id=body.get("store_id", sid),
                         display_name=body.get("display_name"),
                         status=body.get("status", ""),
                         embedding=body.get("embedding", {}),
                         counts=body.get("counts", {}))

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
                      poll_timeout: float) -> JobInfo:
        idem = uuid.uuid4().hex        # ONE key per batch, forever
        path = f"/v1/stores/{store_id}/docs"
        deadline = time.monotonic() + poll_timeout
        while True:
            try:
                body = self._request(
                    "POST", path,
                    json={"documents": batch, "idempotency_key": idem},
                    retries=3, expected=(202,))
                return JobInfo(job_id=body["job_id"], store_id=store_id,
                               status=body["status"])
            except _StoreBusy:
                # another batch is indexing (one-pipeline law): wait
                # for the store, not the customer's patience
                if time.monotonic() > deadline:
                    raise SearchCandyError(
                        "store stayed busy past the poll timeout — a "
                        "long ingest is running; retry later or raise "
                        "poll_timeout")
                time.sleep(5.0)

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

    @staticmethod
    def _raise_for(resp: httpx.Response) -> None:
        try:
            err = resp.json().get("error", {})
        except Exception:  # noqa: BLE001 — non-JSON error body
            err = {}
        code = err.get("code", "")
        message = err.get("message", f"HTTP {resp.status_code}")
        rid = err.get("request_id")
        if resp.status_code == 401:
            raise AuthError(message, rid)
        if resp.status_code in (403, 404):
            raise StoreAccessError(message, rid)
        if code == "store_not_ready":
            raise _StoreBusy(message, rid)
        if code == "over_embed_budget":
            raise BudgetPausedError(message, rid)
        if code == "byoe_upstream_error":
            raise YourEmbeddingEndpointError(message, rid)
        if resp.status_code in (400, 409, 413, 422):
            raise RequestError(message, rid)
        raise SearchCandyServerError(message, rid)


class _StoreBusy(SearchCandyError):
    """Internal: 409 store_not_ready — handled by submit polling."""


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
                f"{w // (1024 * 1024)}MB — over the {MAX_BATCH_BYTES // (1024 * 1024)}MB "
                f"per-request limit; split it or send it to us to "
                f"convert")
        if batch and (len(batch) >= MAX_DOCS_PER_BATCH
                      or weight + w > MAX_BATCH_BYTES):
            yield batch
            batch, weight = [], 0
        batch.append(d)
        weight += w
    if batch:
        yield batch
