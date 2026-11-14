# Configuration

substrate reads two kinds of configuration. A vault describes itself in its own
`.substrate.toml`. The machine's state lives in the engine's folder, `~/.substrate`. You edit the
manifests and the `*.toml` files under `~/.substrate`, except `scopes.toml`, which the engine writes.
The engine refuses a file it can't read rather than guessing what it meant.

## The vault manifest: `.substrate.toml`

This file sits at the root of a project vault, and `compose` reads it. Here is the demo vault's,
with its comments shortened:

```toml
name = "demo"                                  # the scope name a caller asks for
inherits = ["demo-core-vault"]                 # the vaults loaded into this scope
reference_domains = ["software-dev", "distributed-systems"]

[reference_pins]                               # must come after every top-level key
go = "1.21"
```

| key | type | what it does |
|---|---|---|
| `name` | string, required | the scope name. The vault's folder name is used separately, as provenance |
| `inherits` | list of strings | vaults to compose. An absolute path is used as given. Anything else resolves against the project vault's **parent** folder, never the working directory. A vault that doesn't exist fails the compose |
| `reference_domains` | list of tags | the project's domains, carried as a tag. It never filters results |
| `reference_pins` | table, name to version | which version of a versioned reference counts as current for this project |
| `identity` | absolute path | a JSON file of people and things that notes mention, resolved on every compose |
| `skip` | list of names | the file and folder names compose never indexes in this vault. Without it, the default list below applies |

In TOML, every key written after a `[table]` header belongs to that table, so keep every top-level
key above `[reference_pins]`. Any key listed above, found inside a `[table]`, fails the compose
with a message naming the key and the table. The engine also checks the shape of `name`, `inherits`,
`reference_domains`, `reference_pins` and `skip`, and refuses a malformed one.

`skip` names what compose leaves out of the index:

- **An entry is one name.** An entry ending in `.md` is a file name, and skips every note with
  that name, in any folder. Any other entry is a folder name, and skips everything under every
  folder with that name. Names are matched exactly, inside the vault only: case matters, and an
  entry is not a path or a pattern. Two spellings of one accented name in different Unicode forms
  match.
- **With no `skip`, the default applies:** `["_meta.md", "structure.md", "MEMORY.md", "log.md",
  "WRITING.md", "99-templates"]`. A declared list replaces the default, so repeat the default names
  you still want skipped. `skip = []` indexes every `.md` file.
- **Each vault's list governs its own files.** A vault you inherit keeps its own `skip`, or the
  default when it has no manifest, whatever the project vault declares.
- **A skipped file that declares a spine fails the compose.** A file with `status`, `doc_type`,
  `confidence` or `domains` says it is a note, so the engine won't drop it without a word. So a
  file name in `skip` leaves out only files with none of those fields. To leave out a note that
  has them, put it under a skipped folder. Two things are never checked: a note under a skipped
  folder, and `_meta.md`, which is read for the passages beside it whether or not it is skipped.
- **A `skip` the engine can't apply fails the compose:** one that isn't a list of strings, or an
  entry that is empty, is `.` or `..`, starts or ends with white space, or holds `/`, `\`, `*`
  or `?`.
  The message names the faulty entries by position, not by value. A misspelt key, such as
  `skips`, is not caught: the engine ignores keys it doesn't know.
- **A refresh picks a change up.** After `skip` changes, the next compose or refresh pass takes
  the notes under a newly skipped folder out of the index and adds the newly unskipped ones.
- **`ingest` and `amend` never write a skipped note.** They refuse a name or a folder the vault's
  list skips. A default name the list leaves out is an ordinary note to them: under `skip = []`,
  `amend` edits `log.md`. The one exception is `_meta.md`, which they refuse in every vault and
  in any letter case, whatever its list says, because it sets the spine of the passages beside
  it. While the project
  vault's manifest doesn't parse, or its `skip` fails as above, they write nothing.

Where a note sits sets its **tier**: `00-operator/` is tier 1, `10-reference/` is tier 2, and
everything else is tier 3.

## A note's frontmatter

A note declares its spine in YAML frontmatter. A passage under `10-reference/` takes its spine
from the source's `_meta.md` instead. A note with no `status` composes as `active`, and a note with
no `doc_type` composes as `reference`. `substrate status --scope <name>` reports how many notes
were defaulted on each field, so you can tell when notes are retrieved by default without having
said so. A declared value outside the vocabulary fails the compose. This note declares both:

```markdown
---
title: Why the cache is content-addressed
status: active            # active, complete, archived or superseded
doc_type: explanation     # decision, explanation, reference, how-to or digest
domains: [storage]
---

