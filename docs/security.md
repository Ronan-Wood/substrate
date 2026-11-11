# Security model

substrate indexes your notes and answers questions over them, so the question that matters is who
can read what, and where any of it can go. In short:

- the engine opens network connections only to loopback;
- over stdio and the Unix socket, only your user can reach it;
- the shared transports can't write unless you say so, and then only for a client you granted.

To report a vulnerability, follow [`SECURITY.md`](../SECURITY.md). This page describes the design.
It isn't a disclosure channel.

## The trust boundary

| party | trusted with |
|---|---|
| **you**, the user who runs the engine | everything: your vaults, the registry, the index |
| **a process running as you** | the same. A same-user attacker is out of scope (`SECURITY.md`) |
| **an MCP client you launch over stdio** | the tools that server exposes, write tools included unless `--read-only` |
| **an MCP client on the shared engine** | the read tools; the write tools only once you grant it by name (`substrate grant`) |
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
| **Unix socket** | `substrate serve`, or `substrate-mcp --socket` | your uid only | refused unless `--writes=granted` (a granted client) or `--no-read-only` (any caller) |
| **loopback HTTP** | `substrate-mcp --http` (`127.0.0.1:8765`) | any local process, run by any user | refused unless `--no-read-only` |
| **stdio bridge** | `substrate-mcp --connect` | the client that launched it | forwarded to the socket and decided there; never run in the bridge. `--read-only` neither lists nor sends them |

The write tools are `ingest` and `amend`. `ingest` adds a note from the text it is given, and
`amend` overwrites an existing note. `ingest` reads no file, and `amend` reads only the note at
`path`, inside the project vault's writable folders. Both refuse `source_path`, and the
`--connect` bridge refuses it for `ingest` before forwarding, because a model's arguments can be
steered by content it just read. A read-only server leaves both out of its tool list. A client
that can read a file itself can still pass that file's text as a note, so give write access only
to a client whose own file access you trust that far. `substrate ingest <file>` on the command
line still takes a path, since there you typed it.

## Grants: which clients write through the shared engine

An engine started with `--writes=granted` (`substrate serve --writes=granted`) runs a write only
for a client you granted by name ([`substrate/grants.py`](../substrate/grants.py)):

- `substrate grant <client> [--scopes a,b]` issues a token to `~/.substrate/clients/<client>.token`
  (`0600`, in a `0700` folder) and records only its hash in `~/.substrate/grants.toml`. Run it
  again to rotate the token. `substrate revoke <client>` removes both, and `substrate grants`
  lists who may write and who asked.
- The `--connect` bridge sends its client name (`--client`, else the client's own
  `clientInfo.name`) and the token, read on every write. The engine checks them on every write, so
  a revoke stops a running session at its next one. A refused write is recorded in
  `~/.substrate/grant-requests.jsonl`, at most once a client an hour, for a host to show you.
- The engine stamps each granted write in the note's frontmatter: `written_by: <client>` on a new
  note, `amended_by: <client>` on an edit. A note that claims a different value is refused. A plan
  is bound to the client's grant token, so another client can't redeem it, and neither can this
  one after a rotation, or after a revoke and a new grant.
- **The stamp is a label, not proof.** Anyone who can edit the file can add, change or remove
  `written_by` and `amended_by`: you in Obsidian, any process running as you, and a granted client
  that finds a spelling some reader takes for a stamp key and the engine's check misses. Read it
  as who the engine last wrote the note for, not as who wrote what. An engine write log, outside
  the vault, is planned as the record.
- Every other rule still applies after the grant: the vault guard, the project vault only, the
  writable folders, and `amend`'s refusals. Each write is still a plan, then a commit.

**What a grant does:** a client writes only after you said yes to that client name, every write it
makes is labelled with that name, and a revoke stops it at its next write.

**What it doesn't do:** any process running as you can read a token file, claim a client's name
(`clientInfo.name` is whatever the client says it is), or write vault files directly if it can
reach them. A grant is consent and accounting for well-behaved clients, the same honesty as the
socket's uid check. It is not a wall against hostile code running as your user, which
[`SECURITY.md`](../SECURITY.md) puts out of scope.

**The Unix socket** is the default shared transport, because loopback TCP authenticates nobody
([`substrate/local_socket.py`](../substrate/local_socket.py)):

- The socket sits in a `0700` directory, so another uid can neither connect to it nor create the
  path first to squat on it.
- The client reads the server's uid off the connected socket and refuses a listener that isn't your
  user, before it writes a byte.
- The server reads each caller's uid the same way and drops any caller that isn't your user.

No token proves the engine. A secret sent upstream would go to whoever is listening, and a
squatter is exactly who that would be, so the bridge sends a client's grant token only after the
listener's uid has passed.

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
