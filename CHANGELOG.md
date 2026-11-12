# Changelog

Every release of the engine, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/). Each release attaches its source tarball and a
`SHA256SUMS` (see [`RELEASING.md`](RELEASING.md)).

## 0.2.2

### Fixed

- The golden frames in `contract/frames/` are one capture again. In 0.2.1 the `status`,
  `documents` and `agent-status` frames came from an earlier capture than the rest, so the
  `status` frame named a different `index_version` from the `search` frame. A client that reads
  them as one index, as Scripta's decode tests do, failed. A test now holds the demo capture's
  `search`, `search-sources`, `expand`, `documents` and `status` frames to one `index_version`.

## 0.2.1

### Added

- **An engine write log, `~/.substrate/writes.jsonl`.** For each write the engine runs for a
  granted client, it appends an `intent` line, fsynced before the note is touched, then a `done`
  line after it, or a `void` line when the write was refused inside and nothing was written. A
  write the log can't record isn't run. Each line names the client, a prefix of its grant, the
  scope, the note, and the note's checksum before and after; never its content. The owner's own
  writes aren't logged. Nothing rewrites a line, and there is no rotation yet.
- **`provenance` on every passage**, in `search`, `expand` and `query --json`:
  - `state` is `engine` (the indexed bytes are what the engine last wrote, for `client`), `edited`
    (the engine wrote the note and it has changed since), `none` (no engine write) or `unknown`;
  - `stamp` says whether the note's own `amended_by`, else `written_by`, as the engine's reader
    reads the key, agrees with the log: `matches`, `contradicted`, or `unchecked` when there is
    no engine write to compare it with (a note stamped before the log, or by hand) or the note
    was not read. It is null when the note has none.

  `state` is judged against the write that produced the bytes the index holds, so an index that
  lags a write isn't reported as an edit, and the stamp is read from those same bytes. A note is
  found by its path only: one that moved reads `none`. Looked up at query time from the index's
  checksum and the log, so no recompose is needed. A note's stamp is remembered by the file's
  size and mtime, so an unchanged note isn't read again.
