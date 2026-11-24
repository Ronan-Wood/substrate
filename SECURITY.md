# Security

substrate indexes documents and notes and serves search over them. A vulnerability in it is
taken seriously and answered.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **[open a draft advisory](https://github.com/Ronan-Wood/substrate/security/advisories/new)**.
It is private to you and the maintainer until a fix ships. If you cannot use it, write to
scripta.app@outlook.com.

Please do not open a public issue for a vulnerability. Public issues are the right place for bugs,
crashes and everything else.

This is a one-person project. Expect a first reply within a week, and a fix timed to severity rather
than to a policy. You will be credited in the advisory unless you ask not to be.

## What is in scope

- Ingestion: the PDF, image, archive, HTML and Markdown readers, and the documents they are given.
- Composition and the index: the scope registry, vault manifests, and the files the engine writes.
- Retrieval, the CLI, and the MCP server over stdio, the Unix socket and loopback HTTP (`--http`),
  including what a client can read or change, and the HTTP transport's Host, Origin and
  content-type guards.
- The model transport: the loopback-only guard on every model request.
- The release tooling under `tools/`, including the bundle build and the interpreter pin.

Out of scope: vulnerabilities in third-party dependencies with no substrate-specific exposure, which
belong upstream; and findings that require an attacker who already has code execution as your user.

## The conversion worker

Docling converts documents in a separate worker process, never in the engine's own. The boundary
protects the engine from a converter that hangs, crashes, floods its output or answers garbage: the
engine kills the worker's process group, retries once, then quarantines the file. It is not a
sandbox. The worker runs as you, with your files in reach, so a document that gets code to run
inside docling runs it with your privileges. That belongs upstream with docling; what
substrate does with the worker's answers is in scope here.

## Supported versions

The latest release. Fixes go into the next release rather than to older versions.
