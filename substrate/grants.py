"""Which MCP clients may write through the shared engine: the grant store and the one decision.

The shared engine (`substrate serve --writes=granted`) runs `ingest` and `amend` for a client only
once the user has granted that client by name. Every client reaches the engine through the
`--connect` bridge, which sends its name (`Substrate-Client`) and, when it has one, the token the
grant issued (`Substrate-Grant`). The engine re-reads the store on every write, so a grant or a
revoke from the CLI applies to a running session at its next call, with no restart.

**What a grant does.** An MCP client writes only after the user said yes to that client name; every
write it makes is stamped with the name (`written_by`, `amended_by`); a revoke stops a running
session at its next write; and a client that tried without one is listed for the user to allow.

**What it does not do.** Any process running as this user can read a token file, claim a client
name (`clientInfo.name` is self-declared), or write vault files directly if it can reach them. This
is consent and accounting for well-behaved clients, the same honesty as the socket's uid check
(`local_socket`). It is not a wall against hostile code running as the user.

Files, all under the engine's own home (`scopes.engine_home()`), all 0600:

  * `grants.toml`: one `[grants."<client>"]` table per granted client: `token_sha256`,
    `generation`, `scopes` (`["*"]` for every scope) and `issued_at`. Only the token's hash.
  * `clients/<client>.token` (directory 0700): the token itself, which the bridge reads.
  * `grant-requests.jsonl`: one line per refused write from an ungranted client, at most one per
    client per hour. It is what a host shows the user to allow.

Stdlib only: the `--connect` bridge imports this to find its token.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from substrate import scopes

# What a shared engine does with a write. `none`: refuses every one (the read-only socket). `granted`:
# runs it for a client holding a live grant for that scope. `open`: runs it for anyone who can reach
# the socket (`--no-read-only`). Stdio is `open`: reaching it means spawning the process.
WRITES_MODES = ("none", "granted", "open")

CLIENT_HEADER = "Substrate-Client"
GRANT_HEADER = "Substrate-Grant"
# The client's own `clientInfo` (name and version), compact JSON, so a request names what asked.
CLIENT_INFO_HEADER = "Substrate-Client-Info"

# Always a safe filename, and never a TOML key that nests: the table key is quoted anyway.
CLIENT_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

ALL_SCOPES = "*"
GRANTS_FILE = "grants.toml"
CLIENTS_DIR = "clients"
REQUESTS_FILE = "grant-requests.jsonl"

# A client asking again within this long is not recorded again.
REQUEST_INTERVAL_SECONDS = 3600.0
# Past this size the requests file is rewritten keeping each client's latest request.
REQUESTS_CAP_BYTES = 256 * 1024
# What a header may carry. A token is 64 hex characters; the rest are bounds on untrusted input.
TOKEN_BYTES = 32
MAX_HEADER_CHARS = 512


class GrantError(RuntimeError):
    """The grant store cannot be read or written, or a name or scope list is not usable."""


@dataclass(frozen=True)
class Caller:
    """Who sent a request on the socket, as the request says. Nothing here is verified yet:
    `decide` verifies the token against the store."""

    client: str | None = None
    token: str | None = None
    info: dict | None = None


@dataclass(frozen=True)
class Grant:
    client: str
    token_sha256: str
    generation: int
    scopes: tuple[str, ...]
    issued_at: str


@dataclass(frozen=True)
class Decision:
    """Whether one write may run. `client`, `generation` and `token_sha256` are set only for a
    VERIFIED grant: the client stamps the note, and the token binds the plan. `record` says whether
    the refusal is one a host should show the user as a request to allow."""

    allowed: bool
    client: str | None = None
    generation: int | None = None
    reason: str | None = None
    record: bool = False
    token_sha256: str | None = None

    @property
    def binding(self) -> str:
        """What a plan is bound to beyond its bytes: this client's grant, by its TOKEN. Not by its
        generation: a revoke deletes the grant, so a re-grant starts again at 1, and a binding by
        generation would bring back every plan made before the revoke. A token is never issued
        twice. Empty for a write no grant authorised (stdio, an open socket), which binds as it
        always has."""
        return "" if self.client is None else f"{self.client}\0{self.token_sha256}"


OWNER = Decision(allowed=True)


# --------------------------------------------------------------------------- the decision

def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _how_to_grant(client: str) -> str:
    return (f"Ask the user to allow it in the app that hosts the engine, under its clients, or to "
            f"run `substrate grant {client}`, then call again.")


def decide(mode: str, caller: Caller | None, scope: object,
           grants: dict[str, Grant] | None) -> Decision:
    """The one write decision, a pure function of the engine's mode, the caller, the scope written
    to, and the grant store as read (None when it could not be read).

    `caller` None is the OWNER: a stdio server's own client, which spawned the process. It is
    allowed exactly where the mode allows anyone (`open`), never under `granted`, which only a
    socket serves.
    """
    if mode == "open":
        return OWNER
    if mode != "granted":
        return Decision(False, reason=(
            "this server is read-only: it runs no write for any client (Doc 3 §3). A shared "
            "engine started with `--writes=granted` runs writes for a client the user has "
            "granted, and a stdio `substrate-mcp` for the client that spawned it."))
    client = caller.client if caller is not None else None
    if not client:
        return Decision(False, reason=(
            "this engine runs writes only for a client the user has granted, and this request "
            "named no client. Start the bridge with `substrate-mcp --connect --client NAME`."))
    if not CLIENT_NAME.fullmatch(client):
        return Decision(False, reason=(
            f"this request named the client {client[:64]!r}, which is not a client name "
            f"(lowercase letters, digits, `.`, `_` and `-`, at most 64)."))
    if grants is None:
        return Decision(False, reason=(
            f"the grant store ({GRANTS_FILE} in the engine's home) cannot be read, so no write "
            f"is run. Fix or remove it, then grant `{client}` again."))
    grant = grants.get(client)
    if grant is None:
        return Decision(False, record=True, reason=(
            f"client `{client}` has no grant to write notes. " + _how_to_grant(client)))
    token = caller.token if caller is not None else None
    if not token or not hmac.compare_digest(token_sha256(token), grant.token_sha256):
        return Decision(False, record=True, reason=(
            f"client `{client}` did not present its current grant token: its grant was rotated, "
            f"or its token file (`~/.substrate/{CLIENTS_DIR}/{client}.token`) is missing or "
            f"stale. " + _how_to_grant(client)))
    if ALL_SCOPES not in grant.scopes and scope not in grant.scopes:
        return Decision(False, record=True, reason=(
            f"client `{client}` is granted only {', '.join(sorted(grant.scopes))}, not scope "
            f"{scope!r}. Run `substrate grant {client} --scopes …` to widen it."))
    return Decision(True, client=client, generation=grant.generation,
                    token_sha256=grant.token_sha256)


def may_write(mode: str, caller: Caller | None, scope: object) -> Decision:
    """`decide` over the store as it is NOW: read on every write call, never cached, so a revoke
    reaches a running session at its next call. A refusal a host should show the user is recorded
    as a request. Every write tool is gated alike."""
    grants: dict[str, Grant] | None = {}
    if mode == "granted":
        try:
            grants = load()
        except GrantError:
            grants = None
    decision = decide(mode, caller, scope, grants)
    if decision.record and caller is not None and caller.client:
        with contextlib.suppress(OSError, GrantError):
            record_request(caller)
    return decision


# --------------------------------------------------------------------------- the files

def home() -> Path:
    return scopes.engine_home()


def grants_path() -> Path:
    return home() / GRANTS_FILE


def requests_path() -> Path:
    return home() / REQUESTS_FILE


def token_path(client: str) -> Path:
    return home() / CLIENTS_DIR / f"{_valid(client)}.token"


def _valid(client: str) -> str:
    if not isinstance(client, str) or not CLIENT_NAME.fullmatch(client):
        raise GrantError(f"{client!r} is not a client name: lowercase letters, digits, `.`, `_` "
                         f"and `-`, starting with a letter or digit, at most 64 characters.")
    return client


def client_name(raw: object) -> str | None:
    """A client's self-declared `clientInfo.name` as a grant name: lowercased, with every run of
    other characters made one `-`, or None when nothing usable is left. "Claude Code" becomes
    `claude-code`."""
    if not isinstance(raw, str):
        return None
    name = re.sub(r"[^a-z0-9._-]+", "-", raw.strip().lower()).strip("-._")[:64]
    return name if CLIENT_NAME.fullmatch(name) else None


@contextlib.contextmanager
def _locked():
    """The store's read-modify-write lock, beside it, through the registry's one lock helper."""
    directory = home()
    directory.mkdir(parents=True, exist_ok=True)
    with scopes.flocked(directory / f".{GRANTS_FILE}.lock"):
        yield


def _read_regular(path: Path, limit: int) -> bytes | None:
    """The bytes of a regular file this user owns, or None when there is none. Not followed
    through a symlink; refused past `limit` bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise GrantError(f"cannot open {path}: {e}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            raise GrantError(f"{path} is not a regular file owned by this user")
        if st.st_size > limit:
            raise GrantError(f"{path} is over {limit} bytes")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return fh.read(limit + 1)
    finally:
        if fd != -1:
            os.close(fd)


def load() -> dict[str, Grant]:
    """Every grant in the store. An absent store is no grants; one that cannot be read is a
    GrantError, which the decision treats as no write for anyone."""
    path = grants_path()
    raw = _read_regular(path, 1024 * 1024)
    if raw is None:
        return {}
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise GrantError(f"{path} is not readable TOML: {e}") from e
    table = data.get("grants", {})
    if not isinstance(table, dict):
        raise GrantError(f"{path}: `grants` must be a table")
    out: dict[str, Grant] = {}
    for client, row in table.items():
        try:
            _valid(client)
            sha, gen, scope_list, issued = (row["token_sha256"], row["generation"],
                                            row["scopes"], row["issued_at"])
        except (GrantError, KeyError, TypeError) as e:
            raise GrantError(f"{path}: the grant for {client!r} is malformed ({e})") from e
        if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
                or isinstance(gen, bool) or not isinstance(gen, int) or gen < 1
                or not isinstance(scope_list, list) or not scope_list
                or not all(isinstance(s, str) and s for s in scope_list)
                or not isinstance(issued, str)):
            raise GrantError(f"{path}: the grant for {client!r} is malformed")
        out[client] = Grant(client, sha, gen, tuple(scope_list), issued)
    return out


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _render(grants: dict[str, Grant]) -> bytes:
    q = scopes._quote
    lines = ["# Written by `substrate grant` and `substrate revoke`. Only each token's hash is kept.",
             "", "version = 1"]
    for g in sorted(grants.values(), key=lambda g: g.client):
        lines += ["", f"[grants.{q(g.client)}]",
                  f"token_sha256 = {q(g.token_sha256)}",
                  f"generation = {g.generation}",
                  f"scopes = [{', '.join(q(s) for s in g.scopes)}]",
                  f"issued_at = {q(g.issued_at)}"]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _clients_dir() -> Path:
    d = home() / CLIENTS_DIR
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise GrantError(f"{d} is not a directory owned by this user")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(d, 0o700)
    return d


