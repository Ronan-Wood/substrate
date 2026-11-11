# Changelog

Every release of the engine, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/). Each release attaches its source tarball and a
`SHA256SUMS` (see [`RELEASING.md`](RELEASING.md)).

## Unreleased

### Fixed

- A YAML block list in frontmatter (`key:` then `  - item` lines, which Obsidian's Properties panel
  writes) no longer makes the reader drop the note's whole frontmatter. Before, the note lost its
  `status` and every other field, and `compose` refused it as having no status, which froze its
  whole scope.
- The reader now reads any frontmatter block that is not flat `key: value` lines through YAML
  (PyYAML, already a dependency, resolving no types). A flat block, which every engine and app
  writer emits, is read line by line exactly as before. So a block list, a comment, an indented
  key, a folded or quoted value over two lines all read as YAML reads them, to ingest, `amend`, a
  `_meta.md`, the skipped-file gate and `frontmatter check`.
- A value YAML reads that one line can't hold (a nested mapping, a multi-line scalar, a list of
  mappings), or that the line readers would read otherwise (an alias, a tag, a merge key `<<`, a
  key written twice), is left out. Under a key the engine reads, vault ingest refuses the note,
  and compose a `_meta.md` its passages inherit from, naming the key and the line. Before, an
  indented `key:` under another key was read as a top-level one, so `  status: archived` under
  `meta:` set the status.
- A block YAML can't read that names a key the engine reads is refused naming YAML's line. One that
  names none is body text, as a divider always was, and a vault note refused for its missing
  status now says why its block was not read.
- `frontmatter check` reports a value the engine leaves unread. Before, it called a note whose
  frontmatter the engine dropped clean.

### Changed

- **Note format 2** (`notes.version`; `min_version` stays 1, since a flat block, the only shape a
  format-1 writer emits, reads exactly as before): a writer may use block lists and any YAML that
  holds one-line values.
- Newly refused on the vault path, each naming the file and the line: a note or `_meta.md` with
  such a value under a key the engine reads; a note whose non-flat block YAML can't read (or is
  over 1 MiB, or holds a NEL, LS or PS) and that names such a key; `amend` of a note whose
  unreadable value is its `doc_id`, `status`, `superseded_by` or class; and a navigation file
  (`MEMORY.md`, `log.md`, `structure.md`, `WRITING.md`) whose non-flat frontmatter declares a
  spine field, as the same file in flow form always was. A note that opens
  with a divider whose block YAML reads as a mapping is frontmatter now, as Obsidian shows it. Run
  `substrate frontmatter check` before upgrading to list them.

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
