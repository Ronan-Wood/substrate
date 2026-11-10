# Where the specification lives

> **Doc 2 and Doc 3a are not published yet.** They sit in the maintainer's own vaults, so a clone
> of this repository does not contain them. Until they are published, the public references are
> [`configuration.md`](configuration.md) for the manifest and the files the engine reads, the
> spine vocabularies in [`contract/contract.json`](../contract/contract.json),
> [`WRITING.md`](../vaults/demo-core-vault/00-operator/WRITING.md) for how notes are written, and
> the demo vaults, which exercise every tier and status and three of the five `doc_type`s.

The code cites these documents by section, as in "Doc 2 §6a". This is what each one governs:

| doc | governs |
|---|---|
| **Doc 1** | the substrate model the engine serves |
| **Doc 2** | the vault structure: tiers, the `.substrate.toml` manifest, the spine fields (`status`, `doc_type`, `confidence`, `domains`), inheritance and supersession |
| **Doc 3a** | the MCP server surface |

**This repo implements Doc 2; it does not define it.** The engine is one consumer of the vault
structure. Scripta is another, an editor plugin would be a third, and none of them owns the spec,
which is why it does not live in any one of them. Doc 3a describes one product's transport and
changes with that product, so it lives with that project rather than beside Docs 1 and 2.

This page is a pointer, never a copy or a summary: a pointer cannot drift out of agreement with
its target, and a summary would.

`docs/principles.md` and the records in `docs/history/` are about how the engine is built and how it
fails. They are engine documents and stay here.
