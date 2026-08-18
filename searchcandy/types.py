"""Typed responses — the wire schema, made pleasant.

`refused` is a first-class FIELD, never an exception: an honest "not
in your data" is a correct answer, and the type system says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ContextAtom:
    """One served evidence span — byte-faithful to your document."""
    text: str
    source_id: str
    node_id: str
    tokens: int


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
    #: True = the honest "not in your data": zero tokens, no invention
    refused: bool
    context_atoms: List[ContextAtom]
    verbatim_quotes: List[ContextAtom]
    telemetry: Telemetry
    request_id: str
    store_id: str

    @property
    def quotes(self) -> List[ContextAtom]:
        """The per-source stand-out evidence (alias)."""
        return self.verbatim_quotes

    @property
    def atoms(self) -> List[ContextAtom]:
        return self.context_atoms


@dataclass(frozen=True)
class JobInfo:
    job_id: str
    store_id: str
    status: str  # queued | running | sealed | error | paused_budget
    docs_submitted: Optional[int] = None
    docs_ingested: Optional[int] = None
    embed_tokens: Optional[int] = None
    error_code: Optional[str] = None


@dataclass(frozen=True)
class IngestReport:
    """One ingest() call = one or more sealed batches."""
    docs: int
    jobs: List[JobInfo]
    embed_tokens: int
    skipped: List[str] = field(default_factory=list)  # folder mode


@dataclass(frozen=True)
class StoreInfo:
    store_id: str
    display_name: Optional[str]
    status: str
    embedding: Dict
    counts: Dict


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
    atoms = [ContextAtom(**a) for a in r.get("context_atoms", [])]
    quotes = [ContextAtom(**a) for a in r.get("verbatim_quotes", [])]
    t = r.get("telemetry", {})
    return RetrievalResponse(
        query=r.get("query", ""), refused=bool(r.get("refused")),
        context_atoms=atoms, verbatim_quotes=quotes,
        telemetry=Telemetry(
            total_tokens=t.get("total_tokens", 0),
            joint_coverage=t.get("joint_coverage"),
            joint_coverage_max=t.get("joint_coverage_max"),
            n_demanded=t.get("n_demanded"),
            stage_latencies_ms=dict(t.get("stage_latencies_ms", {}))),
        request_id=body.get("request_id", ""),
        store_id=body.get("store_id", ""))


def job_from_wire(body: dict) -> JobInfo:
    return JobInfo(job_id=body.get("job_id", ""),
                   store_id=body.get("store_id", ""),
                   status=body.get("status", ""),
                   docs_submitted=body.get("docs_submitted"),
                   docs_ingested=body.get("docs_ingested"),
                   embed_tokens=body.get("embed_tokens"),
                   error_code=body.get("error_code"))


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