- **`substrate writes check [--scope S] [--json]`**, under the log's lock:
  - decides each write a crash left with neither `done` nor `void`: `done` when the note holds the
    bytes the write named, or a later write of the note replaced exactly those bytes; `void` when
    the note still holds what it held before (or a new note is absent);
  - lists damaged lines, and `seq` gaps, repeats and reorderings;
  - lists the notes whose stamp the log contradicts, as the engine's reader reads the key, each
    once however many scopes inherit it;
  - counts refused writes and stamps with no engine write to compare, and names the scopes it
    skipped (not composed yet, or an index it can't read).

  **It exits 1 for one thing only:** a write it could not decide, because its note holds neither
  the bytes it had before nor the bytes the write named. Everything else is history, reported
  and exiting 0, since nothing may rewrite the log to clear it.
- The `list_scopes` server block names the log as `writes_log`, null only in agent mode, which
  reads no log.

### Changed

- **Agent-mode snapshots leave out every call unless the scope's entry opts in.** A
  conversation-class note is removed from the snapshot, not filtered, for every published scope,
  and `include_sources = true` under `[publish.<scope>]` in `agent-publish.toml` publishes them.
  Before, the guard removed a guarded vault's own notes and every other scope's calls were
  published. An entry with only `dir` keeps working, now without its calls, except for a scope
  that holds nothing but calls, which refuses to publish. Any value of `include_sources` other
  than `true` or `false` is refused. The snapshot manifest records the choice, and a caller asking
  for sources from a snapshot without them is told so in `filters`.
- A published snapshot that may hold calls its entry does not let in is removed before anything
  else is tried, on a failed refresh too. That includes every snapshot an earlier engine
  published, so after upgrading, a scope that does not opt in serves nothing until its next
  successful refresh. `publish-snapshot --dir` into a folder other than the entry's takes no calls.
- A snapshot's rowids are renumbered, so their gaps no longer show how many documents and
  passages were left out of it.
- **Wire 3.** `provenance` and `writes_log` are new reply keys, so `VERSION` is 3. `MIN_VERSION`
  stays 1, and older clients ignore the keys.

### Removed

- **The vault guard.** `guard_state` is no longer a manifest key, and a manifest that still
  declares it composes, as with any key the engine does not read. Nothing is withheld for it:
  the MCP server returns the vault's own notes through `search`, `documents` and `expand` whatever
  the state file says, and `ingest` and `amend` no longer refuse the vault.
- The refusal of a stamp key spelled another way (`Written_By`, `"amended_by"`). It was a denylist
  that two review rounds showed can't be complete. The stamp is a label, and the write log is the
  record. A stamp key written exactly as the engine writes it is still refused when its value is
  wrong.

## 0.2.0

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
- **Frontmatter ends only at a whole `---` line**, with nothing after it but spaces or tabs. A line
  that merely starts with `---` (`---x: 1`, `----`, `--- x`) is inside the block, as Obsidian
  reads it. Before, it ended the block for the engine, so the lines after it were body to the
  engine and properties to Obsidian, and a granted client could put a stamp key there unchecked.
  `--- ` still closes for the engine while Obsidian reads on past it, so that one fence still
  reads differently. `frontmatter check` and `fix` use the reader's own rule, so `fix` no longer
  refuses a `--- ` fence.
- A note whose block ended only at such a line now reads the longer block when a whole `---` line
  follows, and has no frontmatter when none does. If that block names a key the engine reads,
  vault ingest refuses the note and `frontmatter check` lists it, naming the line. Run `substrate
  frontmatter check` after upgrading: the older check uses the old fence and can't see them.

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

### Consented writes

- `substrate serve --writes=granted` runs `ingest` and `amend` on the shared socket for an MCP
  client the user has granted by name, and refuses every other. `substrate grant <client>
  [--scopes a,b]` issues (or rotates) the client's token, `substrate revoke <client>` withdraws
  it, and `substrate grants [--json]` lists who may write and who asked. The engine re-reads the
  grants on every write, so neither needs a restart. See `docs/security.md` for what a grant does
  and does not protect against.
- **`substrate-mcp --connect` forwards `ingest` and `amend` to the engine** with the client's name
  and token, and no longer writes in its own process. With no engine, or no grant, nothing is
  written. Its check that the bridge and the engine read the same registry is gone with it.
  A bridge from an older release (wire 1, engine 0.1.x, such as the one Scripta 0.3.5 bundles)
  still writes in its own process, ungated and unstamped, beside a granted engine, until the
  client's helper is switched to this release's bridge (`tools/dev-engine helper dev`).
- A granted write is stamped in the note's frontmatter, `written_by: <client>` on a new note and
  `amended_by: <client>` on an edit, before it is planned, so the plan's diff and checksum include
  it. A note declaring another value is refused. A plan is bound to the client's grant token, so
  a rotation, or a revoke and a new grant, leaves no old plan redeemable. The stamp is a label,
  not proof: anyone who can edit the file can change it (`docs/security.md`).
- After a write, an engine that refreshes runs a pass for it, and the reply says when the note
  will be found, instead of telling the reader to compose.
- `--client NAME` on `--connect` names the client a write is granted and stamped as, where two
  hosts would otherwise send the same `clientInfo.name`.
- `tools/dev-engine` runs a source tree as the shared engine (`start`, `stop`, `status`) and
  switches the `substrate-mcp` helper script MCP clients run between it and the app's bundled
  bridge (`helper dev|app`).

### Security

- **The MCP `ingest` tool no longer reads a file by path.** It takes the note as `content`, with a
  `filename`, both now required, and refuses `source_path` with "read the file yourself and pass
  its text as `content`". Before, a model could name any file the engine could read, such as an
  SSH private key, and a plan and a commit copied it into a vault that may sync. The `--connect`
  bridge refuses it too, so it never forwards one. A client that reads a file itself can still
  pass its text; that read is the client's, under its own permissions. `substrate ingest <file>`
  on the command line still takes a path.

### Contract

- `ingest`'s `source_path` argument is removed, and `content` and `filename` are required, with no
  wire bump: no socket client sends `source_path`. A wire-1 bridge never forwards a write, a
  wire-2 bridge sends only `content`, and Scripta calls no write tool over the socket.
- Wire version 2; the engine serves 1 to 2. New request headers (`Substrate-Client`,
  `Substrate-Grant`, `Substrate-Client-Info`) and write tools on the socket, which a wire-2 client
  relies on. `contract.json` gains `grants`.
- **The `server` block's `read_only` is redefined** as "runs no write without a grant": true for
  `writes` `none` and `granted`, false only for `open`. The new `writes` key says which. No wire-1
  client acts on the engine refusing writes, so the range still starts at 1.
- No index schema change, and no note format change beyond note format 2 above: the stamp is a
  frontmatter key the reader ignores.

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
