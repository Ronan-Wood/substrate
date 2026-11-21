"""What a scope holds and whether it can be trusted right now — assembled once, rendered by both.

Sibling to `render`, which shapes retrieval RESULTS; this shapes what a caller needs to know
BEFORE reading one. It lives in the engine for the same reason: an adapter that assembled its own
status would answer a different question from the other adapter's, and the discrepancy would only
show up when someone compared them.

The honest ordering matters here. `index_version` says what the index was built from. Vector
coverage says whether the arm the capability envelope names can actually contribute. `refresh` says
what the unattended agent last managed to do. Drift says whether the vault has moved since. A
caller that reads only the first concludes an index is fine when three of the four say otherwise —
which is the whole reason `status` exists as a tool rather than as a line in the search response.

`refresh` and `drift` answer neighbouring questions and neither replaces the other. Drift is
computed HERE, live, from the notes on disk — authoritative, and a sweep over every note in the
scope. The refresh record is a lookup of what an agent reported, and it is the only one of the two
cheap enough to ride on every search response. They also disagree usefully: `drift.stale` with
`refresh.frozen` is a scope whose rebuild refused, while `drift.stale` with a clean refresh is
simply an edit made since the last tick.
"""

from __future__ import annotations

from collections.abc import Iterable

from substrate import freshness, private, refresh_state, scopes
from substrate import quarantine as _quarantine
from substrate import vault as _vault


def arms(stack) -> dict:
    """Which arms this PROCESS is wired with — distinct from what any one query achieved, which is
    what the per-response capability envelope reports.

    `hyde_requested` is what HyDE was asked for (`auto`, a name, or None for off) and `hyde` is what
    it resolved to. Together they are the engine's Auto pick, said by the engine: a client that
    wants to show it reads it here rather than keeping a copy of `expand.pick_auto`."""
    return {
        "embedder": getattr(stack.embedder, "key", getattr(stack.embedder, "model", None))
        if stack.embedder else None,
        "hyde": getattr(stack.expander, "model", None) if stack.expander else None,
        "hyde_requested": stack.hyde_requested,
        "reranker": getattr(stack.reranker, "model", None) if stack.reranker else None,
        "unavailable": list(stack.unavailable),
    }


def vector_status(store, stack) -> dict | None:
    """Coverage for the CONFIGURED embedder, or null when no vector arm is wired.

    COMPLETENESS, not presence: a partially-embedded index answers lexically while a wired
    embedder reports live, which is what stamped a measured MRR on a lexical-only run before the
    per-query guard existed. This surfaces the same condition ahead of the query rather than only
    in the degradation that follows it.
    """
    if stack.embedder is None:
        return None
    key = getattr(stack.embedder, "key", getattr(stack.embedder, "model", ""))
    n, total = store.vector_coverage(key)
    # An EMPTY index satisfies `n >= total` as 0 >= 0. Reporting that as complete claimed full
    # vector coverage over a store with nothing in it — zero chunks is the absence of coverage,
    # not the completion of it. Same correction as the per-query guard in _retrieve.
    complete = total > 0 and n >= total
    if complete:
        note = None
    elif total == 0:
        note = "the index holds no chunks — recompose this scope"
    else:
        note = f"the vector arm cannot contribute — run `substrate embed --db {store.path}`"
    return {"model": key, "stored": n, "chunks": total, "complete": complete, "note": note}


def engine_keys_block(vaults, held: list[dict] | None) -> dict:
    """`status.engine_keys`: the renames each vault's manifest declares (`[engine_keys]`), one row
    per renamed field, or None when the scope's vaults could not be resolved; and how many notes
    the last compose quarantined for the guard (`engine_keys.GUARD_KIND`: a withheld or frozen
    value under an old name; one whose value there cannot be read is `frontmatter_unreadable`,
    listed under `quarantined` like any other), None when this index
    keeps no quarantine record. Engine field names, vault names and the names the operator's own
    manifest gives, never a value from a note."""
    from substrate import engine_keys

    renamed = None
    if vaults is not None:
        renamed = []
        for v in vaults:
            for f, name in v.keys.renamed:
                renamed.append({"vault": v.name, "field": f, "key": name})
    return {"renamed": renamed,
            "guarded": None if held is None else sum(
                1 for q in held if _quarantine.checked(q.get("kind"), None)[0]
                == engine_keys.GUARD_KIND)}


def _fingerprints(scope):
    """A note's `freshness.quarantine_fingerprint`, over the same inputs compose judged it on: the
    note, its source's `_meta.md`, found inside the note's own vault, and that vault's renames."""
    by_path = {n.path: n for n in scope.notes}

    def fingerprint(path):
        n = by_path.get(path)
        root, keys = (path.parent, "") if n is None else (scope.root(n), n.keys.signature)
        return freshness.quarantine_fingerprint(path, _vault.source_meta_path(path, root), keys)

    return fingerprint


