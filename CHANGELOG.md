# Changelog

## 0.3.1 (not released yet)

- **Fixed: the docs link.** The package pointed to
  `docs.searchcandy-labs.com`, an address that never existed. The docs
  live at https://searchcandy-labs.com/docs.
- **Fixed: registering your own embedding server.**
  `embedding_servers.add()` and `.rotate()` sent the key to
  `console.searchcandy-labs.com`, which also never existed, so they
  failed with `CredentialVaultError` ("could not reach the credential
  vault"). They now use the console's vault at
  https://searchcandy-labs.com/api/embedding-servers/vault
  (`SEARCHCANDY_VAULT_URL` still overrides it).
- **Faster failure when the API cannot be reached.** Opening a
  connection now gives up after 10 seconds per attempt (it was 75), so
  an unreachable API is reported in well under a minute instead of
  about four. Slow answers still get the full `timeout` (75 seconds by
  default). The error now names the address it tried.
- **README:** a quickstart that runs as written, asking about a past
  date with `versions.as_of`, and plain wording for `not_covered` (no
  supporting passage was returned; not proof that none exists).
- Messages no longer use long dashes; nothing else in the API changed.

## 0.3.0 (2026-08-28)

- **Versions and drafts.** Every sealed change to a graph is a numbered
  version you can ask, list (`versions()`), look up by date
  (`versions.as_of`) and restore (`versions.restore`). Drafts let you
  ask a change before it becomes current.
- `remove()` takes documents out of current answers as a new version.
- **Renamed:** the `refused` field is now `not_covered`. `refused`
  still works as a deprecated alias.
- New errors: `VersionNotFound`, `StoreUnversioned`, `DraftNotFound`,
  `DraftClosed`, `DraftBehind`, `NameTaken`, and the graph-integrity
  errors `GraphIncomplete` and `EmbeddingModelMismatch`.

## 0.1.0 (2026-08-18)

- First release: `ingest`, `ingest_folder`, `query`, `usage`, typed
  errors and safe retries.
