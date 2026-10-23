"""Versions — every sealed change to a graph is a numbered, immutable
snapshot you can ask questions of, forever.

    sc = SearchCandy()

    page = sc.versions(store="client-contracts")   # newest first
    for v in page:
        print(v.number, v.kind, v.created_at, v.changes.summary)

    sc.versions.get(3, store="client-contracts")            # one record
    sc.versions.as_of("2026-08-19T10:00:00Z", store="…")    # by date
    for sid in sc.versions.documents(3, store="…"): ...     # the doc set
    sc.versions.restore(1, store="client-contracts")        # → a NEW version

`current` is a pointer, versions are never rewritten, and restore is a
new version plus a pointer move — v4 and v5 stay askable after you
restore v3 ("kept, not current"). Restore is instant: no re-embedding,
no reseal, nothing to wait for.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .client import SearchCandy

#: the API's own ceiling for GET versions (§3 caps)
MAX_VERSION_PAGE = 100
#: default page size for versions() and the all()/documents() walkers
DEFAULT_VERSION_PAGE = 50


@dataclass(frozen=True)
class DocumentChange:
    """One document as a version recorded it. `bytes` is the content
    size we ingested, not the file on your disk."""
    source_id: str
    bytes: Optional[int] = None
    title: Optional[str] = None


@dataclass(frozen=True)
class VersionChanges:
    """What this version did to the document set. Exact, never
    sampled: one write job carries at most 500 documents.

    `added` and `updated` are disjoint — an existing `source_id` is an
    UPDATE (the old passages stop answering, the new ones start), a
    new one is an ADD. Pointer moves (restore, publish, open_draft)
    change no documents and carry none of these."""
    added: List[DocumentChange] = field(default_factory=list)
    updated: List[DocumentChange] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    @property
    def summary(self) -> str:
        """The console's own sentence: "12 documents added"."""
        parts = []
        if self.added:
            parts.append(f"{len(self.added)} document(s) added")
        if self.updated:
            parts.append(f"{len(self.updated)} document(s) updated")
        if self.removed:
            parts.append(f"{len(self.removed)} document(s) removed")
        return " · ".join(parts) or "no document changes"


@dataclass(frozen=True)
class Version:
    """One immutable, numbered snapshot of a graph's document set.

    `kind` ∈ ingest | remove | restore | publish | open_draft.
    `draft` names the draft line a version belongs to (None = the
    graph's main line, the only line `restore` accepts).
    `store_generation` names the substrate that produced the answer.

    `calibration` is the GUARANTEE this version carries (V21):
      "frozen"     — the version was sealed with its own measurement.
                     Replaying a question against it reproduces the
                     answer byte-for-byte, forever.
      "generation" — sealed before per-version freezing: identical
                     only within the store generation named above.
    A version never reports the stronger promise by default."""
    number: int
    kind: str
    created_at: Optional[str] = None
    created_via: Optional[str] = None
    parent: Optional[int] = None
    restored_from: Optional[int] = None
    draft: Optional[str] = None
    changes: VersionChanges = field(default_factory=VersionChanges)
    counts: Dict[str, int] = field(default_factory=dict)
    index_tier: Optional[str] = None
    store_generation: Optional[str] = None
    calibration: str = "generation"

    @property
    def docs(self) -> Optional[int]:
        """How many documents this version answers from."""
        c = self.counts.get("docs")
        return int(c) if c is not None else None

    def __str__(self) -> str:
        tail = (f" · restores v{self.restored_from}"
                if self.restored_from else f" · {self.changes.summary}")
        draft = f' · draft "{self.draft}"' if self.draft else ""
        return f"v{self.number} {self.created_at or ''}{draft}{tail}"


