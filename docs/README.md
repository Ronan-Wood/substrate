# Documentation

Start with the [project README](../README.md) for the install and the quickstart. In these pages,
`substrate <command>` means `uv run python -m substrate.cli <command>`, and `substrate-mcp` means
`uv run python -m substrate.mcp.server`.

## Using the engine

| doc | what it covers |
|---|---|
| [`architecture.md`](architecture.md) | the pipeline from a vault to an answer, and a module map |
| [`configuration.md`](configuration.md) | `.substrate.toml`, the registry, `serve.toml`, `~/.substrate`, local models |
| [`security.md`](security.md) | who can reach what: loopback-only models, the Unix socket, read-only transports, the vault guard, agent mode |
| [`demo-vault.md`](demo-vault.md) | the demo vaults, note by note, and queries that show each behaviour |
| [`client-contract.md`](client-contract.md) | for client authors: `contract/contract.json`, the golden frames, the wire version |

## How the engine was built

| doc | what it covers |
|---|---|
| [`principles.md`](principles.md) | the Boundary Principle and its laws: the failure shape this project is built against |
| [`specification.md`](specification.md) | the specification documents the code cites (Doc 1, 2, 3a), and what is public today |
| [`../EXPERIMENTS.md`](../EXPERIMENTS.md) | every retrieval experiment, accepted or rejected, and the configuration that shipped |
| [`ingestion-findings.md`](ingestion-findings.md) | what PDF and Docling ingestion taught, measured on real documents |
| [`migration-vocabulary.md`](migration-vocabulary.md) | moving an existing Obsidian vault onto the spine: the vocabulary and the landmines |
| [`history/`](history/) | dated records: a handoff, an audit tracker and a spike readout. Superseded where they disagree with the code |

## Project process

| doc | what it covers |
|---|---|
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | setup, tests, and the rules a change follows |
| [`../RELEASING.md`](../RELEASING.md) | cutting a release, and the interpreter pin |
| [`../CHANGELOG.md`](../CHANGELOG.md) | what each release contains |
| [`../SECURITY.md`](../SECURITY.md) | reporting a vulnerability |
