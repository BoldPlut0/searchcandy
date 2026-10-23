"""Embedding servers — bring your own embedding endpoint, from code.

    sc = SearchCandy()                          # your normal API key

    srv = sc.embedding_servers.add(
        name="prod-openai", kind="openai",
        model="text-embedding-3-large",
        api_key=os.environ["OPENAI_API_KEY"])
    if not srv.ready:                           # a failed probe is a RESULT
        print(srv.status, srv.status_reason, srv.status_detail)

    for s in sc.embedding_servers():            # the same list the portal shows
        print(s.name, s.status, s.dimension)

    sc.create_graph("client-contracts", embedding_server="prod-openai")

What happens on `add`, in order — three calls, and the middle one is
the only place the key travels:

1. `POST /v1/embedding-servers` — registers the NAME/kind/model/endpoint
   with the hosted API. No secret is in this request. The API answers
   with the server id and the derived secret name.
2. The key is vaulted through SearchCandy's console vault route
   (`vault_url`, default searchcandy-labs.com/api/embedding-servers/
   vault; `SEARCHCANDY_VAULT_URL` overrides it) as ONE Secret
   Manager version under that derived name. The hosted API never
   accepts a secret in a body; the vault route is authenticated with
   the same API key. The plaintext is sent once, over TLS, and never
   stored by this SDK.
3. `POST /v1/embedding-servers/{id}/test?version=…` — the hosted API
   reads that version and actually EMBEDS three sentences with your
   endpoint. `ready` means it worked; `failed` names why (your side);
   `unknown` means WE could not complete the check — try again.

Revoke and delete are portal / ops actions (they destroy a credential);
this SDK lists, adds, tests, and rotates — by design.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional

import httpx

from .errors import (AuthError, CredentialVaultError, RequestError,
                     StoreAccessError)

if TYPE_CHECKING:  # pragma: no cover
    from .client import SearchCandy

#: the console is served from the main site (0.3.0 named a
#: console.searchcandy-labs.com host that never existed)
DEFAULT_VAULT_URL = "https://searchcandy-labs.com/api/embedding-servers/vault"
ENV_VAULT_URL = "SEARCHCANDY_VAULT_URL"

@dataclass(frozen=True)
class RotationResult:
    """The outcome of one rotate(): the NEW key was probed; `promoted`
    says whether it became the active one. When it did not, the
    previous key is still serving — nothing broke."""
    verdict: str                    # ready | failed | unknown
    reason: Optional[str]
    detail: Optional[Dict]
    promoted: bool
    note: str


@dataclass(frozen=True)
class EmbeddingServer:
    """One registered embedding server (or the built-in Google row).
    `status` is the truth the hosted API keeps: `ready` means the last
    probe embedded successfully; `failed` names YOUR endpoint's problem
    in `status_reason` (+ `status_detail`); `unknown` means our check
    could not complete; `revoked` means we hold no credential."""
    id: str
    name: str
    kind: str
    model: Optional[str]
    endpoint: Optional[str]
    dimension: Optional[int]
    status: str
    status_reason: Optional[str]
    status_detail: Optional[Dict]
    key_hint: Optional[str]
    in_use: int
    builtin: bool
    created_at: Optional[str] = None
    last_probe_at: Optional[str] = None
    last_probe_ms: Optional[float] = None
    credential_generation: Optional[int] = None
    #: set only on the record returned by rotate()
    last_rotation: Optional[RotationResult] = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"


def server_from_wire(d: dict,
                     rotation: Optional[dict] = None) -> EmbeddingServer:
    rot = None
    if rotation:
        rot = RotationResult(verdict=rotation.get("verdict", "unknown"),
                             reason=rotation.get("reason"),
                             detail=rotation.get("detail"),
                             promoted=bool(rotation.get("promoted")),
                             note=rotation.get("note", ""))
    return EmbeddingServer(
        id=d.get("id", ""), name=d.get("name", ""), kind=d.get("kind", ""),
        model=d.get("model"), endpoint=d.get("endpoint"),
        dimension=d.get("dimension"), status=d.get("status", ""),
        status_reason=d.get("status_reason"),
        status_detail=d.get("status_detail"), key_hint=d.get("key_hint"),
        in_use=int(d.get("in_use", 0)), builtin=bool(d.get("builtin")),
        created_at=d.get("created_at"), last_probe_at=d.get("last_probe_at"),
        last_probe_ms=d.get("last_probe_ms"),
        credential_generation=d.get("credential_generation"),
        last_rotation=rot)


class EmbeddingServers:
    """`sc.embedding_servers` — call it for the list, or use the
    methods. Bound to one client."""

    def __init__(self, client: "SearchCandy", vault_url: str):
        self._c = client
        self._vault_url = vault_url

    # ---- read --------------------------------------------------------------
    def __call__(self) -> List[EmbeddingServer]:
        return self.list()

    def list(self) -> List[EmbeddingServer]:
        """Every server on the account, Google first — identical to the
        portal's dropdown."""
        body = self._c._request("GET", "/v1/embedding-servers", retries=2)
        return [server_from_wire(s) for s in body.get("servers", [])]

    def get(self, id_or_name: str) -> EmbeddingServer:
        """By id (`es_…`) or by name. Unknown → StoreAccessError."""
        for s in self.list():
            if s.id == id_or_name or s.name == id_or_name:
                return s
        raise StoreAccessError(
            f"no embedding server {id_or_name!r} on this account; "
            f"client.embedding_servers() lists what exists")

    # ---- write -------------------------------------------------------------
    def add(self, name: str, kind: str, *, api_key: str,
            model: Optional[str] = None,
            endpoint: Optional[str] = None) -> EmbeddingServer:
        """Register + vault + test. Returns the record; read `.status`.
        kind ∈ openai | cohere | hf | vllm | http. `endpoint` only for
        hf/vllm/http (OpenAI/Cohere URLs are fixed). No dimension
        argument: the probe OBSERVES what your server returns."""
        if not api_key and kind in ("openai", "cohere"):
            raise RequestError(f"api_key: required for kind={kind}")
        body = {"name": name, "kind": kind}
        if model is not None:
            body["model"] = model
        if endpoint is not None:
            body["endpoint"] = endpoint
        created = self._c._request("POST", "/v1/embedding-servers",
                                   json=body, expected=(201,))
        esid = created["id"]
        version = self._vault(esid, api_key or "")
        return self.test(esid, _version=version)

    def test(self, server_id: str, *, _version: Optional[str] = None
             ) -> EmbeddingServer:
        """Re-run the connection test. A failed probe is a RESULT."""
        params = {"version": _version} if _version else None
        body = self._c._request(
            "POST", f"/v1/embedding-servers/{server_id}/test",
            params=params, expected=(200,))
        return server_from_wire(body.get("server", {}))

    def rotate(self, server_id: str, *, api_key: str) -> EmbeddingServer:
        """Verify-then-promote: the new key is vaulted as a new version
        and PROBED; only if it works does it become the active key, and
        the old one is destroyed. If it does not work, the previous key
        keeps serving — read `.last_rotation`."""
        if not api_key:
            raise RequestError("api_key: required")
        version = self._vault(server_id, api_key)
        body = self._c._request(
            "POST", f"/v1/embedding-servers/{server_id}/rotate",
            params={"version": version}, expected=(200,))
        return server_from_wire(body.get("server", {}),
                                rotation=body.get("rotation"))

    # ---- the vault hop ----------------------------------------------------
    def _vault(self, server_id: str, api_key: str) -> str:
        """POST the key to the console vault route — the ONE hop where
        the plaintext travels — and return the secret VERSION name the
        hosted API should probe. The record stays `awaiting_credential`
        if this fails (visible in the portal; `test()` later picks up a
        version vaulted there)."""
        try:
            resp = self._c._http.post(
                self._vault_url, json={"server_id": server_id,
                                       "api_key": api_key},
                timeout=30.0)
        except httpx.HTTPError as exc:
            raise CredentialVaultError(
                f"could not reach the credential vault ({type(exc).__name__})"
                f"; server {server_id} is registered but has no "
                f"credential yet: vault it from the portal or retry",
                server_id=server_id) from None
        if resp.status_code == 401:
            raise AuthError("the vault refused this API key", None)
        if resp.status_code in (403, 404):
            raise StoreAccessError(
                f"the vault does not know server {server_id} for this key",
                None)
        if resp.status_code >= 400:
            raise CredentialVaultError(
                f"the credential vault answered {resp.status_code}; "
                f"server {server_id} is registered but has no credential "
                f"yet; retry, or vault it from the portal",
                server_id=server_id)
        try:
            version = resp.json().get("version")
        except ValueError:
            version = None
        if not version:
            raise CredentialVaultError(
                "the credential vault answered without a version name",
                server_id=server_id)
        return str(version)
