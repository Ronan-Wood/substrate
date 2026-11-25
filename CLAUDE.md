# substrate — project guidance

How to set up, check a change and land it is in [`CONTRIBUTING.md`](CONTRIBUTING.md); this file does
not restate it. What follows is what a session editing this repository also needs.

## Writing standard (load-bearing)

**Every note written into a vault, and every doc/finding written in this repo, follows
[`vaults/demo-core-vault/00-operator/WRITING.md`](vaults/demo-core-vault/00-operator/WRITING.md).**
Read it before authoring or editing vault notes. It is the authority on *how* content is written —
it makes the vault and the RAG over it consistent, predictable, and readable.

The vault content is the primary target of the standard, not just Claude Code sessions: notes are
mostly model-written, so the rules apply when a note is written.

**The vaults here are examples.** The repo carries only the pair `vaults/demo-core-vault` (root)
and `vaults/demo-vault` (project): synthetic content that doubles as the engine's regression
fixture. The engine has an opinion on a vault's shape and none on where it lives; point `compose`
at a path. Fixtures, examples and docs use these vaults or generated data, as `CONTRIBUTING.md`
rule 4 says.

Non-negotiables from that file:
- **Lead with the conclusion; no preamble/postamble.**
- **Structure over prose** for anything enumerable (decisions, tradeoffs, options → tables/lists).
- **Preserve decision history and confidence markers; attach conditions to every number.**
- **One note, one job** — declare a `doc_type` (the vocabulary is `spine.DOC_TYPES`: decision /
  explanation / reference / how-to / digest); if a note does two jobs, split it.
- **Glossary:** one word per concept (its table is in `WRITING.md`). **Reserved:** `status` = the
  note lifecycle only (active / complete / archived / superseded); `confidence` = how settled a
  claim is; the retrieval-stack state is `capability`.

## Refresh belongs to the engine (`substrate serve`)

- `substrate serve` is the shared socket server plus a refresh loop (`substrate/refresh.py`). It
  runs a pass at start and every `--every` seconds after, with or without a host app
  (`--no-refresh` turns it off). `substrate refresh` runs one pass now. `serve` takes its models
  and period from `~/.substrate/serve.toml` (`hyde_model`, `rerank_model`, `every`) where no flag
  sets them, read once at start (`substrate/serve_config.py`). A person edits that file by hand; no
  client writes it. It has no embedder key, by design: the embedder decides what every index holds, so
  a serve-only embedder would match no vectors.
- `compose` and `embed` take `vaults.lock` (an `flock` beside the registry), whoever runs them. Run
  by hand they wait up to 60 s (`--wait`, `--no-wait`), then exit 75 having written nothing. A
  refresh pass takes no lock of its own: it runs each scope's compose with `--no-wait`, which
  declines when another holds the lock (the loop comes round again), and its embed with a 30-minute
  wait. `tools/substrate-deploy` takes the default registry's lock itself and waits 120 s unless
  `SUBSTRATE_DEPLOY_LOCK_WAIT` says otherwise.

### The deployment pin

`tools/substrate-deploy` publishes an export of one commit to `~/.substrate/engine`, the engine a
DEBUG build of Scripta runs. `tools/deployment.py` emits the manifest and signature that
deploy records, and verifies the export file by file.

**Editing this repo does not change what that engine serves. Neither does committing.** Only
`tools/substrate-deploy` does, and a pin sitting behind HEAD is the normal state of a repo being
worked in, not a fault. `tools/substrate-deploy --show` says how far behind it is and whether it
still verifies.

Do not "simplify" this into running the working tree directly: code being edited should not run
against real data. `tools/deployment.py` carries the argument for the pin over the two cheaper
guards (refuse-if-dirty, refuse-if-uncommitted): the first would switch the refresh off on any day
the tree is edited, and the second lets a `wip` commit through.

## Discipline

`audit → review → implement → verify`. `/crosscheck` right after implementing (auto-applies what
clears its bar), `/adversary` as the last gate before presenting (report-only), where your session
has those skills. Refuse rather than mislead — the engine hard-fails on an
incomplete/silently-narrowed state rather than returning a plausible-but-wrong result. See
`docs/principles.md`, the dated records in `docs/history/`, and `docs/specification.md` for where
the structural spec lives.
