"""Offline checks of the client: a fake API inside the test (httpx's
MockTransport), so nothing is installed and nothing leaves the machine.

    PYTHONPATH=. python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

import searchcandy
from searchcandy import SearchCandy, SearchCandyUnavailable
from searchcandy import client as client_mod
from searchcandy.embedding_servers import DEFAULT_VAULT_URL

REPO = Path(__file__).resolve().parent.parent


def _atom(text, source_id, node_id="n1", tokens=12):
    return {"text": text, "source_id": source_id, "node_id": node_id,
            "tokens": tokens}


class FakeAPI:
    """Just enough of the REST API for the README quickstart."""

    def __init__(self):
        self.requests = []
        self.uploaded = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if method == "GET" and path == "/v1/me":
            return httpx.Response(200, json={"default_store_id": "st_test"})
        if method == "POST" and path == "/v1/stores/st_test/docs":
            self.uploaded.extend(json.loads(request.content)["documents"])
            return httpx.Response(202, json={"job_id": "job_1",
                                             "status": "queued"})
        if method == "GET" and path == "/v1/jobs/job_1":
            return httpx.Response(200, json={
                "job_id": "job_1", "store_id": "st_test",
                "status": "sealed", "version": 1, "embed_tokens": 20,
                "added": 1, "updated": 0})
        if method == "POST" and path == "/v1/query":
            # the live service's shape (measured 2026-10-23): a file's
            # passages in its reading order, title first; `quotes` holds
            # the FIRST passage served from each file, so the title
            title = _atom("# Services Agreement\n\n## Termination\n",
                          "services-agreement.md", "n1", 4)
            clause = _atom("Either party may terminate this agreement with "
                           "sixty days' written notice.\n",
                           "services-agreement.md", "n2", 12)
            return httpx.Response(200, json={
                "result": {"query": json.loads(request.content)["query"],
                           "not_covered": False,
                           "context_atoms": [title, clause],
                           "verbatim_quotes": [title],
                           "telemetry": {"total_tokens": 16}},
                "request_id": "req_1", "store_id": "st_test",
                "version": {"number": 1, "current_version": 1,
                            "pinned": False}})
        return httpx.Response(404, json={"error": {
            "code": "not_found", "message": f"{method} {path}"}})


class ClientOfflineTest(unittest.TestCase):

    def test_imports_this_repo_copy(self):
        # the Mac has an older searchcandy installed; the tests must
        # never pass by reading it
        self.assertTrue(Path(searchcandy.__file__).resolve()
                        .is_relative_to(REPO), searchcandy.__file__)

    def test_version_strings_agree(self):
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        declared = re.search(r'^version = "([^"]+)"', pyproject, re.M)[1]
        self.assertEqual(searchcandy.__version__, declared)
        self.assertEqual(client_mod.SDK_VERSION, declared)

    def test_readme_quickstart_flow(self):
        api = FakeAPI()
        sc = SearchCandy(api_key="sc_live_test",
                         transport=httpx.MockTransport(api))
        with tempfile.TemporaryDirectory() as tmp:
            # the README's quickstart, step for step
            folder = Path(tmp) / "searchcandy-quickstart"
            folder.mkdir(exist_ok=True)
            (folder / "services-agreement.md").write_text(
                "# Services Agreement\n\n## Termination\n\n"
                "Either party may terminate this agreement with sixty "
                "days' written notice.\n", encoding="utf-8")
            (folder / "board-minutes.pdf").write_bytes(b"%PDF-1.4")
            report = sc.ingest_folder(folder)
        self.assertEqual(report.versions, [1])
        self.assertEqual([d["source_id"] for d in api.uploaded],
                         ["services-agreement.md"])
        self.assertEqual(len(report.skipped), 1)
        self.assertIn("unsupported type (.pdf)", report.skipped[0])
        self.assertNotIn("—", report.skipped[0])

        r = sc.query("How much notice do we need to cancel?")
        self.assertEqual(r.version, 1)
        self.assertFalse(r.not_covered)
        self.assertEqual([q.source_id for q in r.quotes],
                         ["services-agreement.md"])
        self.assertIn("sixty days' written notice",
                      r.context_atoms[1].text)

        first = api.requests[0]
        self.assertEqual(first.headers["authorization"],
                         "Bearer sc_live_test")
        self.assertEqual(first.headers["user-agent"],
                         f"searchcandy-python/{searchcandy.__version__}")

    def test_readme_quickstart_runs_as_written(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```python\n(.*?)```", readme, re.S)
        for block in blocks:                  # every example parses
            compile(block, "README.md", "exec")
        quickstart = blocks[0]
        self.assertIn("sc.ingest_folder(folder)", quickstart)
        api = FakeAPI()

        class Offline(SearchCandy):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(api)
                super().__init__(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(searchcandy, "SearchCandy", Offline), \
                mock.patch.dict("os.environ",
                                {"SEARCHCANDY_API_KEY": "sc_live_test"}), \
                mock.patch("builtins.print") as printed:
            cwd = Path.cwd()
            try:
                os.chdir(tmp)
                exec(quickstart, {"__name__": "readme_quickstart"})
            finally:
                os.chdir(cwd)
        lines = [" ".join(str(a) for a in c.args)
                 for c in printed.call_args_list]
        self.assertEqual(lines[0], "versions: [1] skipped: []")
        # the clause itself must be printed, not only the file's title
        self.assertIn("services-agreement.md | Either party may terminate "
                      "this agreement with sixty days' written notice.",
                      lines)
        self.assertEqual(lines[-1], "answered from version 1")

    def test_connect_timeout_is_short_and_reads_stay_long(self):
        sc = SearchCandy(api_key="sc_live_test")
        self.assertEqual(sc._http.timeout.connect, 10.0)
        self.assertEqual(sc._http.timeout.read, 75.0)
        quick = SearchCandy(api_key="sc_live_test", timeout=5.0)
        self.assertEqual(quick._http.timeout.connect, 5.0)
        # 0.3.0 accepted timeout=None ("no limit"); it must still work
        unbounded = SearchCandy(api_key="sc_live_test", timeout=None)
        self.assertIsNone(unbounded._http.timeout.read)
        self.assertEqual(unbounded._http.timeout.connect, 10.0)

    def test_unreachable_api_names_the_address(self):
        def refuse(request):
            raise httpx.ConnectTimeout("timed out", request=request)

        sc = SearchCandy(api_key="sc_live_test",
                         transport=httpx.MockTransport(refuse))
        with mock.patch.object(client_mod.time, "sleep") as slept:
            with self.assertRaises(SearchCandyUnavailable) as caught:
                sc.query("anything")
        message = str(caught.exception)
        self.assertIn("api.searchcandy-labs.com", message)
        self.assertIn("3 attempt(s)", message)
        self.assertIn("ConnectTimeout", message)
        self.assertEqual(slept.call_count, 2)    # retries=2 for reads

    def test_no_dead_hosts_and_the_vault_on_the_main_site(self):
        self.assertEqual(httpx.URL(DEFAULT_VAULT_URL).host,
                         "searchcandy-labs.com")
        self.assertTrue(DEFAULT_VAULT_URL.endswith(
            "/api/embedding-servers/vault"))
        checked = [REPO / "README.md", REPO / "pyproject.toml",
                   *sorted((REPO / "searchcandy").glob("*.py"))]
        for path in checked:
            text = path.read_text(encoding="utf-8")
            # 0.3.0's two dead hosts may be NAMED in a comment, never used
            for host in ("docs.searchcandy-labs.com",
                         "console.searchcandy-labs.com"):
                self.assertNotIn(f"https://{host}", text, path.name)

    def test_customer_pages_carry_no_long_dashes(self):
        for name in ("README.md", "pyproject.toml", "CHANGELOG.md"):
            text = (REPO / name).read_text(encoding="utf-8")
            self.assertNotIn("—", text, name)


if __name__ == "__main__":
    unittest.main()
