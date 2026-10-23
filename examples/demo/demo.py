#!/usr/bin/env python3
"""The TGM demo: load three small document libraries, rehearse the
scripted questions, and ask them live.

    export SEARCHCANDY_API_KEY=sc_live_...       # a key that may create graphs
    python3 examples/demo/demo.py status          # is the service up? what is loaded?
    python3 examples/demo/demo.py load            # version 1, then version 2, of every library
    python3 examples/demo/demo.py check           # ask every scripted question: PASS or FAIL
    python3 examples/demo/demo.py questions       # the scripted questions, ready to copy
    python3 examples/demo/demo.py ask contracts notice-now
    python3 examples/demo/demo.py ask contracts notice-now --before-update
    python3 examples/demo/demo.py ask contracts "any question" --as-of 2026-10-27T10:04:00-07:00
    python3 examples/demo/demo.py timeline contracts

Each library has two versions. `load --step 1` loads the first and
`load --step 2` the change (an amended contract, an amended
regulation, an updated refund policy); `load` does both. Load step 1
a day before a call and step 2 on the day, and "as of yesterday"
shows version 1.

The contracts and the help center are made up. The regulation is
public U.S. government text: see README.md beside this file.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
if (REPO / "searchcandy" / "__init__.py").is_file():
    # this repo's client, never an older copy installed on this computer
    sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

import searchcandy  # noqa: E402
from searchcandy import (GraphNotReadyError, NameTaken,  # noqa: E402
                         SearchCandy, SearchCandyError, StoreAccessError,
                         VersionNotFound)

DATA = HERE / "data"
TEXT_TYPES = (".md", ".txt")
#: files `ask` shows unless told otherwise (`--files`); a scripted
#: question always shows every file that holds its answer
SHOW_FILES = 3
#: characters of a passage shown with `ask --short` (default: whole)
SHORT_CHARS = 700
#: seconds `ask`, `check` and `load` wait for a graph that is still
#: opening after a change (the API answers `store_warming` meanwhile)
OPEN_WAIT_SECONDS = 120


@dataclass(frozen=True)
class Question:
    key: str
    text: str
    #: "now" asks the current version; "before" asks the version that
    #: was answering just before step 2 was loaded
    when: str
    #: files that must come back, and words that must be in what comes
    #: back (the check's PASS rule)
    files: Tuple[str, ...]
    phrases: Tuple[str, ...]
    #: what this question shows, in the site's words
    shows: str


@dataclass(frozen=True)
class Library:
    name: str
    title: str
    questions: Tuple[Question, ...]

    @property
    def folder(self) -> Path:
        return DATA / self.name


NORTHWIND = "services-agreement-northwind.md"
RULE = "17-cfr-240.15c6-1.md"
NOTICE = "amendment-notice-88-fr-13872.md"
PROCEDURE = "settlement-desk-procedure.md"

LIBRARIES: Dict[str, Library] = {lib.name: lib for lib in (
    Library("contracts", "Legal: contracts (made up)", (
        Question("notice-now",
                 "How much notice do we need to give Northwind Health to cancel?",
                 "now", (NORTHWIND,),
                 ("Either party may terminate this agreement with sixty "
                  "days' written notice.",),
                 "the whole passage, with its file and version"),
        Question("notice-before",
                 "How much notice do we need to give Northwind Health to cancel?",
                 "before", (NORTHWIND,),
                 ("Either party may terminate this agreement with thirty "
                  "days' written notice.",),
                 "every version kept: the answer your AI got before the "
                 "amendment"),
        Question("biggest-customer",
                 "What's the notice period with our biggest customer?",
                 "now", ("customers.md", NORTHWIND),
                 ("Our biggest customer is Northwind Health",
                  "Either party may terminate this agreement with sixty "
                  "days' written notice."),
                 "a question that needs two facts, from two files"),
        Question("last-invoice",
                 "If we cancel the Northwind agreement, when is the last "
                 "invoice due?",
                 "now", (NORTHWIND,),
                 ("Either party may terminate this agreement with sixty "
                  "days' written notice.",
                  "Invoices are issued monthly and are payable within "
                  "thirty days of receipt."),
                 "two facts from two places in one contract"),
    )),
    Library("regulation", "Compliance: SEC Rule 15c6-1 (public text) and "
            "a made-up desk procedure", (
        Question("cycle-now",
                 "What is the standard settlement cycle for a "
                 "broker-dealer trade?",
                 "now", (RULE, NOTICE),
                 ("later than the first business day after the date of "
                  "the contract",
                  "to one business day after the trade date"),
                 "the whole passage, with its file and version"),
        Question("cycle-before",
                 "What is the standard settlement cycle for a "
                 "broker-dealer trade?",
                 "before", (RULE,),
                 ("later than the second business day after the date of "
                  "the contract",),
                 "every version kept: the rule as your AI read it before "
                 "the library was updated"),
        Question("syndicate",
                 "How long does the Syndicate Desk have to settle an "
                 "offering priced after 4:30 p.m.?",
                 "now", (PROCEDURE, RULE),
                 ("The Syndicate Desk books new issues",
                  "4:30 p.m. Eastern Time (ET)"),
                 "two facts: our procedure names the desk, the rule "
                 "gives the deadline"),
        Question("syndicate-before",
                 "How long does the Syndicate Desk have to settle an "
                 "offering priced after 4:30 p.m.?",
                 "before", (PROCEDURE, RULE),
                 ("The Syndicate Desk books new issues",
                  "later than the fourth business day after the date of "
                  "the contract"),
                 "two facts and a past date at once"),
        Question("alternatives",
                 "Does the Alternatives Desk have to settle within the "
                 "standard cycle?",
                 "now", (PROCEDURE, RULE),
                 ("The Alternatives Desk books trades in limited "
                  "partnership interests",
                  "Contracts for the purchase or sale of limited "
                  "partnership interests that are not listed on an "
                  "exchange"),
                 "two facts: who books it, and the rule's exception"),
        Question("effective",
                 "When did the one-business-day settlement rule take "
                 "effect?",
                 "now", (NOTICE,),
                 ("Effective date: May 5, 2023.",),
                 "the official notice, word for word"),
    )),
    Library("help-center", "Support: help center articles (made up)", (
        Question("refund-now",
                 "Can I get a refund on a monthly plan?",
                 "now", ("refunds.md",),
                 ("Monthly plans can be refunded within 14 days of "
                  "payment.",),
                 "the whole passage, with its file and version"),
        Question("refund-before",
                 "Can I get a refund on a monthly plan?",
                 "before", ("refunds.md",),
                 ("Monthly plans can be refunded within 30 days of "
                  "payment.",),
                 "every version kept: what your support AI read before "
                 "the policy changed"),
        Question("team-refund",
                 "I'm on the Team plan. How long do I have to ask for a "
                 "refund?",
                 "now", ("plans-and-pricing.md", "refunds.md"),
                 ("billed annually",
                  "Annual plans can be refunded within 30 days of the "
                  "start of the billing year."),
                 "a question that needs two facts, from two articles"),
        Question("team-sso",
                 "Can our Team plan use single sign-on with Okta?",
                 "now", ("plans-and-pricing.md", "single-sign-on.md"),
                 ("is available on the Enterprise plan only",),
                 "two facts: the plan, and what it includes"),
    )),
)}


class DemoError(Exception):
    """A plain-words stop: printed, exit code 2."""


# ---- small helpers ---------------------------------------------------------

def say(line: str = "") -> None:
    print(line, flush=True)


def graph_name(lib: Library, prefix: str) -> str:
    return f"{prefix}{lib.name}"


def text_files(folder: Path) -> List[str]:
    return sorted(p.name for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in TEXT_TYPES
                  and not p.name.startswith("."))


def local_text(lib: Library, source_id: str, step: int) -> Optional[str]:
    """The file as it stood after `step` (step 2 = step 1 plus v2/)."""
    for sub in (("v2", "v1") if step == 2 else ("v1",)):
        path = lib.folder / sub / source_id
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return None


def parse_ts(value: str) -> datetime:
    """An RFC3339 timestamp from the API (`Z`, up to nine decimals)."""
    s = value.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*\.\d{6})\d*(.*)$", s)
    if m:
        s = m.group(1) + m.group(2)
    return datetime.fromisoformat(s)


def local(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def parse_when(value: str) -> str:
    """What `--as-of` accepts, as the exact instant sent to the API:
    a full timestamp with a zone, a date and time without one (read as
    this computer's time zone), or a bare date (the end of that day,
    this computer's time zone)."""
    v = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            day = datetime.fromisoformat(v)
            dt = day.replace(hour=23, minute=59, second=59).astimezone()
        else:
            dt = parse_ts(v)
            if dt.tzinfo is None:
                dt = dt.astimezone()
    except ValueError:
        raise DemoError(f"--as-of {value!r}: use a date (2026-10-27) or "
                        f"a time (2026-10-27T10:04 or "
                        f"2026-10-27T10:04:00-07:00)") from None
    return dt.isoformat(timespec="seconds")


def find_steps(sc: SearchCandy, lib: Library, graph: str
               ) -> Tuple[Optional[object], Optional[object]]:
    """The versions that steps 1 and 2 became, read from the graph's
    own timeline (the one source of truth, whichever computer loaded
    them): step 1 = the first version that added every v1/ file, step 2
    = the first later version that added or updated every v2/ file."""
    want1 = set(text_files(lib.folder / "v1"))
    want2 = set(text_files(lib.folder / "v2"))
    try:
        versions = sorted(sc.versions.all(store=graph),
                          key=lambda v: v.number)
    except (GraphNotReadyError, StoreAccessError):
        return None, None        # not created yet, or nothing sealed yet
    step1 = step2 = None
    for v in versions:
        if v.draft:
            continue
        touched = ({d.source_id for d in v.changes.added}
                   | {d.source_id for d in v.changes.updated})
        if step1 is None:
            if want1 <= touched:
                step1 = v
        elif want2 <= touched:
            step2 = v
            break
    return step1, step2


def step_of(version: Optional[int], steps) -> Optional[int]:
    step1, step2 = steps
    if version is None or step1 is None:
        return None
    if step2 is not None and version >= step2.number:
        return 2
    if version >= step1.number:
        return 1
    return None


def before_update(steps) -> str:
    """An instant one second before step 2 was sealed."""
    step2 = steps[1]
    if step2 is None or not step2.created_at:
        raise DemoError("step 2 is not loaded yet, so the current version "
                        "IS the one before the update: ask without "
                        "--before-update, or run `load --step 2` first")
    return local(parse_ts(step2.created_at) - timedelta(seconds=1))


def word_for_word(lib: Library, passage, version: Optional[int],
                  steps) -> Tuple[Optional[bool], str]:
    step = step_of(version, steps)
    if step is None:
        return None, "not checked (a version this demo did not load)"
    text = local_text(lib, passage.source_id, step)
    if text is None:
        return None, "not checked (no local copy of this file)"
    if passage.text in text:
        return True, (f"yes, the exact text of {passage.source_id} as "
                      f"loaded in step {step}")
    return False, "NO: not found in the local copy (look into this)"


def query(sc: SearchCandy, question: str, graph: str,
          version: Optional[int]):
    """sc.query, waiting out a graph that is still opening after a
    change. The API says so (GraphNotReadyError) and, for a question,
    the SDK leaves the retry to the caller: on a call that must read
    as a short wait, never as a STOP."""
    deadline = time.monotonic() + OPEN_WAIT_SECONDS
    told = False
    while True:
        try:
            return sc.query(question, store=graph, version=version)
        except GraphNotReadyError as exc:
            if time.monotonic() > deadline:
                raise DemoError(f"{graph} still cannot answer after "
                                f"{OPEN_WAIT_SECONDS} s: {exc}") from None
            if not told:
                say(f"  ({graph} is opening after a change; waiting)")
                told = True
            time.sleep(exc.retry_after_s or 2.0)


def resolve_question(lib: Library, asked: str) -> Optional[Question]:
    for q in lib.questions:
        if q.key == asked:
            return q
    return None


def show_passage_text(text: str, short: bool) -> None:
    body = text.strip()
    if short and len(body) > SHORT_CHARS:
        body = body[:SHORT_CHARS].rsplit(" ", 1)[0] + " [...]"
    for line in body.splitlines():
        if not line.strip():
            say()
            continue
        say(textwrap.fill(line, width=92, initial_indent="     ",
                          subsequent_indent="     "))


# ---- the commands ----------------------------------------------------------

def cmd_status(sc: SearchCandy, args) -> int:
    say(f"client: searchcandy {searchcandy.__version__} "
        f"({Path(searchcandy.__file__).parent})")
    say(f"service: {sc._http.base_url}")
    started = time.perf_counter()
    try:
        health = sc._http.get("/healthz", timeout=10.0).json()
    except (httpx.HTTPError, ValueError) as exc:
        say(f"NOT ANSWERING ({type(exc).__name__}): the hosted service is "
            f"off or unreachable. DEMO.md, 'Before the call', brings it up.")
        return 2
    say(f"health: ok={health.get('ok')} build={health.get('version')} "
        f"({(time.perf_counter() - started) * 1000:.0f} ms)")
    graphs = {g.name: g for g in sc.graphs()}          # proves the key
    say(f"key: accepted ({len(graphs)} graph(s) on this account)")
    for lib in LIBRARIES.values():
        name = graph_name(lib, args.prefix)
        g = graphs.get(name)
        if g is None:
            say(f"  {name}: not created yet (run: load)")
            continue
        steps = find_steps(sc, lib, name)
        loaded = [f"step {i} = version {v.number}"
                  for i, v in enumerate(steps, 1) if v is not None]
        say(f"  {name}: {g.status}, {g.docs} documents, current version "
            f"{g.current_version}; {', '.join(loaded) or 'no step loaded'}")
    return 0


def _receipt(sc: SearchCandy, graph: str, folder: Path, report) -> None:
    for number in report.versions:
        v = sc.versions.get(number, store=graph)
        say(f"    version {number} sealed {local(parse_ts(v.created_at)) if v.created_at else ''}")
        for d in v.changes.added:
            size = f"{d.bytes:,} bytes" if d.bytes is not None else ""
            say(f"      added    {d.source_id}  {size}")
        for d in v.changes.updated:
            size = f"{d.bytes:,} bytes" if d.bytes is not None else ""
            say(f"      updated  {d.source_id}  {size}")
    for line in report.skipped:
        say(f"      skipped  {line.replace(str(folder) + os.sep, '')}")


def _ensure_graph(sc: SearchCandy, graph: str) -> None:
    if graph in {g.name for g in sc.graphs()}:
        return
    try:
        sc.create_graph(graph)
        say(f"  created graph {graph}")
    except NameTaken:
        pass                     # made a moment ago by another run


def _timed(sc: SearchCandy, lib: Library, graph: str,
           started: float) -> None:
    """Say how long the step took, then ask one scripted question so
    the graph is open again (it reopens after every change) before
    anyone watches. Together the two times are what a live update on
    a call costs."""
    loaded = time.perf_counter() - started
    asked = time.perf_counter()
    query(sc, lib.questions[0].text, graph, None)
    say(f"    took {loaded:.1f} s to load; the first question after it "
        f"answered in {time.perf_counter() - asked:.1f} s")


def cmd_load(sc: SearchCandy, args) -> int:
    for lib in selected(args.only):
        graph = graph_name(lib, args.prefix)
        say(f"{lib.title}  ->  graph {graph}")
        _ensure_graph(sc, graph)
        steps = find_steps(sc, lib, graph)
        loaded_now = False
        if args.step in ("1", "all"):
            if steps[0] is not None and not args.force:
                say(f"  step 1 already loaded as version {steps[0].number}")
            else:
                folder = lib.folder / "v1"
                say(f"  step 1: sc.ingest_folder({folder.relative_to(HERE)}, "
                    f"store={graph!r})   # no chunk size, no settings")
                started = time.perf_counter()
                report = sc.ingest_folder(folder, store=graph,
                                          poll_timeout=args.wait)
                _receipt(sc, graph, folder, report)
                _timed(sc, lib, graph, started)
                loaded_now = True
                steps = find_steps(sc, lib, graph)
        if args.step in ("2", "all"):
            if steps[0] is None:
                raise DemoError(f"{graph}: step 1 is not loaded; run "
                                f"`load --step 1` first")
            if steps[1] is not None and not args.force:
                say(f"  step 2 already loaded as version {steps[1].number}")
            else:
                if loaded_now and args.gap_seconds > 0:
                    say(f"  waiting {args.gap_seconds} s so the two "
                        f"versions carry clearly different times")
                    time.sleep(args.gap_seconds)
                folder = lib.folder / "v2"
                say(f"  step 2: sc.ingest_folder({folder.relative_to(HERE)}, "
                    f"store={graph!r})")
                started = time.perf_counter()
                report = sc.ingest_folder(folder, store=graph,
                                          poll_timeout=args.wait)
                _receipt(sc, graph, folder, report)
                _timed(sc, lib, graph, started)
    return 0


def cmd_ask(sc: SearchCandy, args) -> int:
    lib = LIBRARIES[args.library]
    graph = graph_name(lib, args.prefix)
    scripted = resolve_question(lib, args.question)
    question = scripted.text if scripted else args.question
    steps = find_steps(sc, lib, graph)
    # a scripted "before" question asks about the time before the
    # update even when the flag is forgotten (once step 2 exists;
    # until then the current version IS the one before the update)
    before = args.before_update or (scripted is not None
                                    and scripted.when == "before"
                                    and steps[1] is not None)
    version: Optional[int] = None
    when = "the current version"
    if args.version is not None:
        version, when = args.version, f"version {args.version}, pinned"
    elif args.as_of or before:
        instant = (parse_when(args.as_of) if args.as_of
                   else before_update(steps))
        try:
            v = sc.versions.as_of(instant, store=graph)
        except VersionNotFound:
            raise DemoError(f"nothing was loaded in {graph} at {instant}: "
                            f"`timeline {lib.name}` shows when each "
                            f"version was sealed") from None
        version = v.number
        when = (f"as of {instant}: version {v.number} was answering "
                f"(sealed {local(parse_ts(v.created_at)) if v.created_at else '?'})")
    started = time.perf_counter()
    r = query(sc, question, graph, version)
    seconds = time.perf_counter() - started
    say()
    say(f"{lib.title}")
    say(f"Q: {question}")
    say(f"   asked {when}; answered from version {r.version}"
        f"{' (an earlier version, on purpose)' if r.behind else ''}"
        f" in {seconds:.2f} s")
    files = served_files(r)
    say(f"   TGM returned {len(r.context_atoms)} passages "
        f"({r.telemetry.total_tokens:,} tokens) from {len(files)} file(s): "
        f"the file it ranks first comes first, each file's passages in "
        f"that file's own reading order")
    # the passages to point at: the answer words of every scripted
    # question with this text (its "now" AND "before" wording, so the
    # version that answered gets its own sentence marked), and whatever
    # --find names. Never a guess about the answer
    marks = [words for q in lib.questions if q.text == question
             for words in q.phrases]
    marks += args.find or []
    marked = [i for i, (_, ps) in enumerate(files, 1)
              if any(_has(p, marks) for p in ps)]
    show = max([args.files] + marked)
    for rank, (source_id, passages) in enumerate(files[:show], 1):
        hits = [p for p in passages if _has(p, marks)]
        if args.all or (not marks and rank == 1):
            shown = passages
        else:
            shown = hits
        checks = [word_for_word(lib, p, r.version, steps)[0]
                  for p in passages]
        say()
        say(f"  {rank}. {source_id} · version {r.version} · "
            f"{len(passages)} passage(s) · {_verdict(checks)}")
        for p in shown:
            show_passage_text(p.text, args.short)
            if p in hits:
                say("     ▲ the answer is in this passage")
        rest = len(passages) - len(shown)
        if rest:
            say(f"     (+ {rest} more passage(s) of this file, in its "
                f"reading order: --all shows them)")
    if len(files) > show:
        say()
        say(f"  (+ {len(files) - show} more file(s): "
            f"{', '.join(f for f, _ in files[show:])}; "
            f"--files {len(files)} shows them)")
    if r.not_covered:
        say()
        say("  no supporting passage was returned (not_covered=True)")
    return 0


def served_files(r) -> List[Tuple[str, list]]:
    """The served passages grouped by file, in the order TGM served
    them: the file it ranks first comes first, and each file's passages
    stay in that file's own reading order."""
    groups: Dict[str, list] = {}
    for p in r.context_atoms:
        groups.setdefault(p.source_id, []).append(p)
    return list(groups.items())


def _has(passage, marks: Sequence[str]) -> bool:
    text = passage.text.lower()
    return any(m.lower() in text for m in marks)


def _verdict(checks: Sequence[Optional[bool]]) -> str:
    if False in checks:
        bad = sum(1 for c in checks if c is False)
        return f"word for word: NO for {bad} of {len(checks)} (look into this)"
    if None in checks:
        return "word for word: not checked (not a file this demo loaded)"
    return f"word for word: yes ({len(checks)} of {len(checks)})"


def cmd_timeline(sc: SearchCandy, args) -> int:
    lib = LIBRARIES[args.library]
    graph = graph_name(lib, args.prefix)
    versions = sorted(sc.versions.all(store=graph), key=lambda v: v.number)
    say(f"{lib.title}  (graph {graph})")
    for v in versions:
        stamp = local(parse_ts(v.created_at)) if v.created_at else "?"
        say(f"  version {v.number}  {stamp}  {v.kind}: {v.changes.summary}")
        for d in v.changes.added:
            say(f"      added    {d.source_id}")
        for d in v.changes.updated:
            say(f"      updated  {d.source_id}")
        for s in v.changes.removed:
            say(f"      removed  {s}")
    say(f"  current: version {sc.store(graph).current_version}")
    steps = find_steps(sc, lib, graph)
    if steps[1] is not None:
        say(f"  to ask about the time before the update: "
            f"--as-of {before_update(steps)} (or --before-update)")
    return 0


def cmd_questions(sc: Optional[SearchCandy], args) -> int:
    me = "python3 examples/demo/demo.py"
    for lib in selected(args.only):
        say(f"{lib.title}")
        for q in lib.questions:
            flag = " --before-update" if q.when == "before" else ""
            say(f"  {q.key:17} {q.text}")
            say(f"  {'':17} shows: {q.shows}")
            say(f"  {'':17} {me} ask {lib.name} {q.key}{flag}")
        say()
    return 0


def cmd_check(sc: SearchCandy, args) -> int:
    """Ask every scripted question the way the call will, and say
    plainly which ones are ready: PASS needs the expected files and
    words back, the expected version, every passage word for word
    against the local file, and the same passages on a second ask.
    The note says where TGM ranked each needed file (`ask` always
    shows the files that hold a scripted answer).

    With only step 1 loaded (step 2 kept for the call, loaded live),
    the "before" questions are asked of the current version, which IS
    step 1, and the "now" questions are SKIP until step 2 is loaded."""
    rows = []
    for lib in selected(args.only):
        graph = graph_name(lib, args.prefix)
        steps = find_steps(sc, lib, graph)
        if steps[0] is None:
            rows.append(("FAIL", lib.name, "(all)", "-", "-",
                         "not loaded: run `load`"))
            continue
        only_step1 = steps[1] is None
        current = sc.store(graph).current_version
        for q in lib.questions:
            notes: List[str] = []
            version = None
            if only_step1 and q.when == "now":
                rows.append(("SKIP", lib.name, q.key, "-", "-",
                             "step 2 not loaded yet (checked after "
                             "`load --step 2`)"))
                continue
            if q.when == "before" and not only_step1:
                version = sc.versions.as_of(before_update(steps),
                                            store=graph).number
            started = time.perf_counter()
            r = query(sc, q.text, graph, version)
            seconds = time.perf_counter() - started
            served = list(r.context_atoms) + list(r.quotes)
            quote_files = [p.source_id for p in r.quotes]
            if r.not_covered:
                notes.append("not_covered=True")
            for f in q.files:
                if f not in {p.source_id for p in served}:
                    notes.append(f"missing file {f}")
            for words in q.phrases:
                if not any(words in p.text for p in served):
                    notes.append(f"missing words {words[:40]!r}")
            expected = steps[0].number if q.when == "before" else current
            if r.version != expected:
                notes.append(f"answered from version {r.version}, "
                             f"expected {expected}")
            for p in served:
                ok, _ = word_for_word(lib, p, r.version, steps)
                if ok is False:
                    notes.append(f"not word for word: {p.source_id}")
                    break
            again = query(sc, q.text, graph, r.version)
            if ([(p.source_id, p.text) for p in again.context_atoms]
                    != [(p.source_id, p.text) for p in r.context_atoms]):
                notes.append("a second ask returned different passages")
            # where TGM ranked each needed file, for the presenter
            order = [f for f, _ in served_files(r)]
            notes.append("ranked " + ", ".join(
                f"{f} #{order.index(f) + 1}" if f in order else f"{f} -"
                for f in q.files))
            verdict = "PASS" if len(notes) == 1 else "FAIL"
            rows.append((verdict, lib.name, q.key, str(r.version),
                         f"{seconds:.2f}", "; ".join(notes)))
    width = max(len(r[2]) for r in rows)
    for verdict, lib_name, key, version, seconds, note in rows:
        say(f"{verdict}  {lib_name:12} {key:{width}}  v{version:3} "
            f"{seconds:>5} s  {note}")
    failed = sum(1 for r in rows if r[0] == "FAIL")
    skipped = sum(1 for r in rows if r[0] == "SKIP")
    asked = len(rows) - skipped
    say()
    say(f"{asked - failed} of {asked} ready"
        + (f"; {skipped} wait for step 2" if skipped else "")
        + ("" if not failed else
           "; swap or drop each FAIL question before the call (DEMO.md)"))
    return 1 if failed else 0


def selected(only: Optional[str]) -> List[Library]:
    if not only:
        return list(LIBRARIES.values())
    names = [n.strip() for n in only.split(",") if n.strip()]
    unknown = [n for n in names if n not in LIBRARIES]
    if unknown:
        raise DemoError(f"--only: unknown {unknown}; choose from "
                        f"{', '.join(LIBRARIES)}")
    return [LIBRARIES[n] for n in names]


# ---- entry point -----------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Load, rehearse and run the TGM demo.")
    p.add_argument("--prefix", default="demo-",
                   help="graph names are <prefix><library> (default demo-)")
    p.add_argument("--base-url", default=None,
                   help="the API address (default: the SDK's)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="is the service up, what is loaded")
    load = sub.add_parser("load", help="load the demo libraries")
    load.add_argument("--step", choices=("1", "2", "all"), default="all")
    load.add_argument("--only", help="comma-separated libraries")
    load.add_argument("--gap-seconds", type=int, default=10)
    load.add_argument("--wait", type=int, default=300,
                      help="seconds to wait for one upload to seal before "
                           "giving up (default 300)")
    load.add_argument("--force", action="store_true",
                      help="load a step again even if it is loaded")
    check = sub.add_parser("check", help="rehearse every scripted question")
    check.add_argument("--only", help="comma-separated libraries")
    qs = sub.add_parser("questions", help="print the scripted questions")
    qs.add_argument("--only", help="comma-separated libraries")
    ask = sub.add_parser("ask", help="ask one question")
    ask.add_argument("library", choices=sorted(LIBRARIES))
    ask.add_argument("question", help="a scripted key or any question")
    when = ask.add_mutually_exclusive_group()
    when.add_argument("--as-of", help="a date or time to ask about")
    when.add_argument("--before-update", action="store_true",
                      help="as of just before step 2 was loaded")
    when.add_argument("--version", type=int)
    ask.add_argument("--all", action="store_true",
                     help="every served passage of every shown file")
    ask.add_argument("--find", action="append", metavar="WORDS",
                     help="also point at the passages containing WORDS "
                          "(case does not matter; repeatable)")
    ask.add_argument("--short", action="store_true",
                     help=f"cut passages at {SHORT_CHARS} characters "
                          f"(default: whole passages)")
    ask.add_argument("--files", type=int, default=SHOW_FILES,
                     help=f"files to show (default {SHOW_FILES}; more "
                          f"when a scripted answer needs them)")
    tl = sub.add_parser("timeline", help="every version of one library")
    tl.add_argument("library", choices=sorted(LIBRARIES))
    return p


COMMANDS = {"status": cmd_status, "load": cmd_load, "check": cmd_check,
            "questions": cmd_questions, "ask": cmd_ask,
            "timeline": cmd_timeline}


def run(argv: Sequence[str], sc: Optional[SearchCandy] = None) -> int:
    args = build_parser().parse_args(list(argv))
    try:
        if sc is None and args.command != "questions":
            key = os.environ.get("SEARCHCANDY_API_KEY")
            if not key:
                raise DemoError("set SEARCHCANDY_API_KEY first (DEMO.md, "
                                "'The demo key')")
            extra = {"base_url": args.base_url} if args.base_url else {}
            sc = SearchCandy(api_key=key, **extra)
        return COMMANDS[args.command](sc, args)
    except DemoError as exc:
        say(f"STOP: {exc}")
        return 2
    except SearchCandyError as exc:
        say(f"STOP: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
