"""searchcandy — grounded answers from your documents, or an honest
"not in your data." Never an invention."""
from .client import SearchCandy
from .errors import (AuthError, BudgetPausedError, IngestFailedError,
                     RequestError, SearchCandyError,
                     SearchCandyServerError, SearchCandyUnavailable,
                     StoreAccessError, YourEmbeddingEndpointError)
from .types import (ContextAtom, IngestReport, JobInfo,
                    RetrievalResponse, StoreInfo, Telemetry, UsageDay)

__version__ = "0.1.0"
__all__ = [
    "SearchCandy", "RetrievalResponse", "ContextAtom", "Telemetry",
    "IngestReport", "JobInfo", "StoreInfo", "UsageDay",
    "SearchCandyError", "AuthError", "StoreAccessError", "RequestError",
    "BudgetPausedError", "YourEmbeddingEndpointError",
    "IngestFailedError", "SearchCandyServerError",
    "SearchCandyUnavailable",
]