def issue(client: str, scope_list: list[str] | None = None) -> tuple[Grant, Path]:
    """Grant `client`, or rotate its grant if it has one: a new token, the generation one higher.
    A rotation kills the plans its old token made (they are bound to the token) and keeps the
    grant's scopes unless `scope_list` names new ones. A new grant without one is for every scope.
    Returns the grant and where its token was written."""
    _valid(client)
    asked = None if scope_list is None else tuple(dict.fromkeys(scope_list))
    if asked is not None:
        # An empty list is refused, never read as "no limit": a host that sends what the user left
        # unselected must not hand out every scope.
        if not any(isinstance(s, str) and s.strip() for s in asked):
            raise GrantError("--scopes names no scope. Name the scopes, or `*` for every one; leave "
                             "it out to keep the scopes a grant already has.")
        if not all(isinstance(s, str) and s.strip() == s and s for s in asked):
            raise GrantError(f"{list(asked)!r} is not a list of scope names")
        if ALL_SCOPES in asked and len(asked) > 1:
            raise GrantError(f"`{ALL_SCOPES}` already means every scope; name it alone")
    with _locked():
        current = load()
        token = secrets.token_hex(TOKEN_BYTES)
        previous = current.get(client)
        # A rotation keeps the scopes the grant has: only an explicit list changes them, so
        # granting again (or a host's Allow on a granted name) never widens a narrowed grant.
        chosen = (asked if asked is not None else previous.scopes if previous is not None
                  else (ALL_SCOPES,))
        grant = Grant(client, token_sha256(token), (previous.generation + 1) if previous else 1,
                      chosen, datetime.now(UTC).isoformat(timespec="seconds"))
        # The token first. If the store write then fails, the old grant still names the old hash,
        # so the new token is refused: every failure here leaves the client refused, never open.
        path = _clients_dir() / f"{client}.token"
        _atomic_write(path, (token + "\n").encode("ascii"))
        current[client] = grant
        _atomic_write(grants_path(), _render(current))
        _drop_requests(client)
    return grant, path


