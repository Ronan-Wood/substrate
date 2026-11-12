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

In TOML, every key written after a `[table]` header belongs to that table, so keep every top-level
key above `[reference_pins]`. Any key listed above, found inside a `[table]`, fails the compose
with a message naming the key and the table. The engine also checks the shape of `name`, `inherits`,
`reference_domains` and `reference_pins`, and refuses a malformed one.

Where a note sits sets its **tier**: `00-operator/` is tier 1, `10-reference/` is tier 2, and
everything else is tier 3.

## A note's frontmatter

Every note declares its spine in YAML frontmatter. A passage under `10-reference/` takes its spine
from the source's `_meta.md` instead. A note with no `status` fails the compose, because the
engine won't guess which retrieval set it belongs in. This is the smallest note that composes:

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
- A note that replaces another declares `supersedes: <doc_id>`, and the replaced note takes
  `status: superseded` and `superseded_by: <doc_id>`, as the two `engine-boundary` notes in the
  demo vault do.

The vocabularies are in [`contract/contract.json`](../contract/contract.json) under `spine`.
[`WRITING.md`](../vaults/demo-core-vault/00-operator/WRITING.md) describes how to write a note,
and `vaults/demo-vault/99-templates/` has templates to copy. To move an existing vault onto the
spine, read [`migration-vocabulary.md`](migration-vocabulary.md) first.

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
| `vaults.lock` | `compose`, `embed`, the refresh loop | serializes writers |

`refresh.json`, `agent-publish.toml` and `vaults.lock` sit beside whichever registry is in use, so
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