# Why the cache is content-addressed
```

- `doc_id` is derived from the file name when it is absent. Declare one to keep a note's identity
  stable across renames.
- `confidence` is optional. Without it, a note is stored as `unjudged`.
- A note written through the `ingest` or `amend` tool must declare `status`, `doc_type` and
  `confidence`. The defaults apply only to notes already in a vault.
- A note that replaces another declares `supersedes: <doc_id>`, and the replaced note takes
  `status: superseded` and `superseded_by: <doc_id>`, as the two `engine-boundary` notes in the
  demo vault do.

The vocabularies are in [`contract/contract.json`](../contract/contract.json) under `spine`.
[`WRITING.md`](../vaults/demo-core-vault/00-operator/WRITING.md) describes how to write a note,
and `vaults/demo-vault/99-templates/` has templates to copy. To move an existing vault onto the
spine, read [`migration-vocabulary.md`](migration-vocabulary.md) first.

### When a default doesn't apply

A default stands in for a field a note doesn't declare. It doesn't stand in for frontmatter the
engine couldn't read, or for an engine key spelled another way. These are the cases, and what
compose does with each. For a passage, the same rules apply to its source's `_meta.md`.

| the note | what compose does |
|---|---|
| has no frontmatter, and doesn't start with a `---` line | composes with the defaults |
| starts with frontmatter the engine reads, or an empty `---`/`---` block | composes, with the defaults for what it doesn't declare |
| has no `status` or no `doc_type`, and starts with a `---` line that isn't frontmatter the engine reads | fails. This covers a byte-order mark, a blank line or an invisible character before the `---`, spaces after it, fences that smart punctuation turned into `—-` or `—`, a block that never closes or closes with `...`, a line that isn't a plain key (below), a key the engine reads written twice, and a horizontal rule as the note's first line |
| writes `status`, `doc_type`, `class`, `document_class` or `superseded_by` in another spelling: `Status:`, `doc-type:`, `docType:`, `"class":`, `superseded-by:` | fails, whatever else it declares. The message names the note, the key as written and the spelling to write |

**Exact spelling only.** The engine reads those five keys under exactly those names and no other.
`Status: archived` is not read as `status`, so the note fails rather than being indexed as live.
`Class: conversation` fails rather than being indexed as an ordinary note. A key counts as another
spelling of one of the five when they match after these steps: one pair of quotes and every
character that renders as nothing removed, letter case folded, and each run of `-`, `_` and
spaces made one `_`, where a capital after a lower-case letter may count as a word break or not.
So `DOC TYPE`, `DocType`, `docType` and `sTATUS` are spellings of engine keys, while `doctype`,
Pandoc's `documentclass` and Marp's `_class` are keys of their own and compose. **A vault that keeps a `Status` or `Class` property of
its own has those notes refused**, even beside a real `status`. Engine issue #53 decides how such
a vault is served.

**A plain key** is one YAML reads as a key. Either it is wrapped in one pair of matching quotes,
or it doesn't start with a YAML indicator character (`?` `:` `,` `[` `]` `{` `}` `#` `&` `*` `!`
`|` `>` `'` `"` `%` `@` or a backtick, or `-`), where `-`, `?` and `:` count only before a space
or nothing, so `-1` and `-x` are keys and `- x` is a list item. It also holds no ` #` and isn't
`<<`. Any script, spaces, hyphens, digits and punctuation inside a name are plain:
`Date created`, `related-notes`, `Rating (1-5)`, `автор` and `创建时间戳` all are. A list written
without indenting it (`source:` above `- https://…`) composes when YAML reads the block as the
same mapping, with the same value for each key that decides the spine or class (`status`,
`doc_type`, `class`, `document_class`, `superseded_by`, `supersedes`, `confidence`).
A key nested under another key belongs to that key, not to the note. Values aren't affected: they
are compared exactly as written, so `status: Archived` fails as a value outside the vocabulary.