def _reread(store, scope):
    """Whether a note's vault is read with other renames than the index was composed with
    (`IndexStore.engine_keys`): its reading changed with its bytes unchanged."""
    recorded = store.engine_keys()
    moved = {n.path for n in scope.notes
             if recorded.get(str(scope.root(n)), "") != n.keys.signature}
    return lambda path: path in moved


def _shown_quarantine(q: dict) -> dict:
    """One quarantined note as a client sees it: the vault, the path inside it, and the kind of
    fault and the engine key, held to the contract's vocabularies. Never the reason, which quotes
    the note: a call's frontmatter value would reach a caller that did not ask for calls."""
    kind, key = _quarantine.checked(q.get("kind"), q.get("key"))
    return {"vault": q["vault"], "path": q["path"], "kind": kind, "key": key}


# How much of the identical notes `identical_report` lists: a count of each, and the first ones.
# A month of daily notes made from one template is one group of thirty, and a vault copied into a
# folder of itself is a duplicate per note, so neither is listed whole.
MAX_DUPLICATES_LISTED = 50
MAX_GROUPS_LISTED = 20
MAX_GROUP_NOTES_LISTED = 10


def identical_report(scope, quarantined: Iterable[tuple[str, str]] = ()) -> dict:
    """What `status` says about notes with the same text (`vault.Scope.duplicates` and
    `.identical`), from the vault as it stands, as `drift` is. Each note is named by its vault and
    its path inside it, never by anything it holds. `quarantined` names the notes, as (vault,
    vault-relative path), that are not in the index for failing on their own: `status` passes the
    index's quarantine record, compose the notes it quarantined.

      * `duplicates`, `duplicate_notes`: the copies left out because another note derives the
        same doc_id from the same bytes, each with that note as `copy_of`. That note is indexed,
        unless it is quarantined (`quarantined` says so), and then neither answers;
      * `groups`, `notes`, `listed`: the groups of two or more notes with the same text that the
        vault composes, less those the index quarantines, and the notes in them, each group with
        its size and its first notes. A quarantined note is in no group, and a group it leaves
        with one note is no group: neither answers twice. Like `drift`, this is the vault as it
        stands, so a note added since the last compose counts before it is indexed. A sync
        conflict copy shows here: it is indexed, and answers what the note it copies answers;
      * `empty`: such notes with no body (nothing after the frontmatter but headings: an empty
        note, an unfilled template) that have the same text as another of them.
        Counted, not listed: they index no passage, so none answers anything twice, and thirty
        `Untitled` notes would bury the group that matters.

    Ordered first by path (`vault._first_by_path`), so two calls on one vault list the same
    notes."""
    held = set(quarantined)
    indexed = [g._replace(notes=kept) for g in scope.identical
               if len(kept := tuple(n for n in g.notes
                                    if (n.vault, scope.relative(n)) not in held)) > 1]
    with_body = [g for g in indexed if g.body]
    return {
        "duplicates": len(scope.duplicates),
        "duplicate_notes": [_duplicate_note(scope, n, kept)
                            for n, kept in scope.duplicates[:MAX_DUPLICATES_LISTED]],
        "groups": len(with_body),
        "notes": sum(len(g.notes) for g in with_body),
        "listed": [_identical_group(scope, g) for g in with_body[:MAX_GROUPS_LISTED]],
        "empty": sum(len(g.notes) for g in indexed if not g.body),
    }


def _note_ref(scope, n) -> dict:
    """A note as `status` names it: its vault and its path inside it."""
    return {"vault": n.vault, "path": scope.relative(n)}


def _duplicate_note(scope, n, kept) -> dict:
    """A copy left out, and the note composed in its place."""
    return {"vault": n.vault, "path": scope.relative(n), "copy_of": _note_ref(scope, kept)}


def _identical_group(scope, g) -> dict:
    """A group's size, and its first notes."""
    return {"notes": len(g.notes),
            "first": [_note_ref(scope, n) for n in g.notes[:MAX_GROUP_NOTES_LISTED]]}


