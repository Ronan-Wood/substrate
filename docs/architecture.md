# Architecture

substrate turns a set of Markdown vaults into one searchable **scope** and answers queries over
it. A vault is the source of truth. The index is a disposable cache that the engine can rebuild
from the vault alone.

```
vault(s) ──compose──▶ ingest tree ──reconcile──▶ index (SQLite) ──retrieve──▶ CLI · MCP server
  .substrate.toml        per-note dirs             FTS5 + vectors      lexical, vector,
  + inherited vaults                                                   HyDE, rerank
```

## The pipeline

| stage | what happens | where |
|---|---|---|
| **Compose** | Reads the project vault's `.substrate.toml`, resolves `inherits`, and collects every note across the vaults. A missing inherited vault is a hard failure, never a silently narrower scope. | `substrate/vault.py` |
| **Ingest** | Each note becomes an output directory holding its Markdown, its chunks and its spine. Markdown is read with the standard library. Other formats go through `extract/` (docling, the PDF text layer, Apple's on-device recognizer). | `substrate/markdown/`, `substrate/extract/` |
| **Chunk** | Units are packed into passages, with a policy per document class, and section-level outline records are built alongside them. | `substrate/chunk/`, `substrate/classes.py` |
| **Check** | Per-note assertions (content coverage, spine validity) plus the scope-level ones that `compose` prints: A-compose and A20 to A23. A failure is classified rather than skipped. | `substrate/checks.py`, `substrate/spine.py` |
| **Index** | `reconcile` brings the SQLite index into step with the ingest tree: FTS5 with section-weighted bm25, plus a `chunk_vectors` table. The schema is versioned, and a version change means a drop and rebuild. | `substrate/store/` |
| **Register** | A composed scope is recorded in the registry (`scopes.toml`): its name, its vault and its index. | `substrate/scopes.py` |
| **Embed** (optional) | Vectors from a local embedder through Ollama, kept in a content-addressed cache that survives rebuilds. | `substrate/embed/` |
| **Retrieve** | Lexical and vector arms fused with RRF, with optional HyDE query expansion and reranking. The default status filter is active and complete, and conversations are withheld unless asked for. | `substrate/retrieve/`, `substrate/stack.py` |
| **Render** | One structured result envelope, rendered for the terminal or as JSON for MCP. It carries the passages and their spines, plus the arms that ran (`retrieval_mode`), the filters applied, and refresh state. | `substrate/render.py`, `substrate/introspect.py` |
| **Serve** | The MCP server over stdio, a Unix socket or loopback HTTP. `substrate serve` adds a refresh loop that recomposes and re-embeds every registered scope on a timer. | `substrate/mcp/`, `substrate/refresh.py` |

## Two ideas that shape the code

**Refuse rather than mislead.** A result that looks complete and is not is the failure this
project is built against. When the vector arm has no vectors, when a model is unavailable, or when
the index has drifted from the vault, the result says so in a field. The engine doesn't quietly
fall back. [`principles.md`](principles.md) records the incidents behind this rule, which it names
the Boundary Principle.

**Measure before shipping.** Every retrieval choice (chunk size, embedder, HyDE, reranker) was
measured against a fixed set of gold cases, and the rejected experiments are kept beside the
accepted ones. [`EXPERIMENTS.md`](../EXPERIMENTS.md) is that log, and its *Current best
configuration* section is what the engine runs.

## Module map

| area | modules |
|---|---|
| vaults and notes | `vault.py` (manifests, composition), `spine.py`, `classes.py`, `frontmatter.py`, `notes.py` (plan-then-write note creation), `digests.py`, `identity.py` |
| ingestion | `markdown/`, `extract/`, `chunk/`, `models.py` (the canonical document model) |
| index | `store/` (`index_store.py` is the facade, `schema.py`, `reconcile.py`, `fts.py`) |
| retrieval | `retrieve/` (`retriever.py`, `expand.py`, `rerank.py`, `rerank_cross.py`), `embed/`, `stack.py` |
| state and freshness | `scopes.py` (the registry), `refresh.py`, `refresh_state.py`, `freshness.py`, `prune.py` |
| transports | `mcp/server.py`, `mcp/bridge.py` (`--connect`), `local_socket.py`, `wire.py` |
| safety | `net.py` (loopback egress), `guard.py` (vault guard), `private.py` (file modes), `agent.py` (agent mode) |
| originals | `blobs.py`, `blob_lifecycle.py` (the content-addressed store behind derived notes) |
| entry point | `cli.py` |

## Where to read next

- [`client-contract.md`](client-contract.md): what a client can rely on.
- [`security.md`](security.md): who can reach what.
- [`ingestion-findings.md`](ingestion-findings.md): what PDF and Docling ingestion taught, measured
  on real documents.
- [`configuration.md`](configuration.md): the manifest, the note frontmatter, and the files the
  engine reads.
