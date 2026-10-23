"""Typed responses: the wire schema, made pleasant.

`not_covered` is a first-class FIELD, never an exception: it says no
supporting passage was returned, and your code decides what happens
next.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ContextAtom:
    """One served passage, word for word from your document."""
    text: str
    source_id: str
    node_id: str
    tokens: int


@dataclass(frozen=True)
class GraphSummary:
    """One row of client.graphs() — slim by design (an account can
    hold one graph per end-user of YOUR product).

    `docs`/`nodes` are the LIVE set of the current version: removing
    documents makes them go down. `current_version` is the version
    answering right now — None on a graph built before versioning
    (never a fabricated v1)."""
    store_id: str
    name: Optional[str]
    status: str
    docs: int
    nodes: int
    created_at: Optional[str]
    sealed_at: Optional[str]
    current_version: Optional[int] = None
    #: which embedding server built this graph's vectors (None = our
    #: default Google embeddings); chosen once, for the graph's life
    embedding_server_id: Optional[str] = None


def graphs_from_wire(body: dict) -> List[GraphSummary]:
    return [GraphSummary(store_id=g.get("store_id"),
                         name=g.get("name"),
                         status=g.get("status", ""),
                         docs=int(g.get("docs", 0)),
                         nodes=int(g.get("nodes", 0)),
                         created_at=g.get("created_at"),
                         sealed_at=g.get("sealed_at"),
                         current_version=g.get("current_version"),
                         embedding_server_id=g.get("embedding_server_id"))
            for g in body.get("graphs", [])]


@dataclass(frozen=True)
class Telemetry:
    total_tokens: int
    #: False when no single served span co-realizes everything the
    #: question demanded — a signal to read the evidence closely
    joint_coverage: Optional[bool]
    joint_coverage_max: Optional[int]
    n_demanded: Optional[int]
    stage_latencies_ms: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalResponse:
    query: str
    #: True = no supporting passage was returned. Not proof that none
    #: exists: the flag can be wrong both ways, so read the passages
    not_covered: bool
    context_atoms: List[ContextAtom]
    verbatim_quotes: List[ContextAtom]
    telemetry: Telemetry
    request_id: str
    store_id: str
    #: WHICH VERSION ANSWERED (VERSIONING-DESIGN V2). Flat on purpose:
    #: `r.version` is the number your audit row needs, and
    #: `query(q, version=r.version)` reproduces this answer.
    #: None on a graph built before versioning — never a fabricated v1.
    version: Optional[int] = None
    #: the draft line that answered, or None for the graph's own line
    draft: Optional[str] = None
    #: True when YOU pinned the version/draft; False when this is
    #: simply the graph's current answer
    pinned: bool = False
    #: what `current` was when this answer was served
    current_version: Optional[int] = None
    #: the substrate that produced it — the receipt's generation
    store_generation: Optional[str] = None
    #: THE GUARANTEE THIS RECEIPT MAKES (V21).
    #: "frozen"     — the answering version carries its own sealed
    #:                measurement: replay reproduces this answer
    #:                byte-for-byte, forever.
    #: "generation" — the version predates per-version freezing:
    #:                identical only within `store_generation`.
    #: Never reports the stronger promise by default.
    calibration: str = "generation"

    @property
    def behind(self) -> bool:
        """True when this answer came from an OLDER version than the
        graph's current one — i.e. you pinned the past. False for a
        current answer, and False on a graph with no versions."""
        if self.version is None or self.current_version is None:
            return False
        return int(self.version) < int(self.current_version)

    @property
    def refused(self) -> bool:
        """DEPRECATED alias for `not_covered`, kept so code written
        against 0.1.0 keeps running. `not_covered` names what happened:
        no supporting passage was returned. Removed at the next
        major."""
        return self.not_covered

    @property
    def quotes(self) -> List[ContextAtom]:
        """The per-source stand-out evidence (alias)."""
        return self.verbatim_quotes

    @property
    def atoms(self) -> List[ContextAtom]:
        return self.context_atoms


@dataclass(frozen=True)
class JobInfo:
    """One write job. `version` is the version its documents BECAME —
    set at the seal, so it is None until `status == "sealed"` (a
    number is never minted for bytes that are not on disk)."""
    job_id: str
    store_id: str
    status: str  # queued | running | sealed | error | paused_budget
    docs_submitted: Optional[int] = None
    docs_ingested: Optional[int] = None
    embed_tokens: Optional[int] = None
    error_code: Optional[str] = None
    #: ingest | remove — what this job did
    kind: Optional[str] = None
    #: the version this job sealed (None until sealed)
    version: Optional[int] = None
    #: how many documents were NEW, and how many REPLACED an existing
    #: source_id (disjoint: one source_id is one or the other)
    added: Optional[int] = None
    updated: Optional[int] = None


@dataclass(frozen=True)
class IngestReport:
    """One ingest() call = one or more sealed batches — and one
    VERSION per sealed batch (a 1,200-document folder seals as v1, v2,
    v3, not as one giant v1)."""
    docs: int
    jobs: List[JobInfo]
    embed_tokens: int
    skipped: List[str] = field(default_factory=list)  # folder mode

    @property
    def versions(self) -> List[int]:
        """The versions this upload became, in order. Empty with
        wait=False — nothing is sealed yet, so no number exists."""
        return [j.version for j in self.jobs
                if j.status == "sealed" and j.version is not None]

    @property
    def added(self) -> Optional[int]:
        """New documents across the sealed batches (None if the API
        did not report the split)."""
        return _sum_or_none(j.added for j in self.jobs)

    @property
    def updated(self) -> Optional[int]:
        """Documents that REPLACED an existing source_id."""
        return _sum_or_none(j.updated for j in self.jobs)


def _sum_or_none(values) -> Optional[int]:
    seen = [v for v in values if v is not None]
    return sum(seen) if seen else None


@dataclass(frozen=True)
class RemoveReport:
    """The outcome of one remove() — the SEALED truth, not a guess.

    `requested` is what you asked for; `removed` is what the version
    record says actually left `current`; `not_present` is the
    difference (documents that were not in the graph to begin with —
    not an error: a remove that removes nothing still seals a
    version). `version` is the version the removal became, and `job`
    is the write job that sealed it."""
    requested: List[str]
    removed: List[str]
    not_present: List[str]
    version: Optional[int]
    job: Optional[JobInfo] = None

    def __str__(self) -> str:
        v = f" → v{self.version}" if self.version else " (not sealed yet)"
        return (f"{len(self.removed)} document(s) removed, "
                f"{len(self.not_present)} not found{v}")


@dataclass(frozen=True)
class StoreInfo:
    """One graph in full. `counts` carries the LIVE set of the current
    version (removing documents makes it go down)."""
    store_id: str
    display_name: Optional[str]
    status: str
    embedding: Dict
    counts: Dict
    #: the version answering right now, and the highest ever minted —
    #: None on a graph built before versioning
    current_version: Optional[int] = None
    head_version: Optional[int] = None


@dataclass(frozen=True)
class UsageDay:
    date: str
    queries: int
    answered: int
    refused: int
    served_tokens: int
    docs_ingested: int
    embed_tokens: int
    embed_tokens_byoe: int
    joint_coverage_false: int
    avg_latency_ms: Optional[float]


def response_from_wire(body: dict) -> RetrievalResponse:
    r = body["result"]
    v = body.get("version") or {}
    atoms = [ContextAtom(**a) for a in r.get("context_atoms", [])]
    quotes = [ContextAtom(**a) for a in r.get("verbatim_quotes", [])]
    t = r.get("telemetry", {})
    return RetrievalResponse(
        query=r.get("query", ""),
        # the API sends both during the rename; prefer the new name
        not_covered=bool(r.get("not_covered", r.get("refused"))),
        context_atoms=atoms, verbatim_quotes=quotes,
        telemetry=Telemetry(
            total_tokens=t.get("total_tokens", 0),
            joint_coverage=t.get("joint_coverage"),
            joint_coverage_max=t.get("joint_coverage_max"),
            n_demanded=t.get("n_demanded"),
            stage_latencies_ms=dict(t.get("stage_latencies_ms", {}))),
        request_id=body.get("request_id", ""),
        store_id=body.get("store_id", ""),
        # the envelope's version block, FLATTENED (§6). Absent/None on
        # a graph that predates the ledger: the fields stay None
        # rather than inventing a v1 for bytes no number backs.
        version=v.get("number"),
        draft=v.get("draft"),
        pinned=bool(v.get("pinned")),
        current_version=v.get("current_version"),
        store_generation=v.get("store_generation"),
        calibration=v.get("calibration") or "generation")


def _count(value) -> Optional[int]:
    """`added`/`updated` on a job view are counts. Tolerate the list
    form too — a caller should never crash because the API answered
    with the source_ids instead of how many there were."""
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        return len(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def job_from_wire(body: dict) -> JobInfo:
    return JobInfo(job_id=body.get("job_id", ""),
                   store_id=body.get("store_id", ""),
                   status=body.get("status", ""),
                   docs_submitted=body.get("docs_submitted"),
                   docs_ingested=body.get("docs_ingested"),
                   embed_tokens=body.get("embed_tokens"),
                   error_code=body.get("error_code"),
                   kind=body.get("kind"),
                   version=body.get("version"),
                   added=_count(body.get("added")),
                   updated=_count(body.get("updated")))


def usage_from_wire(day: dict) -> UsageDay:
    return UsageDay(date=day.get("date", ""),
                    queries=day.get("queries", 0),
                    answered=day.get("answered", 0),
                    refused=day.get("refused", 0),
                    served_tokens=day.get("served_tokens", 0),
                    docs_ingested=day.get("docs_ingested", 0),
                    embed_tokens=day.get("embed_tokens", 0),
                    embed_tokens_byoe=day.get("embed_tokens_byoe", 0),
                    joint_coverage_false=day.get("joint_coverage_false", 0),
                    avg_latency_ms=day.get("avg_latency_ms"))