def _changes_from_wire(d: Optional[dict]) -> VersionChanges:
    d = d or {}

    def docs(key: str) -> List[DocumentChange]:
        return [DocumentChange(source_id=str(x.get("source_id", "")),
                               bytes=x.get("bytes"), title=x.get("title"))
                if isinstance(x, dict) else DocumentChange(source_id=str(x))
                for x in (d.get(key) or [])]

    return VersionChanges(added=docs("added"), updated=docs("updated"),
                          removed=[str(s) for s in (d.get("removed") or [])])


def version_from_wire(d: dict) -> Version:
    return Version(
        number=int(d.get("number") or 0),
        kind=str(d.get("kind") or ""),
        created_at=d.get("created_at"),
        created_via=d.get("created_via"),
        parent=d.get("parent"),
        restored_from=d.get("restored_from"),
        draft=d.get("draft"),
        changes=_changes_from_wire(d.get("changes")),
        counts=dict(d.get("counts") or {}),
        index_tier=d.get("index_tier"),
        calibration=d.get("calibration") or "generation",
        store_generation=d.get("store_generation"))


@dataclass(frozen=True)
class VersionDocument:
    """One document as it stood AT a version, with the version that
    last touched it. `changed_in` is a version number, never a guess:
    it comes from the ledger."""
    source_id: str
    bytes: Optional[int] = None
    title: Optional[str] = None
    changed_in: Optional[int] = None
    changed_at: Optional[str] = None

    def __str__(self) -> str:
        return self.source_id


def _document_from_wire(d: dict) -> VersionDocument:
    changed = d.get("changed_in") or {}
    if not isinstance(changed, dict):          # a bare version number
        changed = {"version": changed}
    return VersionDocument(source_id=str(d.get("source_id", "")),
                           bytes=d.get("bytes"), title=d.get("title"),
                           changed_in=changed.get("version"),
                           changed_at=changed.get("at"))


class VersionPage(Sequence):
    """One page of a graph's timeline, newest first — a real sequence
    (index it, slice it, len() it, iterate it) that also knows whether
    there is more: `.has_more` and `.next_before` (pass it back as
    `before=`). `versions.all()` walks every page for you."""

    def __init__(self, versions: List[Version], *, current: Optional[int],
                 head: Optional[int], has_more: bool,
                 next_before: Optional[int]):
        self._versions = list(versions)
        #: the version answering right now
        self.current = current
        #: the highest number ever minted on this graph
        self.head = head
        self.has_more = bool(has_more)
        self.next_before = next_before

    # ---- the sequence half -------------------------------------------------
    def __len__(self) -> int:
        return len(self._versions)

    def __getitem__(self, i):
        return self._versions[i]

    def __iter__(self) -> Iterator[Version]:
        return iter(self._versions)

    def __eq__(self, other) -> bool:
        if isinstance(other, VersionPage):
            return self._versions == other._versions
        if isinstance(other, list):
            return self._versions == other
        return NotImplemented

    def __repr__(self) -> str:
        if self.head is None:
            return ("<VersionPage 0 versions: this graph predates "
                    "versioning>")
        return f"<VersionPage {len(self)} of {self.head:,} versions>"


def page_from_wire(body: dict) -> VersionPage:
    return VersionPage(
        [version_from_wire(v) for v in body.get("versions", [])],
        current=body.get("current"), head=body.get("head"),
        has_more=bool(body.get("has_more")),
        next_before=body.get("next_before"))


