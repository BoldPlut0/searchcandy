"""Drafts — an unpublished line of your graph that you can ask
questions of before it becomes current.

    sc = SearchCandy()

    sc.drafts.open("policy-2026", store="client-contracts")   # forks from current
    sc.ingest([...], store="client-contracts", draft="policy-2026")
    sc.query("…", store="client-contracts", draft="policy-2026")   # the draft's answer
    sc.drafts.publish("policy-2026", store="client-contracts")     # → the new current

Publish is never a merge: what you tested is what ships. If versions
reached `current` after the draft was opened, publish is REFUSED the
way git refuses a non-fast-forward push — `DraftBehind` names them, and
`accept_behind=True` is the force. Discard closes a draft without
destroying anything: its versions stay listed and stay askable by
number, and `drafts.open(name, from_version=<its head>)` reopens the
work.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .errors import DraftBehind
from .versions import Version, version_from_wire

if TYPE_CHECKING:  # pragma: no cover
    from .client import SearchCandy

#: one follow-up read is all a DraftBehind is allowed (§6)
_BEHIND_LOOKBACK = 100


@dataclass(frozen=True)
class Draft:
    """One draft line of a graph.

    `base_version` is where it forked; `current_at_open` is what the
    graph's `current` was at that moment (publish compares against
    it); `head_version` is the draft's own newest version. `status` ∈
    open | published | discarded."""
    name: str
    status: str = "open"
    base_version: Optional[int] = None
    current_at_open: Optional[int] = None
    head_version: Optional[int] = None
    opened_at: Optional[str] = None
    opened_by: Optional[str] = None
    closed_at: Optional[str] = None
    #: the graph's `current` when this record was read — the other
    #: half of `behind`
    current_version: Optional[int] = None

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def behind(self) -> Optional[int]:
        """How many versions reached `current` after this draft was
        opened — publish refuses while this is non-zero unless you
        pass `accept_behind=True`. None when the API did not say."""
        if self.current_at_open is None or self.current_version is None:
            return None
        return max(0, int(self.current_version) - int(self.current_at_open))

    def __str__(self) -> str:
        return f'draft "{self.name}" ({self.status})'


def draft_from_wire(d: dict, current: Optional[int] = None) -> Draft:
    return Draft(name=str(d.get("name", "")),
                 status=str(d.get("status") or "open"),
                 base_version=d.get("base_version"),
                 current_at_open=d.get("current_at_open"),
                 head_version=d.get("head_version"),
                 opened_at=d.get("opened_at"),
                 opened_by=d.get("opened_by"),
                 closed_at=d.get("closed_at"),
                 current_version=current)


class Drafts:
    """`sc.drafts` — call it for the open drafts, or use the methods.
    Bound to one client."""

    def __init__(self, client: "SearchCandy"):
        self._c = client

    # ---- read --------------------------------------------------------------
    def __call__(self, store: Optional[str] = None) -> List[Draft]:
        return self.list(store)

    def list(self, store: Optional[str] = None) -> List[Draft]:
        """Every draft on this graph — the same list the console
        shows."""
        body = self._c._request(
            "GET", f"/v1/stores/{self._store(store)}/drafts", retries=2)
        current = body.get("current")
        return [draft_from_wire(d, current)
                for d in body.get("drafts", [])]

    # ---- write -------------------------------------------------------------
    def open(self, name: str, store: Optional[str] = None,
             from_version: Optional[int] = None) -> Draft:
        """Open a draft line, forked from `current` (or from any
        sealed version you name — that is also how you reopen a
        discarded draft: `from_version=<its head>`).

        Instant: opening a draft is a pointer move, not a rebuild. The
        draft is usable the moment this returns — ingest into it with
        `draft=`, ask it with `query(..., draft=)`."""
        payload: Dict[str, Any] = {"name": name,
                                   "idempotency_key": uuid.uuid4().hex}
        if from_version is not None:
            payload["from_version"] = int(from_version)
        body = self._c._request(
            "POST", f"/v1/stores/{self._store(store)}/drafts",
            json=payload, expected=(201,))
        return draft_from_wire(body.get("draft") or {},
                               body.get("current"))

    def publish(self, name: str, store: Optional[str] = None,
                accept_behind: bool = False) -> Version:
        """Make this draft's head the graph's `current`.

        A new version is created ("v7 = published 'policy-2026'") and
        the pointer moves — instant, no reseal, and every version on
        both lines stays askable by number.

        If versions reached `current` after the draft was opened,
        publish is REFUSED with `DraftBehind` listing them: that is
        git's non-fast-forward rule, and it is deliberate — publishing
        would take those versions out of `current`. `accept_behind=
        True` is the force. To keep them instead, open a NEW draft from
        current and re-add your files; we do not retain the original
        document bytes, so we cannot rebase for you."""
        sid = self._store(store)
        try:
            body = self._c._request(
                "POST", f"/v1/stores/{sid}/drafts/{name}/publish",
                json={"accept_behind": bool(accept_behind),
                      "idempotency_key": uuid.uuid4().hex},
                expected=(200,))
        except DraftBehind as exc:
            raise DraftBehind(
                str(exc).split(" (request_id")[0], exc.request_id,
                self._since_fork(sid, name)) from None
        return version_from_wire(body.get("version") or {})

    def discard(self, name: str, store: Optional[str] = None) -> None:
        """Close a draft. NOTHING is destroyed: its versions stay in
        the graph's history and stay askable by number, and the name
        is free to use again. Reopen the work with
        `drafts.open(name, from_version=<its head>)`."""
        self._c._request(
            "DELETE", f"/v1/stores/{self._store(store)}/drafts/{name}",
            expected=(200, 204))

    # ---- internals ---------------------------------------------------------
    def _since_fork(self, store_id: str, name: str) -> List[Version]:
        """The current-line versions a publish would drop — ONE
        follow-up `GET versions`, never a parse of the error message
        (§5: numbers a caller needs come from a read).

        The draft's own `open_draft` version records where it forked
        (its `parent`); everything on the main line above that is what
        publishing would take out of `current`."""
        try:
            page = self._c.versions(store_id, limit=_BEHIND_LOOKBACK)
        except Exception:      # noqa: BLE001 — enrichment is best-effort
            return []          # the typed error still fires, just bare
        fork_at = None
        for v in page:         # newest first: the latest open of this name
            if v.kind == "open_draft" and v.draft == name:
                fork_at = v.parent if v.parent is not None else v.number
                break
        if fork_at is None:
            return []
        return [v for v in page
                if v.draft is None and v.number > int(fork_at)]

    def _store(self, store: Optional[str]) -> str:
        return (self._c._resolve_store(store)
                or self._c._default_store_id())