def revoke(client: str) -> bool:
    """Remove `client`'s grant and token, and forget that it asked. True if it had a grant. Its
    next write is refused and recorded as a request again."""
    _valid(client)
    with _locked():
        current = load()
        had = current.pop(client, None) is not None
        if had:
            _atomic_write(grants_path(), _render(current))
        with contextlib.suppress(FileNotFoundError):
            os.unlink(home() / CLIENTS_DIR / f"{client}.token")
        _drop_requests(client)
    return had


def read_token(client: str) -> str | None:
    """The token in `client`'s token file, or None when there is none or it cannot be read. Read on
    every write by the bridge, so a rotation reaches a running session."""
    try:
        raw = _read_regular(token_path(client), 4096)
    except GrantError:
        return None
    if raw is None:
        return None
    token = raw.decode("ascii", "replace").strip()
    return token if re.fullmatch(r"[0-9a-f]{64}", token) else None


# --------------------------------------------------------------------------- requests

def _requests() -> list[dict]:
    try:
        raw = _read_regular(requests_path(), 4 * REQUESTS_CAP_BYTES)
    except GrantError:
        return []
    out = []
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and isinstance(row.get("client"), str)
                and CLIENT_NAME.fullmatch(row["client"]) and isinstance(row.get("at"), str)):
            out.append(row)
    return out