class Versions:
    """`sc.versions` — call it for a page of the timeline, or use the
    methods. Bound to one client."""

    def __init__(self, client: "SearchCandy"):
        self._c = client

    # ---- read --------------------------------------------------------------
    def __call__(self, store: Optional[str] = None,
                 draft: Optional[str] = None, *,
                 limit: int = DEFAULT_VERSION_PAGE,
                 before: Optional[int] = None) -> VersionPage:
        """One page of this graph's timeline, newest first.

        `draft` scopes the page to a draft line. `before` pages
        backwards (`page.next_before` hands you the cursor). A graph
        that predates versioning answers an EMPTY page with
        `head=None` — never a fabricated v1."""
        if not 1 <= int(limit) <= MAX_VERSION_PAGE:
            raise _request_error(
                f"limit: 1..{MAX_VERSION_PAGE} (got {limit})")
        params: Dict[str, Any] = {"limit": int(limit)}
        if before is not None:
            params["before"] = int(before)
        if draft is not None:
            params["draft"] = draft
        return page_from_wire(self._c._request(
            "GET", f"/v1/stores/{self._store(store)}/versions",
            params=params, retries=2))

    def all(self, store: Optional[str] = None,
            draft: Optional[str] = None, *,
            page_size: int = DEFAULT_VERSION_PAGE) -> Iterator[Version]:
        """Every version, newest first, page after page — the audit
        export. A generator: it fetches a page at a time, so a graph
        with 10,000 versions costs one page of memory."""
        before = None
        while True:
            page = self(store, draft, limit=page_size, before=before)
            for v in page:
                yield v
            if not page.has_more or page.next_before is None:
                return
            before = page.next_before

    def get(self, number: int, store: Optional[str] = None) -> Version:
        """One version record: what it changed, what it counts, what
        substrate produced it. Never the document set — that is
        `versions.documents(n)`, which is paged."""
        body = self._c._request(
            "GET",
            f"/v1/stores/{self._store(store)}/versions/{int(number)}",
            retries=2)
        return version_from_wire(body.get("version") or {})

    def documents(self, number: int, store: Optional[str] = None, *,
                  page_size: int = 1000) -> Iterator[VersionDocument]:
        """The document set AS OF a version, in lexicographic
        `source_id` order — a paged iterator, because a graph's
        inventory can be large and no single response should carry it.

            for d in sc.versions.documents(3, store="contracts"):
                print(d.source_id, "changed in v%s" % d.changed_in)
        """
        sid = self._store(store)
        after: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"limit": int(page_size)}
            if after is not None:
                params["after"] = after
            body = self._c._request(
                "GET", f"/v1/stores/{sid}/versions/{int(number)}/documents",
                params=params, retries=2)
            rows = body.get("documents", [])
            for d in rows:
                yield _document_from_wire(d)
            if not body.get("has_more") or not body.get("next_after"):
                return
            after = body["next_after"]

    def as_of(self, timestamp: str,
              store: Optional[str] = None) -> Version:
        """The version that was answering at that instant — "what did
        this graph know on August 19th". `timestamp` is RFC3339 WITH an
        offset (`2026-08-19T10:00:00Z`): a naive one is ambiguous and
        is refused rather than guessed. Nothing before the graph's
        first version → VersionNotFound."""
        body = self._c._request(
            "GET", f"/v1/stores/{self._store(store)}/versions",
            params={"as_of": timestamp}, retries=2)
        return version_from_wire(body.get("version") or {})

    # ---- write (still reversible — V11) ------------------------------------
    def restore(self, number: int,
                store: Optional[str] = None) -> Version:
        """Make the documents of version `number` current again.

        Creates a NEW version ("v7 = restore of v4") and moves the
        graph's pointer — nothing is rewritten and nothing is lost:
        the versions you restored PAST stay in the history and stay
        askable by number. There is no re-embedding and no reseal, so
        there is nothing to wait for: the next question answers from
        the restored set.

        The version must be on the graph's own timeline (`draft` is
        None). To make a draft's content current, publish the draft."""
        body = self._c._request(
            "POST", f"/v1/stores/{self._store(store)}/restore",
            json={"version": int(number),
                  "idempotency_key": uuid.uuid4().hex},
            expected=(200,))
        return version_from_wire(body.get("version") or {})

    # ---- internals ---------------------------------------------------------
    def _store(self, store: Optional[str]) -> str:
        return (self._c._resolve_store(store)
                or self._c._default_store_id())


def _request_error(message: str):
    from .errors import RequestError
    return RequestError(message)