def status_payload(store, entry, *, stack, registry: str | None = None,
                   refresh: dict | None = None, drift: bool = True) -> dict:
    """Everything `status` reports for one composed scope.

    The `refresh` block is the SAME shape `render.search_payload` emits — one reader, one key set,
    so a caller that learned to read it on a search result does not have to learn a second dialect
    here. It carries no clock-derived field for the reason given in `render`: the envelope is
    compared as a whole object across two processes, and a shape that varied between the two
    payloads would put that comparison back where it started.
    """
    s = store.stats()
    held = store.quarantined()
    quarantined = None if held is None else [_shown_quarantine(q) for q in held]
    out = {
        "scope": entry.name,
        "db": str(entry.db),
        "vault": None if entry.vault is None else str(entry.vault),
        "composed": entry.composed,
        "index_version": store.index_version,
        # No `rebuilt_empty` field: every read path now opens with migrate=False, so a schema
        # mismatch REFUSES (naming both versions) instead of silently rebuilding. The flag could
        # only ever report False here, and a field that cannot vary reads as a check that ran.
        "documents": s["documents"],
        "passages": s["passages"],
        "outlines": s["outlines"],
        "schema_version": s["schema_version"],
        "by_vault": store.counts_by("vault"),
        "by_tier": store.counts_by("tier"),
        "by_status": store.counts_by("status"),
        "by_doc_type": store.counts_by("doc_type"),
        "by_confidence": store.counts_by("confidence"),
        # How many of those notes declare no status, and how many no doc_type: compose indexed
        # them as `active` and `reference`, and the counts above cannot tell them from notes that
        # declared it. Null when this index keeps no record, which is not zero
        # (`IndexStore.defaulted_counts`).
        "defaulted": store.defaulted_counts(),
        # THE NOTES THIS INDEX LEAVES OUT, each with why: the last compose quarantined them,
        # because each failed on its own, and indexed the rest. Null when this index keeps no
        # record (an engine from before quarantine, which refused instead), which is not empty.
        # In agent mode always null: a quarantined note's class may be unknown, so its path is
        # not the agent's to read (`agent._fill`).
        "quarantined": quarantined,
        # NOTES WITH THE SAME TEXT, from the vault as it stands, as `drift` is: the copies left out
        # because another note derives the same doc_id from the same bytes, and the groups each
        # indexed under its own name, where a sync conflict copy shows (`identical_report`).
        # Null when the vault is not read: agent mode, or a scope that does not resolve.
        "identical": None,
        # WHO THIS CORPUS IS ABOUT, ordered by how many notes name each. Empty when the scope's
        # vault declares no identity roster, which is most of them — an absent list here means
        # "nobody was resolved", never "nobody was mentioned".
        "entities": store.entity_roster(),
        "retrieval_arms": arms(stack),
        "vectors": vector_status(store, stack),
        "refresh": refresh if refresh is not None else refresh_state.report(entry.name, registry),
        # Each index path another local user can read or list, the folders holding it included.
        # Empty is the only safe value. The next compose seals what the engine owns; a folder
        # outside ~/.substrate, or an index root holding files it did not write, stays listed
        # until the operator changes it. Null in agent mode: a snapshot is 0640 by design, shared
        # with its folder's group, and not this process's to judge.
        "exposed": (private.exposed_index(entry.db, entry.index_root) if drift else None),
    }
    if not drift:
        # Agent mode: the vault is not this process's to read, so drift is UNCHECKED and says so in
        # its own slot, the same way an unresolvable scope does below. `refresh.snapshot` is what
        # stands in for it — how old the published copy is.
        out["drift"] = {"error": "not checked — this server answers from a published snapshot "
                                 "and cannot read the vault it was built from."}
        # The renames are the vault's manifest's, which this process does not read, and the
        # quarantine record is not in a snapshot.
        out["engine_keys"] = None
        return out
    out["engine_keys"] = engine_keys_block(None, held)
    try:
        scope = _vault.resolve_scope(entry.vault)
        out["engine_keys"] = engine_keys_block(scope.vaults, held)
        out["drift"] = freshness.drift(store, [n.path for n in scope.notes], held,
                                       _fingerprints(scope), _reread(store, scope))
        out["identical"] = identical_report(scope, ((q["vault"], q["path"]) for q in held or []))
    except _vault.VaultError as e:
        # A scope that cannot be RESOLVED cannot be drift-checked — the vault is gone, a manifest
        # is malformed, or a skip-list file declares a spine (`_refuse_skipped_note_with_a_spine`).
        # Reported as an error IN the drift slot rather than omitted: a missing key reads as
        # "nothing has changed". Note the coarseness — the whole added/changed/removed breakdown is
        # replaced by the message, so `status` says UNCHECKABLE for an authoring fault too.
        out["drift"] = {"error": str(e)}
    return out


def scopes_payload(registry: str | None = None) -> dict:
    """Every registered scope and what it composes.

    `sources` is resolved fresh from the manifest rather than remembered, because `inherits` can
    change and a vault can move after registration. A scope whose inheritance no longer resolves
    is listed WITH its fault — omitting it would read as "that scope was never composed", which is
    the opposite of the truth.
    """
    out = []
    for name, entry in sorted(scopes.load(registry).items()):
        row = {"scope": name, "db": str(entry.db), "vault": str(entry.vault),
               # Null and `pending` while it waits for its first compose (`scopes.register`), so
               # a client can tell a scope not built yet from one whose index went missing.
               "composed": entry.composed or None, "pending": entry.pending,
               "index_present": entry.db.is_file(),
               # This is the "how are all my scopes doing" view — it already reports
               # `index_present` and a per-scope fault, so a frozen scope rendering here exactly
               # like a healthy one is the same indistinguishability the field exists to remove,
               # moved one surface over. Someone triaging with a bare `substrate status` would see
               # six clean lines while one had been answering from a superseded index for a week.
               "refresh": refresh_state.report(name, registry)}
        try:
            row["sources"] = [v.name for v in _vault.resolve_vaults(entry.vault)]
        except _vault.VaultError as e:
            row["sources"] = None
            row["error"] = str(e)
        out.append(row)
    return {"scopes": out, "registry": str(scopes.registry_path(registry))}
