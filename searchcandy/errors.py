"""Typed failures — every one carries what to do next.

Design law (customer-path §3): failures must be un-ignorable in
customer code, and every server error carries a request_id the
customer can quote to support.
"""
from __future__ import annotations

from typing import Optional


class SearchCandyError(Exception):
    """Base for everything this SDK raises."""

    def __init__(self, message: str, request_id: Optional[str] = None):
        self.request_id = request_id
        if request_id:
            message = f"{message} (request_id: {request_id})"
        super().__init__(message)


class AuthError(SearchCandyError):
    """401 — missing or invalid API key. Is SEARCHCANDY_API_KEY set?"""


class StoreAccessError(SearchCandyError):
    """403/404 — the store id isn't one this key can use."""


class RequestError(SearchCandyError):
    """400/413 — the request itself was invalid (field named inside)."""


class BudgetPausedError(SearchCandyError):
    """Your daily embedding budget is reached; ingest is paused and
    resumes when the cap is raised (contact us) or the next UTC day.
    Deliberately NOT retried automatically — it is a billing guard."""


class YourEmbeddingEndpointError(SearchCandyError):
    """502 — your bring-your-own-embeddings endpoint failed or timed
    out. Check YOUR provider's status; our side is healthy."""


class IngestFailedError(SearchCandyError):
    """An ingest job ended in error server-side. The payload is
    retained for diagnosis — quote the job id to support."""

    def __init__(self, message: str, job_id: str,
                 error_code: Optional[str] = None):
        self.job_id = job_id
        self.error_code = error_code
        super().__init__(f"{message} (job {job_id}, code {error_code})")


class SearchCandyServerError(SearchCandyError):
    """5xx — our side. Quote the request_id to support."""


class SearchCandyUnavailable(SearchCandyError):
    """Network-level failure after retries — the API was unreachable."""
