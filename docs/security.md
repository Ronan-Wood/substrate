# Security model

substrate indexes your notes and answers questions over them, so the question that matters is who
can read what, and where any of it can go. In short:

- the engine opens network connections only to loopback;
- over stdio and the Unix socket, only your user can reach it;
- the shared transports can't write unless you say so.

To report a vulnerability, follow [`SECURITY.md`](../SECURITY.md). This page describes the design.
It isn't a disclosure channel.

## The trust boundary

| party | trusted with |
|---|---|
| **you**, the user who runs the engine | everything: your vaults, the registry, the index |
| **a process running as you** | the same. A same-user attacker is out of scope (`SECURITY.md`) |
| **an MCP client you launch over stdio** | the tools that server exposes, write tools included unless `--read-only` |
| **another local user** | nothing over stdio or the Unix socket. Over `--http`, the same reads your own clients get, including a guarded vault while its guard is satisfied. In agent mode, the snapshots you publish |
| **the network** | nothing. The engine connects only to loopback and listens on nothing beyond it |
| **a document being ingested** | nothing. PDFs, images, archives and HTML are untrusted input |

## Model requests stay on loopback

The embedder sends your corpus to the model, and HyDE sends your query, so every model request goes
through one guard in [`substrate/net.py`](../substrate/net.py):

- The host must be `127.0.0.1`, `localhost` or `::1`, as `urlsplit` parses it. A URL carrying any
  userinfo is refused, because userinfo is how a loopback-looking URL hides another host.
- Requests go through the engine's own opener, which ignores `HTTP_PROXY` and the system proxy
  settings and refuses redirects.
- The connected socket's peer must itself be a loopback address before any byte is written.

What the model server on loopback does with a request is up to that server, so use local models.

## Transports

| transport | how to start it | who can reach it | writes |
|---|---|---|---|
| **stdio** | `substrate-mcp` (`python -m substrate.mcp.server`) | the client that launched it | allowed unless `--read-only` |
| **Unix socket** | `substrate serve`, or `substrate-mcp --socket` | your uid only | refused unless `--no-read-only` |
| **loopback HTTP** | `substrate-mcp --http` (`127.0.0.1:8765`) | any local process, run by any user | refused unless `--no-read-only` |
| **stdio bridge** | `substrate-mcp --connect` | the client that launched it | allowed unless `--read-only`, and run in the bridge's own process |

The write tools are `ingest` and `amend`. `ingest` adds a note from the text it is given, or from
any file the server process can read (`source_path`), and `amend` overwrites an existing note. A
read-only server leaves both out of its tool list. A client allowed to write can therefore read any
of your files into a vault, so give write access only to a client you trust that far.

**The Unix socket** is the default shared transport, because loopback TCP authenticates nobody
([`substrate/local_socket.py`](../substrate/local_socket.py)):

- The socket sits in a `0700` directory, so another uid can neither connect to it nor create the
  path first to squat on it.
- The client reads the server's uid off the connected socket and refuses a listener that isn't your
  user, before it writes a byte.
- The server reads each caller's uid the same way and drops any caller that isn't your user.

There is no token. A secret sent upstream would go to whoever is listening, and a squatter is
exactly who that would be.

**Loopback HTTP** is reachable by every user on the machine, which the server says when it starts.
Prefer the socket. It refuses to bind anything but an IPv4 loopback address, and checks every
request:

- a `Host` other than `localhost` or `127.0.0.1`, with an optional port, gets `421`. No name is
  resolved;
- any `Origin` header gets `403`, since the server has no web page to allow;
- a `Content-Type` other than `application/json` gets `415`.

Together these close DNS rebinding and cross-origin form posts from a browser.

## What the engine writes is private

The engine's entry points run under umask `077`, and an index or ingest tree it already owns is
sealed to `0600` and `0700`. A reader that finds an index other users can read reports the exposure
in its reply, and the next compose seals it ([`substrate/private.py`](../substrate/private.py)).
The store of originals refuses a cloud-sync folder and any file carrying an extended ACL
([`substrate/blobs.py`](../substrate/blobs.py)).

The one exception is agent-mode snapshots, which are readable by their folder's group by design
(see below).

## The vault guard

A vault can hold content whose owner wants a narrower audience than you have, for example call
transcripts that an app shows only while it is running. Such a vault declares a guard at the
top level of its manifest, above any `[table]`:

```toml
guard_state = "/absolute/path/to/state.json"
```

The MCP server is designed to withhold that vault's own notes unless the state file shows a
heartbeat from the last 60 seconds naming the scope being asked for
([`substrate/guard.py`](../substrate/guard.py)). The guard is a privacy control and is opt-in, so
report anything that gets past it privately, as [`SECURITY.md`](../SECURITY.md) describes.

- **It is built to fail closed.** A missing, unreadable or malformed state file withholds the vault.
  So does a `guard_state` written inside a table, which `compose` also refuses.
- **It withholds a vault, not a scope.** The vaults the guarded one inherits stay readable.
- **Only what the MCP server returns is guarded.** The CLI, `query` included, and anything that
  opens the index read the guarded notes. `compose`, `embed` and the refresh loop are your own tools
  and keep the index current.

## Agent mode

`substrate-mcp --agent-mode --scopes <names> --snapshot-dir <dir>` serves chosen scopes to another
local user who can't read your home folder ([`substrate/agent.py`](../substrate/agent.py)).

- Your own engine publishes a snapshot of each scope listed in `agent-publish.toml`, after each
  successful refresh. A snapshot holds the full text of the scope's notes, with vault-relative
  paths, and with a guarded vault's notes removed rather than filtered.
- Snapshot files are mode `0640`, so **what another user can read is decided by which scopes you
  publish and by the snapshot folder's group**. Give each agent account its own folder.
- `--scopes` limits what that one server answers. Any other scope gets the same fixed "scope not
  available" reply, so the error can't be used to list what else exists.

## Calls are withheld by default

Conversations (`class: conversation`, such as call transcripts under `_sources/`) aren't
retrieved unless the caller passes `include_sources`. A passage from the middle of a transcript can
misrepresent what was finally decided. This rule is about accuracy rather than access, and each
reply's `filters` field says what it left out.
