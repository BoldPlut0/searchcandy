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
#: passages `ask` shows unless told otherwise (`--limit`)
SHOW_PASSAGES = 3
#: characters of a passage shown with `ask --short` (default: whole)
SHORT_CHARS = 700


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
    #: "quotes" shows the best passage of each file; "all" every served
    #: passage (two facts from ONE file need it)
    view: str = "quotes"


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
                 "two facts from two places in one contract", view="all"),
    )),
    Library("regulation", "Compliance: SEC Rule 15c6-1 (public text) and "
            "a made-up desk procedure", (
        Question("cycle-now",
                 "What is the standard settlement cycle for a "
                 "broker-dealer trade?",
                 "now", (RULE,),
                 ("later than the first business day after the date of "
                  "the contract",),
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
                  "Paragraph (a) of this section shall not apply to"),
                 "two facts: who books it, and the rule's exception"),
        Question("effective",
                 "When did the one-business-day settlement rule take "
                 "effect?",
                 "now", ("amendment-notice-88-fr-13872.md",),
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
        raise DemoError("step 2 is not loaded yet, so there is no 'before "
                        "the update': run `load --step 2` first")
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
                report = sc.ingest_folder(folder, store=graph)
                _receipt(sc, graph, folder, report)
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
                report = sc.ingest_folder(folder, store=graph)
                _receipt(sc, graph, folder, report)
    return 0


def cmd_ask(sc: SearchCandy, args) -> int:
    lib = LIBRARIES[args.library]
    graph = graph_name(lib, args.prefix)
    scripted = resolve_question(lib, args.question)
    question = scripted.text if scripted else args.question
    show_all = args.all or (scripted is not None and scripted.view == "all")
    steps = find_steps(sc, lib, graph)
    version: Optional[int] = None
    when = "the current version"
    if args.version is not None:
        version, when = args.version, f"version {args.version}, pinned"
    elif args.as_of or args.before_update:
        instant = (before_update(steps) if args.before_update
                   else parse_when(args.as_of))
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
    r = sc.query(question, store=graph, version=version)
    seconds = time.perf_counter() - started
    say()
    say(f"{lib.title}")
    say(f"Q: {question}")
    say(f"   asked {when}; answered from version {r.version}"
        f"{' (an earlier version, on purpose)' if r.behind else ''}"
        f" in {seconds:.2f} s")
    passages = r.context_atoms if show_all else r.quotes
    for i, p in enumerate(passages[:args.limit], 1):
        say()
        say(f"  {i}. {p.source_id} · version {r.version}")
        show_passage_text(p.text, args.short)
        say(f"     word for word: {word_for_word(lib, p, r.version, steps)[1]}")
    if r.not_covered:
        say()
        say("  no supporting passage was returned (not_covered=True)")
    return 0


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
            flag += " --all" if q.view == "all" else ""
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
    WARN = all of that holds, but `ask` would not show it in its first
    three passages: the note names the flag to use on screen."""
    rows = []
    for lib in selected(args.only):
        graph = graph_name(lib, args.prefix)
        steps = find_steps(sc, lib, graph)
        if steps[0] is None or steps[1] is None:
            rows.append(("FAIL", lib.name, "(all)", "-", "-",
                         "not loaded: run `load`"))
            continue
        current = sc.store(graph).current_version
        for q in lib.questions:
            notes: List[str] = []
            version = None
            if q.when == "before":
                version = sc.versions.as_of(before_update(steps),
                                            store=graph).number
            started = time.perf_counter()
            r = sc.query(q.text, store=graph, version=version)
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
            again = sc.query(q.text, store=graph, version=r.version)
            if ([(p.source_id, p.text) for p in again.context_atoms]
                    != [(p.source_id, p.text) for p in r.context_atoms]):
                notes.append("a second ask returned different passages")
            # where each needed piece lands in what `ask` shows
            places = _places(r.context_atoms if q.view == "all"
                             else r.quotes, q.phrases)
            verdict = "FAIL" if notes else "PASS"
            if verdict == "PASS" and (None in places
                                      or max(places) > SHOW_PASSAGES):
                verdict = "WARN"
                if None in places:      # not in a file's best passage
                    every = _places(r.context_atoms, q.phrases)
                    notes.append(f"on screen add --all --limit "
                                 f"{max(every)}")
                else:
                    notes.append(f"on screen add --limit {max(places)}")
            notes.append("shown at " + ", ".join(
                f"#{n}" if n else "not shown" for n in places))
            rows.append((verdict, lib.name, q.key, str(r.version),
                         f"{seconds:.2f}", "; ".join(notes)))
    width = max(len(r[2]) for r in rows)
    for verdict, lib_name, key, version, seconds, note in rows:
        say(f"{verdict}  {lib_name:12} {key:{width}}  v{version:3} "
            f"{seconds:>5} s  {note}")
    failed = sum(1 for r in rows if r[0] == "FAIL")
    warned = sum(1 for r in rows if r[0] == "WARN")
    say()
    say(f"{len(rows) - failed} of {len(rows)} ready"
        + (f" ({warned} need the flag in their note on screen)"
           if warned else "")
        + ("" if not failed else
           "; swap or drop each FAIL question before the call (DEMO.md)"))
    return 1 if failed else 0


def _places(passages, phrases) -> List[Optional[int]]:
    """Each phrase's first position (1-based) among `passages`."""
    return [next((i for i, p in enumerate(passages, 1)
                  if words in p.text), None) for words in phrases]


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
                     help="every served passage, not one per file")
    ask.add_argument("--short", action="store_true",
                     help=f"cut passages at {SHORT_CHARS} characters "
                          f"(default: whole passages)")
    ask.add_argument("--limit", type=int, default=SHOW_PASSAGES,
                     help=f"passages to show (default {SHOW_PASSAGES})")
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
