"""A guarded vault withholds its OWN notes unless something vouches for it, right now.

EVERY TEST HERE IS A WITHHOLDING TEST except two. That ratio is the point: this is the wall that
keeps a model client out of the operator's call transcripts, and a wall is only worth what its
failure modes are worth. A guard that fell open on a missing file, an unparseable file, a `true`
heartbeat or a clock skew would be defeated by producing that condition — and a local process can
produce all four.

WITHHOLDS, NOT REFUSES. The verdict names the guarded vault's own documents; everything the scope
INHERITS still answers. Refusing the whole scope was the first design and it was measured too blunt:
69 curated notes held back to protect zero calls.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from substrate import guard  # noqa: E402


def _withheld(v: guard.Verdict) -> bool:
    """A verdict that holds something back AND says why. The two travel together: a
    withholding with no reason is content vanishing silently."""
    return bool(v.withhold) and bool(v.reason)


def _vault(tmp_path: Path, *, guard_state: str | None, name: str = "acme") -> Path:
    vault = tmp_path / name
    vault.mkdir(parents=True, exist_ok=True)
    lines = [f'name = "{name}"', "inherits = []"]
    if guard_state is not None:
        lines.append(f'guard_state = "{guard_state}"')
    (vault / ".substrate.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return vault


def _state(tmp_path: Path, **fields) -> Path:
    path = tmp_path / "mcp-state.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


# --------------------------------------------------------------- the two that pass

def test_an_unguarded_vault_is_untouched(tmp_path: Path) -> None:
    """The operator's own vaults declare no guard and must not gain one. Absence of a declaration
    is not a failed check — if this ever refuses, every curated scope stops answering."""
    assert guard.check(_vault(tmp_path, guard_state=None), "acme").vouched


def test_a_fresh_beat_naming_this_scope_passes(tmp_path: Path) -> None:
    state = _state(tmp_path, heartbeat=time.time(), activeScope="acme")
    assert guard.check(_vault(tmp_path, guard_state=str(state)), "acme").vouched


# --------------------------------------------------------------- everything else refuses

def test_the_withheld_set_names_the_guarded_vault_and_nothing_else(tmp_path: Path) -> None:
    """The scope AND the directory. `compose` registers the scope as the manifest's `name` but
    records the vault DIRECTORY on the notes it contributed, so withholding the scope alone missed
    every note of a vault whose directory is named otherwise. The inherited tiers are untouched."""
    vault = _vault(tmp_path, guard_state=str(tmp_path / "nope.json"), name="acme")
    v = guard.check(vault.rename(tmp_path / "acme-workspace"), "acme")
    assert v.withhold == frozenset({"acme", "acme-workspace"})
    assert "core-vault" not in v.reason


def test_the_withheld_set_names_the_directory_a_link_resolves_to(tmp_path: Path) -> None:
    """Compose resolves the vault before recording its name, so a registry row naming a link
    must withhold the name of the directory behind it."""
    real = _vault(tmp_path, guard_state=str(tmp_path / "nope.json"), name="acme")
    link = tmp_path / "acme-link"
    link.symlink_to(real, target_is_directory=True)
    assert "acme" in guard.check(link, "workspace").withhold


def test_a_missing_state_file_refuses(tmp_path: Path) -> None:
    """THE ONE A LOCAL PROCESS CAN ALWAYS ARRANGE. If deleting a file opened the vault, the wall
    would be decoration."""
    vault = _vault(tmp_path, guard_state=str(tmp_path / "nope.json"))
    v = guard.check(vault, "acme")
    assert _withheld(v), v
    # And it says the index is fine, so a refusal is not read as a broken corpus.
    assert "not reporting" in v.reason


def test_a_stale_beat_refuses(tmp_path: Path) -> None:
    state = _state(tmp_path, heartbeat=time.time() - (guard.STALE_AFTER_SECONDS + 5),
                   activeScope="acme")
    assert _withheld(guard.check(_vault(tmp_path, guard_state=str(state)), "acme"))


def test_a_beat_just_inside_the_window_still_passes(tmp_path: Path) -> None:
    """The boundary is asserted so a later tightening is a decision rather than a regression."""
    state = _state(tmp_path, heartbeat=time.time() - (guard.STALE_AFTER_SECONDS - 5),
                   activeScope="acme")
    assert guard.check(_vault(tmp_path, guard_state=str(state)), "acme").vouched


def test_another_workspace_being_active_refuses(tmp_path: Path) -> None:
    """The partition, which is the whole feature: the app is running and vouching for a DIFFERENT
    corpus, so this one stays shut."""
    state = _state(tmp_path, heartbeat=time.time(), activeScope="personal")
    v = guard.check(_vault(tmp_path, guard_state=str(state)), "acme")
    assert _withheld(v), v
    assert "'personal'" in v.reason


def test_a_boolean_heartbeat_refuses(tmp_path: Path) -> None:
    """`True` is an `int` in Python, so an unguarded `isinstance(beat, (int, float))` would read it
    as 1 — an epoch in 1970, which is stale, so this one happens to fail safe. `False` is 0. Both
    are asserted because the guard must refuse them for being the wrong TYPE, not by luck."""
    for value in (True, False):
        state = _state(tmp_path, heartbeat=value, activeScope="acme")
        v = guard.check(_vault(tmp_path, guard_state=str(state)), "acme")
        assert _withheld(v), value
        assert "numeric heartbeat" in v.reason


def test_a_future_beat_refuses(tmp_path: Path) -> None:
    """A clock this side cannot check is not a very fresh app."""
    state = _state(tmp_path, heartbeat=time.time() + 3600, activeScope="acme")
    assert _withheld(guard.check(_vault(tmp_path, guard_state=str(state)), "acme"))


def test_unparseable_or_wrongly_shaped_state_refuses(tmp_path: Path) -> None:
    path = tmp_path / "mcp-state.json"
    vault = _vault(tmp_path, guard_state=str(path))
    for body in ("not json", "[]", '"a string"', "null", "{}", '{"heartbeat": 1}'):
        path.write_text(body, encoding="utf-8")
        assert _withheld(guard.check(vault, "acme")), body


def test_a_missing_active_scope_refuses(tmp_path: Path) -> None:
    """Running but not saying which corpus it vouches for is not consent to all of them."""
    state = _state(tmp_path, heartbeat=time.time(), activeScope="")
    v = guard.check(_vault(tmp_path, guard_state=str(state)), "acme")
    assert _withheld(v), v
    assert "which corpus it is vouching for" in v.reason


def test_an_oversized_state_file_refuses(tmp_path: Path) -> None:
    path = tmp_path / "mcp-state.json"
    path.write_text(" " * (guard.MAX_STATE_BYTES + 1), encoding="utf-8")
    assert _withheld(guard.check(_vault(tmp_path, guard_state=str(path)), "acme"))


def test_a_guard_declared_as_a_non_string_refuses(tmp_path: Path) -> None:
    """A guard someone tried to write and got wrong is not an unguarded vault."""
    vault = tmp_path / "acme"
    vault.mkdir()
    (vault / ".substrate.toml").write_text(
        'name = "acme"\ninherits = []\nguard_state = true\n', encoding="utf-8")
    assert _withheld(guard.check(vault, "acme"))


def test_a_manifest_that_cannot_be_read_withholds(tmp_path: Path) -> None:
    """A manifest that cannot be read cannot say whether the vault is guarded, so its own notes
    are withheld."""
    vault = tmp_path / "acme"
    vault.mkdir()
    (vault / ".substrate.toml").write_text("this is not toml = = =", encoding="utf-8")
    v = guard.check(vault, "acme")
    assert _withheld(v) and "malformed manifest" in v.reason
    (vault / ".substrate.toml").unlink()
    assert "is missing" in guard.check(vault, "acme").reason
    vault.rmdir()
    assert "not there" in guard.check(vault, "acme").reason


def test_a_guard_written_below_a_table_refuses(tmp_path: Path) -> None:
    """TOML puts a key written after a `[table]` header inside that table, so `guard_state` below
    `[reference_pins]` was absent at top level and the vault read as unguarded. A guard someone
    wrote in the wrong place is a guard, not its absence."""
    for i, tail in enumerate(('\n[reference_pins]\ngo = "1.21"\nguard_state = "{s}"\n',
                              '\n[extra]\nguard_state = "{s}"\n',
                              'reference_pins.guard_state = "{s}"\n',
                              '\n[""]\nguard_state = "{s}"\n')):
        vault = tmp_path / f"acme-{i}"
        vault.mkdir()
        state = _state(tmp_path, heartbeat=time.time(), activeScope="acme")
        (vault / ".substrate.toml").write_text(
            'name = "acme"\ninherits = []\n' + tail.format(s=state), encoding="utf-8")
        v = guard.check(vault, "acme")
        assert _withheld(v), tail
        assert "guard_state" in v.reason and "table" in v.reason, v.reason


def test_a_misplaced_guard_withholds_even_beside_a_valid_one(tmp_path: Path) -> None:
    """Two declarations, one of them in a table: which one was meant is not this module's guess."""
    state = _state(tmp_path, heartbeat=time.time(), activeScope="acme")
    vault = tmp_path / "acme"
    vault.mkdir()
    (vault / ".substrate.toml").write_text(
        f'name = "acme"\ninherits = []\nguard_state = "{state}"\n\n[extra]\n'
        f'guard_state = "{state}"\n', encoding="utf-8")
    assert _withheld(guard.check(vault, "acme"))