def _write_requests(rows: list[dict]) -> None:
    path = requests_path()
    if not rows:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        return
    _atomic_write(path, "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in rows).encode())


def _drop_requests(client: str) -> None:
    rows = _requests()
    kept = [r for r in rows if r["client"] != client]
    if len(kept) != len(rows):
        _write_requests(kept)


def _info(info: object) -> dict | None:
    """The part of a self-declared `clientInfo` worth showing: its name and version, as short
    strings."""
    if not isinstance(info, dict):
        return None
    out = {k: info[k][:64] for k in ("name", "version") if isinstance(info.get(k), str)}
    return out or None


def record_request(caller: Caller, *, now: float | None = None) -> bool:
    """Append a request for `caller.client`, unless one was recorded within the interval. True if
    recorded."""
    client = _valid(caller.client or "")
    now = time.time() if now is None else now
    with _locked():
        rows = _requests()
        for r in rows:
            if r["client"] != client:
                continue
            try:
                then = datetime.fromisoformat(r["at"]).timestamp()
            except ValueError:
                continue
            if now - then < REQUEST_INTERVAL_SECONDS:
                return False
        row = {"client": client, "client_info": _info(caller.info),
               "at": datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")}
        path = requests_path()
        try:
            size = os.lstat(path).st_size
        except FileNotFoundError:
            size = 0
        if size > REQUESTS_CAP_BYTES:
            latest = {r["client"]: r for r in rows}
            latest[client] = row
            _write_requests(list(latest.values()))
            return True
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=True) + "\n")
    return True


def listing() -> dict:
    """`substrate grants --json`: the granted clients, and the ones that asked and are not granted,
    each named once with its latest request."""
    granted = load()
    latest: dict[str, dict] = {}
    for r in _requests():
        if r["client"] not in granted:
            latest[r["client"]] = {"client": r["client"], "client_info": _info(r.get("client_info")),
                                   "at": r["at"]}
    return {
        "granted": [{"client": g.client, "generation": g.generation, "scopes": list(g.scopes),
                     "issued_at": g.issued_at} for g in sorted(granted.values(),
                                                              key=lambda g: g.client)],
        "requested": sorted(latest.values(), key=lambda r: r["client"]),
    }


def caller_from_headers(get_all) -> Caller:
    """The caller a socket request names, from its headers (`get_all(name)` → list or None). A
    header stated more than once, or too long to be real, is treated as absent."""

    def one(name: str) -> str | None:
        values = get_all(name) or []
        if len(values) != 1 or len(values[0]) > MAX_HEADER_CHARS:
            return None
        return values[0].strip(" \t") or None

    info = None
    raw = one(CLIENT_INFO_HEADER)
    if raw:
        with contextlib.suppress(ValueError):
            info = _info(json.loads(raw))
    return Caller(client=one(CLIENT_HEADER), token=one(GRANT_HEADER), info=info)
