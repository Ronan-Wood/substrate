"""The embedder never uses a model that runs in Ollama's cloud — the rule HyDE and the reranker keep.

Ollama proxies its cloud models through the loopback daemon, so the loopback guard passes and the
request still leaves the machine. The embedder sends the most of any arm: `substrate embed`, which
the refresh pass and the app run after every compose, sends the text of every note in the index,
and search sends each query. Its `available()` matched by family alone, so `--model
qwen3-embedding:8b-cloud` passed on any Mac that had pulled `qwen3-embedding:0.6b`, and a model
only the listing marks remote (`remote_host`) passed by its name.

These pin the refusal on every path that names an embedder: the arm itself (`available()`, and the
one place it sends), `stack.build` (what `substrate-mcp` and `substrate serve --embed-model`
build), and the two command lines that build one directly, `embed` and `eval`.

Run under pytest: `tests/conftest.py` keeps the vaults lock and `serve.toml` off the real
`~/.substrate`.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from substrate import cli, stack  # noqa: E402
from substrate.embed import engine  # noqa: E402
from substrate.embed.engine import EmbeddingRefused, OllamaEmbedder  # noqa: E402
from substrate.models import Chunk, Document  # noqa: E402
from substrate.retrieve import local_models  # noqa: E402
from substrate.store.index_store import IndexStore  # noqa: E402

_REMOTE = "https://ollama.com:443"
_LISTING = {"models": [
    {"name": engine.DEFAULT_MODEL},
    {"name": "qwen3-embedding:8b-cloud"},
    {"name": "embeddinggemma:cloud"},
    {"name": "big:latest", "remote_host": _REMOTE},
]}

@contextlib.contextmanager
def _daemon(body: object, asked: list | None = None):
    """Every module's `open_local` answering with `body` and recording what it was asked, so no
    daemon is contacted and the old code is held to the same fake. A request to `/api/embed` gets
    a vector per input, so a request that was sent succeeds rather than failing for another reason.
    """
    @contextlib.contextmanager
    def fake(req, *args, **kwargs):
        url = getattr(req, "full_url", req)
        if asked is not None:
            asked.append(url)
        if str(url).endswith("/api/embed"):
            n = len(json.loads(req.data)["input"])
            yield io.BytesIO(json.dumps({"embeddings": [[1.0, 0.0]] * n}).encode())
        else:
            yield io.BytesIO(json.dumps(body).encode())

    with contextlib.ExitStack() as patches:
        for mod in (engine, local_models):
            patches.enter_context(mock.patch.object(mod, "open_local", fake))
        yield


def _stderr_of(fn, argv: list[str]) -> tuple[int, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        rc = fn(argv)
    return rc, err.getvalue()


def _index(where: Path) -> str:
    db = str(where / "scope.db")
    with IndexStore(db) as s:
        text = "a note that must stay on this Mac"
        doc = Document(doc_id="d0", source_path="/d0.md", source_sha256="s" * 8, source_pages=1,
                       document_class="reference-frozen", title="d0", status="active")
        ch = Chunk(chunk_id="d0#c0", doc_id="d0", kind="passage", text=text, path=["Root", "d0"],
                   level=2, n_chars=len(text), document_class="reference-frozen")
        s.upsert(doc, [ch], markdown_path="/d0.md", markdown_mtime=0.0, markdown_sha256="m" * 8)
    return db


def _embed_requests(asked: list) -> list:
    return [u for u in asked if str(u).endswith("/api/embed")]


def test_the_embedder_refuses_a_cloud_model_by_name_and_by_listing() -> None:
    with _daemon(_LISTING):
        assert OllamaEmbedder(model=engine.DEFAULT_MODEL).available()
        for model in ("qwen3-embedding:8b-cloud", "embeddinggemma:cloud", "QWEN3-EMBEDDING:8B-CLOUD",
                      "big:latest", "big"):     # `big` is cloud by the listing's `remote_host` alone
            e = OllamaEmbedder(model=model)
            assert not e.available(), model
            assert e.refused_cloud, model


def test_a_cloud_tag_is_refused_without_asking_the_daemon() -> None:
    asked: list = []
    with _daemon(_LISTING, asked):
        assert not OllamaEmbedder(model="qwen3-embedding:8b-cloud").available()
    assert asked == []


def test_the_embedder_never_sends_to_a_cloud_tag() -> None:
    """The send point refuses too, so a caller that skips `available()` sends nothing."""
    asked: list = []
    with _daemon(_LISTING, asked):
        e = OllamaEmbedder(model="qwen3-embedding:8b-cloud")
        for send in (lambda: e.embed_documents(["a note"]), lambda: e.embed_query("a query")):
            try:
                send()
            except EmbeddingRefused as refused:
                assert "cloud model" in str(refused), refused
            else:
                raise AssertionError("a cloud embedder embedded")
    assert asked == []


def test_a_cloud_embedder_is_never_wired_into_a_stack() -> None:
    """What `substrate-mcp --embed-model` and `substrate serve --embed-model` build: the arm is not
    wired, and is reported as a cloud refusal, not as a daemon that could not be reached."""
    with tempfile.TemporaryDirectory() as tmp, _daemon(_LISTING):
        for model in ("qwen3-embedding:8b-cloud", "big"):
            st = stack.build(embed_model=model, hyde_model=None, rerank_model=None,
                             cache_path=str(Path(tmp) / "cache.db"))
            assert st.embedder is None, model
            assert st.unavailable_arms == ("embedder",), st.unavailable
            assert local_models.CLOUD_REASON in st.unavailable[0], st.unavailable
            assert "unreachable" not in st.unavailable[0], st.unavailable


class _Built(Exception):
    """`stack.build` returned; carries what it built."""


def test_serve_never_wires_a_cloud_embed_flag() -> None:
    """`serve.toml` refuses `embed_model`; `serve --embed-model` goes to the server unchanged and
    must not be the way round it. The real `stack.build` runs; the server stops right after it.
    A model only the listing marks cloud, so the arm's own refusal is what is tested whether or not
    the server also refuses a cloud NAME at launch."""
    from substrate.mcp import server

    real = server.stack.build

    def built(**kwargs):
        raise _Built(real(**kwargs))

    with tempfile.TemporaryDirectory() as tmp, _daemon(_LISTING), \
            mock.patch.object(server.stack, "build", side_effect=built):
        try:
            _stderr_of(cli.main, ["serve", "--no-refresh", "--registry",
                                  str(Path(tmp) / "scopes.toml"),
                                  "--socket", str(Path(tmp) / "s.sock"),
                                  "--embed-model", "big",
                                  "--hyde-model", "off", "--rerank-model="])
        except _Built as b:
            st = b.args[0]
        else:
            raise AssertionError("serve never reached stack.build")
    assert st.embedder is None and st.unavailable_arms == ("embedder",), st.unavailable
    assert "cloud" in st.unavailable[0], st.unavailable


def test_embed_refuses_a_cloud_model_before_anything_is_sent() -> None:
    asked: list = []
    with tempfile.TemporaryDirectory() as tmp, _daemon(_LISTING, asked):
        db = _index(Path(tmp))
        rc, err = _stderr_of(cli.main, ["embed", "--db", db, "--model", "qwen3-embedding:8b-cloud",
                                        "--cache", str(Path(tmp) / "cache.db")])
        with IndexStore(db, migrate=False) as s:
            stored = s.db.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
    assert rc == 2, (rc, err)
    assert "cloud model" in err and "--model" in err, err
    assert _embed_requests(asked) == [] and stored == 0, (asked, stored)


def test_embed_refuses_a_model_the_listing_marks_cloud() -> None:
    """A name that does not say "cloud", served from the cloud all the same. Said as cloud, not as
    "not available", which sends the reader to a daemon that is fine."""
    asked: list = []
    with tempfile.TemporaryDirectory() as tmp, _daemon(_LISTING, asked):
        db = _index(Path(tmp))
        rc, err = _stderr_of(cli.main, ["embed", "--db", db, "--model", "big",
                                        "--cache", str(Path(tmp) / "cache.db")])
    assert rc == 2, (rc, err)
    assert "cloud model" in err and "not available" not in err, err
    assert _embed_requests(asked) == [], asked


def test_embed_does_not_blame_a_flag_the_refresh_pass_never_passes() -> None:
    """The refresh pass runs `embed --scope` with no --model. When the listing marks the DEFAULT
    remote, it is refused without telling anyone to leave out a flag nobody gave."""
    listing = {"models": [{"name": engine.DEFAULT_MODEL, "remote_host": _REMOTE}]}
    asked: list = []
    with tempfile.TemporaryDirectory() as tmp, _daemon(listing, asked):
        db = _index(Path(tmp))
        rc, err = _stderr_of(cli.main, ["embed", "--db", db, "--cache", str(Path(tmp) / "c.db")])
    assert rc == 2, (rc, err)
    assert "cloud model" in err and engine.DEFAULT_MODEL in err and "--model" not in err, err
    assert _embed_requests(asked) == [], asked


def test_embed_still_embeds_with_a_local_model() -> None:
    """The control: the same fake, a local model, and the note is vectored."""
    asked: list = []
    with tempfile.TemporaryDirectory() as tmp, _daemon(_LISTING, asked):
        db = _index(Path(tmp))
        rc, err = _stderr_of(cli.main, ["embed", "--db", db, "--cache", str(Path(tmp) / "c.db")])
        with IndexStore(db, migrate=False) as s:
            stored = s.db.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
    assert rc == 0 and stored == 1, (rc, err, stored)


def test_eval_refuses_a_cloud_embedder() -> None:
    """`eval` builds its embedder itself, and falls back to lexical-only when one is unavailable.
    A cloud one is refused instead, by name and by the listing."""
    with tempfile.TemporaryDirectory() as tmp, _daemon(_LISTING):
        for model in ("qwen3-embedding:8b-cloud", "big"):
            rc, err = _stderr_of(cli.main, [
                "eval", "--no-hyde", "--no-rerank", "--embed-model", model,
                "--db", str(Path(tmp) / "absent.db"), "--baseline", str(Path(tmp) / "none.json"),
                "--cache", str(Path(tmp) / "cache.db")])
            assert rc == 2, (model, rc, err)
            assert "cloud model" in err and "not available" not in err, err
