# The demo vaults

`vaults/demo-vault` and `vaults/demo-core-vault` are a small synthetic pair that exercises every
tier and status of the vault structure. They are also the engine's regression fixtures and the
source of the golden frames in `contract/frames/`, so everything here is safe to publish and
stable to test against.

Run the README's [quickstart](../README.md#quickstart-the-demo-vaults-in-30-seconds) first. Every
command below runs from the root of the clone, against the `demo` scope it composed into `.demo/`.

## What is in them

`demo-vault`'s manifest inherits `demo-core-vault`, so scope `demo` composes 10 notes across both.

| note | vault, tier | spine | what it shows |
|---|---|---|---|
| `00-operator/working-cadence.md` | core, 1 | active · explanation | a tier-1 operator note reached from the project scope |
| `10-reference/frozen/…/passages/00-partitioning.md` | core, 2 | active · reference | a frozen reference, with source metadata in its `_meta.md` |
| `10-reference/versioned/…/go/1.21/passages/00-goroutines.md` | core, 2 | active · reference | a versioned reference, made current by `reference_pins` |
| `02-areas/manifest-inheritance.md` | project, 3 | **complete** · explanation | complete is still retrieved, unlike archived |
| `02-areas/retrieval-tuning.md` | project, 3 | active · explanation | an ordinary project note |
| `03-references/local-glossary.md` | project, 3 | active · reference | a project-local reference |
| `04-synthesis/engine-boundary-current.md` | project, 3 | active · decision | declares `supersedes: engine-boundary-old` |
| `04-synthesis/engine-boundary-old.md` | project, 3 | **superseded** · decision | out of default results |
| `_archive/phase0-spike-notes.md` | project, 3 | **archived** · explanation | out of default results |
| `_sources/engine-boundary-call.md` | project, 3 | active · reference, `class: conversation` | a call transcript, withheld by default |

`MEMORY.md`, `log.md`, `structure.md`, `WRITING.md` and the `99-templates/` folders are navigation
and templates. They aren't indexed as notes.

## Things to try

**Inheritance.** A question only the core vault can answer is answered from the project scope:

```sh
uv run python -m substrate.cli query "audit review implement verify" --scope demo --registry .demo/scopes.toml --no-vector
```

The hits carry `@demo-core-vault`.

**Supersession.** The current decision comes back, and the one it replaced doesn't:

```sh
uv run python -m substrate.cli query "engine boundary" --scope demo --registry .demo/scopes.toml --no-vector
uv run python -m substrate.cli query "engine boundary" --scope demo --registry .demo/scopes.toml --no-vector --all-status
```

The first returns `engine-boundary-current` and not `engine-boundary-old`, with
`supersedes=engine-boundary-old` on its spine line. The second adds the superseded note, marked `status=superseded`.

**Archived notes.** These are excluded by default and returned on request:

```sh
uv run python -m substrate.cli query "phase 0 spike" --scope demo --registry .demo/scopes.toml --no-vector
uv run python -m substrate.cli query "phase 0 spike" --scope demo --registry .demo/scopes.toml --no-vector --include-archived
```

The first returns nothing, and its header says the status filter was `active,complete`. An empty
result is not an empty corpus, and the filters line says why.

**Withheld calls.** The transcript appears only when sources are included:

```sh
uv run python -m substrate.cli query "boundary call" --scope demo --registry .demo/scopes.toml --no-vector --include-sources
```

**Versioned references.** `go = "1.21"` in the manifest makes the 1.21 spec current, and its
passages show `v1.21` in their path:

```sh
uv run python -m substrate.cli query "go statements" --scope demo --registry .demo/scopes.toml --no-vector
```

**What a scope holds.** `status` reports counts by vault, tier, status, `doc_type` and confidence,
whether the vectors are complete, and whether the vault has changed since the index was built:

```sh
uv run python -m substrate.cli status --scope demo --registry .demo/scopes.toml
```

With no embedder run, it reports the vectors as `INCOMPLETE`. That is the engine saying the vector
arm can't contribute, instead of quietly searching lexically.

**The structured reply.** `--json` prints the same envelope the MCP server returns. Beside the
passages it carries `retrieval_mode` (which arms ran, and the engine's health), `filters` (what was
left out), `index_version` and `refresh`:

```sh
uv run python -m substrate.cli query "engine boundary" --scope demo --registry .demo/scopes.toml --no-vector --json
```

Over MCP, `search` returns the same passages, each with an `expand_ref` such as
`demo/engine-boundary-current#c00000`. `expand` reads the rest of the note behind it.