**Frontmatter only.** The engine reads a note's spine and class only from its YAML frontmatter.
A `key:: value` line in the body is body text to the engine, whatever Dataview or Logseq make of
it, so a note that declares its class only that way is `unclassified`.

Until a later release lets one bad note be left out on its own, a note that fails the compose
stops the whole scope from composing, as before.

## The engine's folder: `~/.substrate`

| path | written by | what |
|---|---|---|
| `scopes.toml` | `compose`, `register` | **the registry**: each scope's vault, index and ingest tree, and when it was composed |
| `index/<scope>.db`, `index/<scope>/` | `compose` | the default home of a scope's index and its ingest tree |
| `run/substrate.sock` | `serve` | the default shared socket, in a `0700` directory |
| `serve.toml` | you | the models and the refresh period `serve` runs with |
| `config.toml` | you | settings every command reads. Today that is `[blobs] path`, where the store of originals lives |
| `blobs/` | `ingest`, `blobs` | the default store of originals behind derived notes |
| `vector-cache.db` | `embed`, `serve`, the refresh loop | content-addressed vectors that survive index rebuilds |
| `refresh.json` | the refresh loop | what the last pass did to each scope, reported in every search reply |
| `agent-publish.toml` | you | optional: which scopes to publish as agent-mode snapshots, and where |
| `agent-published.json` | the refresh loop, `publish-snapshot` | every folder a snapshot was published into for an `agent-publish.toml` entry, so it is withdrawn when the entry goes |
| `vaults.lock` | `compose`, `embed`, the refresh loop | serializes writers |

`refresh.json`, `agent-publish.toml`, `agent-published.json` and `vaults.lock` sit beside whichever registry is in use, so
pointing `$SUBSTRATE_REGISTRY` at another folder gives that folder its own set.

### The registry: `scopes.toml`

```toml
version = 1

[scopes.demo]
vault = "/absolute/path/to/vaults/demo-vault"
db = "/absolute/path/to/index.db"
index_root = "/absolute/path/to/index"
composed = "2026-01-01T00:00:00+00:00"
```

- It's machine-local and safe to delete. Recomposing rewrites it.
- Paths are absolute, because an MCP client launches the server from an arbitrary working directory.
- `register` records a scope with an empty `composed`. The engine's next refresh pass composes it,
  and until then a query on it is refused as pending.
- Every command takes `--registry PATH`. Without it, the engine uses `$SUBSTRATE_REGISTRY`, then
  `~/.substrate/scopes.toml`.

### `serve.toml`

```toml
hyde_model   = "auto"        # a model name, "auto" or "off"
rerank_model = "off"         # a model name or "off"
every        = 900           # seconds between refresh passes
```

- `substrate serve` reads this file once, at start. A flag given to `serve` overrides it key by key.
- A missing file means the defaults.
- A file that doesn't parse, an unknown key, a value of the wrong kind, or a cloud model's name
  refuses the start, naming the file and the key.
- There is deliberately no `embed_model` key. The embedder decides what every index holds, so a
  serve-only choice would match no vectors.

### Waiting for the lock

`compose`, `embed` and a refresh pass each take `vaults.lock`. A caller waits up to 60 seconds
(`--wait`, or `--no-wait` to decline at once). It then exits with code `75` having written nothing,
and a client should read `75` as "declined, try again later".

## Local models

Every model arm is optional, and each runs against Ollama on loopback (`http://127.0.0.1:11434`).

| arm | default model | turned on by |
|---|---|---|
| embedder | `qwen3-embedding:0.6b` | `substrate embed`, then queries without `--no-vector` |
| HyDE | `auto` | `query --full-stack`, `serve`, `hyde_model` |
| reranker | `dengcao/Qwen3-Reranker-4B:Q4_K_M` | `query --full-stack`, `serve`, `rerank_model` |

A reply's `retrieval_mode` field names the arms that ran. An arm that is unreachable is reported as
unavailable rather than silently skipped. [`EXPERIMENTS.md`](../EXPERIMENTS.md) records what each
arm was measured to be worth.
