"""Offline checks of examples/demo/demo.py against a fake, in-memory API
(httpx's MockTransport): the script's own logic (load, timeline, asking
about a past date, the rehearsal check) is proven here; whether TGM
itself finds the right passages is proven only by `demo.py check`
against the real service.

    PYTHONPATH=. python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

from searchcandy import SearchCandy

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "demo", REPO / "examples" / "demo" / "demo.py")
demo = importlib.util.module_from_spec(_spec)
sys.modules["demo"] = demo        # dataclasses look their module up here
_spec.loader.exec_module(demo)


def _err(status, code, message="fake"):
    return httpx.Response(status, json={"error": {"code": code,
                                                  "message": message}})


class FakeTGM:
    """Graphs, versions and a word-overlap 'search' over paragraphs.
    Every version is sealed the moment it is submitted; the clock moves
    one minute per version so 'as of' has something to find."""

    def __init__(self):
        self.graphs = {}          # sid -> {"name", "versions": [...]}
        self.jobs = {}
        self.clock = datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc)
        #: questions still to refuse with 409 store_warming, as the API
        #: does while a graph reopens after a change
        self.warming = 0

    # -- helpers ----------------------------------------------------------
    def _docs_at(self, g, number):
        docs = {}
        for v in g["versions"][:number]:
            docs.update(v["_docs"])
        return docs

    def _summary(self, sid):
        g = self.graphs[sid]
        current = len(g["versions"]) or None
        return {"store_id": sid, "name": g["name"],
                "status": "ready" if current else "empty",
                "docs": len(self._docs_at(g, current or 0)), "nodes": 0,
                "created_at": "2026-10-27T16:59:00Z", "sealed_at": None,
                "current_version": current}

    @staticmethod
    def _wire(v):
        return {k: val for k, val in v.items() if not k.startswith("_")}

    def _answer(self, sid, body):
        if self.warming > 0:
            self.warming -= 1
            return httpx.Response(409, headers={"retry-after": "1"}, json={
                "error": {"code": "store_warming", "message": "opening"}})
        g = self.graphs[sid]
        current = len(g["versions"])
        number = int(body.get("version") or current)
        if not 1 <= number <= current:
            return _err(404, "version_not_found")
        words = set(re.findall(r"[a-z0-9]+", body["query"].lower()))
        scored = []
        for source_id, text in sorted(self._docs_at(g, number).items()):
            for para in text.split("\n\n"):
                para = para.strip()
                if para:
                    overlap = len(words & set(re.findall(r"[a-z0-9]+",
                                                         para.lower())))
                    scored.append((-overlap, source_id, para))
        scored.sort()
        atoms = [{"text": p, "source_id": s, "node_id": f"n{i}",
                  "tokens": len(p) // 4}
                 for i, (_, s, p) in enumerate(scored)]
        quotes, seen = [], set()
        for a in atoms:
            if a["source_id"] not in seen:
                seen.add(a["source_id"])
                quotes.append(a)
        return httpx.Response(200, json={
            "result": {"query": body["query"], "not_covered": not atoms,
                       "context_atoms": atoms, "verbatim_quotes": quotes,
                       "telemetry": {"total_tokens": 1}},
            "request_id": "req", "store_id": sid,
            "version": {"number": number, "current_version": current,
                        "pinned": "version" in body}})

    # -- the transport ----------------------------------------------------
    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if path == "/healthz":
            return httpx.Response(200, json={"ok": True, "version": "fake"})
        if path == "/v1/stores" and method == "GET":
            return httpx.Response(200, json={"graphs": [
                self._summary(s) for s in self.graphs]})
        if path == "/v1/stores" and method == "POST":
            if body["name"] in {g["name"] for g in self.graphs.values()}:
                return _err(409, "name_taken")
            sid = f"st_{len(self.graphs) + 1}"
            self.graphs[sid] = {"name": body["name"], "versions": []}
            return httpx.Response(201, json={
                "store_id": sid, "display_name": body["name"],
                "status": "empty", "created_at": "2026-10-27T16:59:00Z"})
        m = re.fullmatch(r"/v1/jobs/(\w+)", path)
        if m:
            return httpx.Response(200, json=self.jobs[m[1]])
        m = re.fullmatch(r"/v1/stores/(st_\d+)(/.*)?", path)
        if not m or m[1] not in self.graphs:
            return _err(404, "not_found")
        sid, rest, g = m[1], m[2] or "", self.graphs[m[1]]
        if rest == "" and method == "GET":
            s = self._summary(sid)
            return httpx.Response(200, json={
                "store_id": sid, "display_name": s["name"],
                "status": s["status"], "embedding": {},
                "counts": {"docs": s["docs"]},
                "current_version": s["current_version"],
                "head_version": s["current_version"]})
        if rest == "/docs" and method == "POST":
            before = self._docs_at(g, len(g["versions"]))
            docs = {d["source_id"]: d["content"] for d in body["documents"]}
            self.clock += timedelta(minutes=1)
            number = len(g["versions"]) + 1
            change = lambda sids: [{"source_id": s, "bytes": len(docs[s])}
                                   for s in sorted(sids)]
            g["versions"].append({
                "number": number, "kind": "ingest",
                "created_at": self.clock.isoformat().replace("+00:00", "Z"),
                "changes": {"added": change(set(docs) - set(before)),
                            "updated": change(set(docs) & set(before)),
                            "removed": []},
                "counts": {"docs": len(set(before) | set(docs))},
                "_docs": docs})
            job_id = f"job{len(self.jobs) + 1}"
            self.jobs[job_id] = {"job_id": job_id, "store_id": sid,
                                 "status": "sealed", "version": number,
                                 "embed_tokens": 1}
            return httpx.Response(202, json={"job_id": job_id,
                                             "status": "queued"})
        if rest == "/versions" and method == "GET":
            as_of = request.url.params.get("as_of")
            if as_of:
                t = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                hits = [v for v in g["versions"] if datetime.fromisoformat(
                    v["created_at"].replace("Z", "+00:00")) <= t]
                if not hits:
                    return _err(404, "version_not_found")
                return httpx.Response(200, json={
                    "version": self._wire(hits[-1])})
            return httpx.Response(200, json={
                "versions": [self._wire(v) for v in reversed(g["versions"])],
                "current": len(g["versions"]) or None,
                "head": len(g["versions"]) or None,
                "has_more": False, "next_before": None})
        m = re.fullmatch(r"/versions/(\d+)", rest)
        if m and method == "GET":
            return httpx.Response(200, json={
                "version": self._wire(g["versions"][int(m[1]) - 1])})
        if rest == "/query" and method == "POST":
            return self._answer(sid, body)
        return _err(404, "not_found", f"{method} {path}")


def run_demo(argv, sc=None):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), \
            mock.patch.object(demo.time, "sleep"):
        code = demo.run(argv, sc=sc)
    return code, out.getvalue()


class DemoOfflineTest(unittest.TestCase):

    def setUp(self):
        self.fake = FakeTGM()
        self.sc = SearchCandy(api_key="sc_live_test",
                              transport=httpx.MockTransport(self.fake))

    def test_uses_this_repo_client(self):
        self.assertTrue(Path(demo.searchcandy.__file__).resolve()
                        .is_relative_to(REPO))

    def test_questions_need_no_key(self):
        code, out = run_demo(["questions"])
        self.assertEqual(code, 0)
        for lib in demo.LIBRARIES.values():
            for q in lib.questions:
                self.assertIn(q.key, out)
        self.assertIn("ask contracts notice-before --before-update", out)

    def test_every_scripted_answer_is_in_the_data(self):
        # each question's words exist in the file version it will be
        # asked at, so a FAIL in `check` is about retrieval, not typos
        for lib in demo.LIBRARIES.values():
            for q in lib.questions:
                step = 1 if q.when == "before" else 2
                texts = [demo.local_text(lib, f, step) for f in q.files]
                self.assertNotIn(None, texts, (lib.name, q.key))
                for words in q.phrases:
                    self.assertTrue(any(words in t for t in texts),
                                    (lib.name, q.key, words))

    def test_load_reports_receipts_and_is_idempotent(self):
        code, out = run_demo(["load"], self.sc)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(g["name"] for g in self.fake.graphs.values()),
                         ["demo-contracts", "demo-help-center",
                          "demo-regulation"])
        for g in self.fake.graphs.values():
            self.assertEqual(len(g["versions"]), 2, g["name"])
        self.assertIn("added    services-agreement-northwind.md", out)
        self.assertIn("updated  services-agreement-northwind.md", out)
        self.assertIn("skipped  board-minutes.pdf: unsupported type (.pdf)",
                      out)
        code, again = run_demo(["load"], self.sc)
        self.assertEqual(code, 0)
        self.assertEqual(again.count("already loaded"), 6)
        for g in self.fake.graphs.values():
            self.assertEqual(len(g["versions"]), 2, "a re-run must not reload")

    def test_step_two_needs_step_one(self):
        code, out = run_demo(["load", "--step", "2", "--only", "contracts"],
                             self.sc)
        self.assertEqual(code, 2)
        self.assertIn("step 1 is not loaded", out)

    def test_check_passes_when_everything_comes_back(self):
        run_demo(["load"], self.sc)
        code, out = run_demo(["check"], self.sc)
        self.assertEqual(code, 0, out)
        total = sum(len(lib.questions) for lib in demo.LIBRARIES.values())
        self.assertIn(f"{total} of {total} ready", out)
        self.assertNotIn("FAIL", out)
        # the fake's word-overlap ranking does not put the refund line
        # first: a WARN that names the flag and the real position
        warn = [line for line in out.splitlines()
                if line.startswith("WARN  help-center  refund-now")]
        self.assertEqual(len(warn), 1, out)
        self.assertRegex(warn[0], r"on screen add (--all )?--limit \d+")
        limit = int(re.search(r"--limit (\d+)", warn[0])[1])
        code, shown = run_demo(["ask", "help-center", "refund-now", "--all",
                                "--limit", str(limit)], self.sc)
        self.assertIn("Monthly plans can be refunded within 14 days", shown)

    def test_check_fails_on_a_wrong_version_answer(self):
        run_demo(["load", "--only", "help-center"], self.sc)
        # the policy changes again after the demo was loaded: the
        # scripted 'now' answer is no longer the current version's
        self.sc.ingest([{"source_id": "refunds.md",
                         "content": "Refunds are not offered."}],
                       store="demo-help-center")
        code, out = run_demo(["check", "--only", "help-center"], self.sc)
        self.assertEqual(code, 1)
        self.assertIn("FAIL  help-center  refund-now", out)
        self.assertIn("missing words", out)

    def test_check_on_call_day_with_only_step_one(self):
        # step 2 kept back to load live on the call
        run_demo(["load", "--step", "1", "--only", "contracts"], self.sc)
        code, out = run_demo(["check", "--only", "contracts"], self.sc)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"(PASS|WARN)  contracts    notice-before")
        self.assertIn("SKIP  contracts    notice-now", out)
        self.assertIn("1 of 1 ready", out)
        self.assertIn("3 wait for step 2", out)
        code, out = run_demo(["ask", "contracts", "notice-now",
                              "--before-update"], self.sc)
        self.assertEqual(code, 2)
        self.assertIn("ask without --before-update", out)

    def test_check_says_not_loaded(self):
        code, out = run_demo(["check", "--only", "contracts"], self.sc)
        self.assertEqual(code, 1)
        self.assertIn("not loaded", out)

    def test_ask_before_the_update_answers_from_version_one(self):
        run_demo(["load"], self.sc)
        code, out = run_demo(["ask", "contracts", "notice-now",
                              "--before-update", "--all",
                              "--limit", "999"], self.sc)
        self.assertEqual(code, 0, out)
        self.assertIn("answered from version 1 (an earlier version, on "
                      "purpose)", out)
        self.assertIn("terminate this agreement with thirty days' written "
                      "notice.", out)
        self.assertNotIn("with sixty days' written notice.\n     word", out)
        self.assertIn("word for word: yes", out)
        self.assertNotIn("word for word: NO", out)

    def test_a_scripted_before_question_needs_no_flag(self):
        run_demo(["load", "--only", "contracts"], self.sc)
        code, out = run_demo(["ask", "contracts", "notice-before", "--all",
                              "--limit", "999"], self.sc)
        self.assertEqual(code, 0, out)
        self.assertIn("answered from version 1", out)
        self.assertIn("thirty days' written notice.", out)

    def test_waits_while_the_graph_reopens(self):
        run_demo(["load", "--only", "help-center"], self.sc)
        self.fake.warming = 2
        code, out = run_demo(["ask", "help-center", "refund-now"], self.sc)
        self.assertEqual(code, 0, out)
        self.assertIn("demo-help-center is opening after a change", out)
        self.assertIn("answered from version 2", out)

    def test_load_reports_how_long_each_step_took(self):
        code, out = run_demo(["load", "--only", "regulation"], self.sc)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(re.findall(
            r"took \d+\.\d s to load; the first question after it answered "
            r"in \d+\.\d s", out)), 2, out)

    def test_ask_now_and_as_of(self):
        run_demo(["load"], self.sc)
        # the fake ranks by shared words, so look at every passage: the
        # point here is the version, not the fake's ranking
        code, out = run_demo(["ask", "help-center", "refund-now", "--all",
                              "--limit", "999"], self.sc)
        self.assertEqual(code, 0, out)
        self.assertIn("answered from version 2", out)
        self.assertIn("within 14 days of payment", out)
        self.assertNotIn("within 30 days of payment", out)
        code, out = run_demo(["ask", "help-center", "refund-now",
                              "--as-of", "2020-01-01"], self.sc)
        self.assertEqual(code, 2)
        self.assertIn("nothing was loaded in demo-help-center", out)

    def test_timeline_and_status(self):
        run_demo(["load", "--only", "regulation"], self.sc)
        code, out = run_demo(["timeline", "regulation"], self.sc)
        self.assertEqual(code, 0, out)
        self.assertIn("version 1", out)
        self.assertIn("updated  17-cfr-240.15c6-1.md", out)
        self.assertIn("added    amendment-notice-88-fr-13872.md", out)
        self.assertIn("--before-update", out)
        code, out = run_demo(["status"], self.sc)
        self.assertEqual(code, 0, out)
        self.assertIn("demo-regulation: ready", out)
        self.assertIn("step 1 = version 1, step 2 = version 2", out)
        self.assertIn("demo-contracts: not created yet", out)

    def test_status_when_the_service_is_off(self):
        def off(request):
            raise httpx.ConnectTimeout("timed out", request=request)

        sc = SearchCandy(api_key="sc_live_test",
                         transport=httpx.MockTransport(off))
        code, out = run_demo(["status"], sc)
        self.assertEqual(code, 2)
        self.assertIn("NOT ANSWERING", out)


if __name__ == "__main__":
    unittest.main()
