"""searchcandy: the Python client for TGM by SearchCandy Labs. TGM
hands your AI the exact passages from your own text and Markdown
files, each with its file and version.

VERSIONS and DRAFTS (0.3.0): every sealed change to a graph is a
numbered, immutable snapshot you can ask questions of forever, restore
in one instant call, or prepare in a draft before it ships.
"""
from .client import SearchCandy
from .drafts import Draft
from .embedding_servers import EmbeddingServer, RotationResult
from .errors import (AuthError, BudgetPausedError, CredentialVaultError,
                     DraftBehind, DraftClosed, DraftNotFound,
                     EmbeddingServerRevokedError, GraphNotReadyError,
                     IngestFailedError, NameTaken, RequestError,
                     SearchCandyError, SearchCandyServerError,
                     SearchCandyUnavailable, ServerInUse, ServerNotReady,
                     StoreAccessError, StoreUnversioned, VersionNotFound,
                     EmbeddingModelMismatch, GraphIncomplete,
                     YourEmbeddingEndpointError)
from .types import (ContextAtom, GraphSummary, IngestReport, JobInfo,
                    RemoveReport, RetrievalResponse, StoreInfo, Telemetry,
                    UsageDay)
from .versions import (DocumentChange, Version, VersionChanges,
                       VersionDocument, VersionPage)

__version__ = "0.3.1"
__all__ = [
    "SearchCandy", "RetrievalResponse", "ContextAtom", "Telemetry",
    "GraphSummary", "GraphNotReadyError",
    "IngestReport", "JobInfo", "StoreInfo", "UsageDay",
    "EmbeddingServer", "RotationResult",
    # versions + drafts (0.3.0)
    "Version", "VersionPage", "VersionChanges", "VersionDocument",
    "DocumentChange", "RemoveReport", "Draft",
    "VersionNotFound", "DraftNotFound", "DraftClosed", "DraftBehind",
    "StoreUnversioned",
    "EmbeddingModelMismatch",
    "GraphIncomplete",
    "SearchCandyError", "AuthError", "StoreAccessError", "RequestError",
    "BudgetPausedError", "YourEmbeddingEndpointError",
    "IngestFailedError", "SearchCandyServerError",
    "SearchCandyUnavailable", "NameTaken", "ServerNotReady",
    "ServerInUse", "EmbeddingServerRevokedError", "CredentialVaultError",
]
