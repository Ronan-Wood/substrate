# Changelog

Every release of the engine, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/). Each release attaches its source tarball and a
`SHA256SUMS` (see [`RELEASING.md`](RELEASING.md)).

## Unreleased

## 0.1.0

The first release from this repository. The engine was developed inside
[Scripta](https://github.com/Ronan-Wood/Scripta) and split out with its history. This is the
version Scripta pins when it stops carrying its own copy.

It speaks **wire version 1** and **note format 1**, and builds **index schema 11**.
`uv run python -m substrate.cli version` prints the wire version and the schema.

### Vaults and scopes

- `compose` reads a project vault's `.substrate.toml`, resolves `inherits`, and indexes the vaults
  as one named scope. A missing inherited vault, a malformed manifest or an invalid spine fails the
  compose rather than narrowing the scope.
- The spine on every note: `status` (active, complete, archived, superseded), `doc_type`,
  `confidence` and `domains`. Archived and superseded notes are excluded by default, and a
  superseded note surfaces as the `supersedes` link on its replacement.
- Tiers from a note's location, document classes with their own chunk policies, and versioned
  references made current by `reference_pins`.
- The scope registry (`scopes.toml`) and `register`, which records a scope for the engine's next
  refresh pass to compose. By default an index lives in `~/.substrate/index/<scope>.db`.
- `status`, `digests` and `frontmatter check|fix`: what a scope holds, which digests are behind,
  and which notes' frontmatter isn't YAML or reads differently from the engine.

### Ingestion

- Markdown through the standard library. PDF, Office, HTML, EPUB, CSV, WebVTT, AsciiDoc, LaTeX,
  email and images through docling, the PDF text layer and Apple's on-device recognizer.
  `substrate formats` lists what is accepted and why the rest is refused.
- A content-addressed store for the originals behind derived notes (`substrate blobs`), with a
  plan-then-confirm `gc`.

### Retrieval

- Lexical search with SQLite FTS5, with nothing else installed.
- Optional local arms through Ollama on loopback: an embedder (`qwen3-embedding:0.6b`), whose
  results are fused with the lexical ones by RRF, HyDE query expansion, and listwise or
  cross-encoder reranking.
- Every reply states which arms ran, what was filtered out, the index version, and the last
  refresh. Conversations are withheld unless the caller includes sources.

### Serving

- An MCP server with the tools `search`, `expand`, `documents`, `list_scopes`, `status`, `refresh`,
  and the write tools `ingest` and `amend`, over stdio, a Unix socket or loopback HTTP.
- `substrate serve`: the shared engine on `~/.substrate/run/substrate.sock`, refreshing every
  registered scope on a timer, configured by `~/.substrate/serve.toml`.
- `substrate-mcp --connect`: a stdio bridge to the shared socket.
- Agent mode: chosen scopes served read-only to another local user, from published snapshots.

### Security

- Model requests go only to loopback, with no proxy and no redirects, and the connected peer is
  checked.
- The socket sits in a `0700` directory, and both ends check the other's uid. The socket and HTTP
  transports are read-only by default, and HTTP refuses foreign `Host`, any `Origin`, and
  non-JSON bodies.
- Everything the engine writes is private to its user, except agent-mode snapshots, which are
  readable by their folder's group by design. A vault can declare a guard, an opt-in
  check designed to withhold its notes from the MCP server unless the guarded app vouches for them.

### For clients

- `contract/contract.json` and the golden frames in `contract/frames/`, both generated and checked
  by the test suite.
- A versioned socket wire, served as a range from `MIN_VERSION` to `VERSION`.

### Platform

- macOS on Apple silicon, Python 3.14. The core test suite also runs on Ubuntu in CI.
- OpenCV is built from source without FFmpeg, so no GPL FFmpeg is bundled.
