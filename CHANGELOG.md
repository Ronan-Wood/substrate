# Changelog

Every release of the engine, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[semantic versioning](https://semver.org/). Each release attaches its source tarball and a
`SHA256SUMS` (see [`RELEASING.md`](RELEASING.md)).

## Unreleased

### Added

- **A vault's manifest may declare `skip`,** the file and folder names compose never indexes in
  that vault. The list used to be fixed: `_meta.md`, `structure.md`, `MEMORY.md`, `log.md`,
  `WRITING.md` and anything under `99-templates/`. That list is now the default, so a vault that
  declares nothing composes as before. `skip = []` indexes every `.md` file. Each vault's list
  governs its own files, and a vault with no manifest has the default. A skipped file that
  declares a spine still fails the compose, under whichever list is in effect.
  [`docs/configuration.md`](docs/configuration.md) has the rules.
- After `skip` changes, the next compose or refresh pass takes the notes under a newly skipped
  folder out of the index, without `--clean`, and adds the newly unskipped ones. Compose removes
  such a note's ingest folder as it removes a deleted note's, and says so on a `pruned:` line.
- **A vault's manifest may declare `writable_folders`,** the folders `ingest` adds a note to and
  `amend` changes one in, as paths relative to the project vault (`.` is its root), each with
  every folder below it. A declared folder must exist and not be skipped.
  [`docs/configuration.md`](docs/configuration.md) has the rules.
- **A note that fails on its own is quarantined, and the rest of the scope is indexed.** Before,
  one such note made compose exit 3 having indexed nothing, and a refresh pass froze the scope
  until the note was fixed. Now the note is left out entirely: never indexed under a fallback
  class or spine, and an earlier version of it is removed from the index. Compose names each one
  with its reason, as a `WARNING (quarantine)` on stderr and a `quarantined:` line on stdout; a
  refresh pass's report names it by kind and key.
  `substrate status` names each with its reason, which quotes the note. Over MCP the `status` tool
  lists them under `quarantined` by `vault`, `path`, `kind` and `key`, never a value from the
  note; the kinds and keys are closed vocabularies in the contract's `quarantine` section.
  `drift.quarantined` counts the ones unchanged since. The refresh record keeps the
  count and the first 50 notes, and every reply's `refresh` block carries the count as
  `refresh.quarantined`. A scope with quarantined notes is not frozen. Agent snapshots carry none
  of it, because a quarantined note may be a call whose class was never read.
- **`quarantine_ceiling`, a manifest key,** sets how many notes may be quarantined before compose
  refuses the whole scope again. Without it the ceiling is the larger of 10 notes and 1% of the
  scope's notes, rounded down. `0` refuses on any failed note, as compose did before; `"none"`
  never refuses for the count. Only the project vault's applies. A compose whose every note
  failed refuses whatever the ceiling says, so the earlier index stays.
- **`substrate init <vault>` writes a minimal `.substrate.toml`** into an existing folder of
  notes: `name`, from the folder name lowercased with accents dropped and each run of other
  characters made one `-`, or `--name`. It writes that one file and nothing else, never replaces
  or follows an existing one, creates no folder, and refuses a folder inside or around another
  vault and a name another vault is registered under. It prints the next commands; compose
  registers the scope. [`docs/configuration.md`](docs/configuration.md) has the three commands.
- **`substrate --version`** prints the engine's version on one line; `substrate version` keeps
  its JSON. **`substrate help [command]`** prints what `--help` prints.
- **`search` is the CLI's name for `query`,** as it is the MCP tool's. `query` is the same
  command, with the same flags, output and exit codes.
- **`[engine_keys]`, a manifest table, has the engine read a spine field from another property,**
  for a vault whose notes use `status`, `doc_type`, `confidence` or `class` for values of their
  own (`status: done`, `class: Math 101`). Such a note was quarantined as a value outside the
  vocabulary, and past the ceiling the scope refused. With `status = "substrate_status"` the
  engine reads the note's status from `substrate_status`, and the vault's own `status` is left
  alone. Each field is renamed on its own and none by default, so a vault with no table reads as
  before. Under the new name the field is judged as before: a value outside the vocabulary is
  quarantined, no value is mapped or defaulted, and the name is read under exactly that spelling.
  Each vault's manifest renames for its own notes only, and a source's `_meta.md` is always read
  under the engine's names. When the old name still holds a value that would withhold or freeze
  the note under the engine's own name (`archived`, `superseded`, `conversation`,
  `reference-frozen`), as the engine or YAML reads it, and the note does not declare that value
  under the new name, the note is quarantined as `withheld_value_under_renamed_key`, and `amend`
  refuses it, so a call written for the engine is never indexed as a live note because of a
  rename. A setting the engine cannot apply (a field other than the four, a misnamed table or one
  holding only spine fields, a name it reads or writes itself, two fields to one name) fails the
  compose by name. `ingest` and `amend` read and require the spine under the new names.
  `status` reports the renames and the notes the guard holds under `engine_keys`, and `drift`
  counts every note of a vault whose renames changed as `changed`. `substrate init` and the
  over-ceiling refusal name the setting when notes hold values of the vault's own under a spine
  key, per field and with the lines to add; `init` prints them and writes `name` alone. Two copies
  of a note under one name in vaults that read its spine keys differently refuse the scope, as
  copies whose `_meta.md` files differ do. [`docs/configuration.md`](docs/configuration.md) has
  the rules.

### Changed

- **`substrate --help` says what the engine does and the three commands a new vault needs,** and
  every command it lists has a description. `ingest-md`, `verify`, `rechunk`, `index`, `eval`,
  `review` and `refresh-record` are left out of the listing; each still runs, and
  `substrate help <command>` shows it. An unknown command exits 2 with the command it is
  probably a slip for, as an unknown manifest key does, instead of the list of every command.
- **Compose and register, refusing a vault with no manifest, name `substrate init <vault>`.**
  They still refuse it rather than guess a scope.
- **Notes with the same bytes compose.** Two such notes refused the whole scope (exit 2, "two notes
  are byte-identical"), and so did one name with the same text in two folders ("doc_id … produced
  by two notes"). An ordinary vault does both without any sync conflict: unfilled daily notes from
  one template, two empty `Untitled` notes, an empty `Untitled.md` in two folders. Now notes with
  the same text under different names are each indexed as their own note, under their own titles.
  Of notes that also derive one `doc_id` (one name in two folders, or `Todo.md` and `todo.md`), the
  first by path is indexed and the others are left out, each named as its copy: compose prints a
  `duplicate:` line, a refresh pass's report names it, and `status` lists it under a new
  `identical` key (`duplicates`, `duplicate_notes`) with the groups of identical notes that are
  indexed (`groups`, `listed`), so a sync conflict copy stays visible. A slug keeps 48 characters,
  so two long names that differ only after them are copies too. A copy left out is not
  quarantined, does not count toward the ceiling, and is not drift. A `doc_id` a note declares
  that another note has still refuses the scope, a conflict copy of such a note included, and so
  does one derived `doc_id` from two notes whose bytes differ, or from copies whose sources'
  `_meta.md` files are not the same bytes. One file compose finds twice under one name (a vault
  inside the vault that inherits it, also under another spelling where the volume reports inode
  numbers, or a hard link of the same name) refuses on its own message, where it used to refuse
  as a `doc_id` produced by two notes.
  `amend` pins the `doc_id` it derives, so amending either note of such a pair makes the next
  compose refuse until one is deleted. [`docs/configuration.md`](docs/configuration.md) has the
  rules.
- **A key `.substrate.toml` doesn't define fails the compose.** A manifest used to compose with
  an unknown key ignored, so a misspelt `skips` or `writeable_folders` left the default in place
  of what was written. This breaks a manifest that carries an extra key, a typo or one another
  tool keeps there: it fails until the key is fixed or moved. `guard_state`, and Scripta's
  `scripta_workspace` and `scripta_workspace_vault`, are still accepted and never read. The
  message names the key and the file, and the known key it looks like. Keys are compared exactly,
  so `Skip` is unknown. A table of any name is not checked, so another tool's settings can go
  under a `[table]` of its own. `ingest` and `amend` write nothing while the unknown key could be
  one they read. Where it could be `inherits`, the scope's vaults are unknown, and until it is
  fixed a compose creates a new index root only directly in `~/.substrate/index`.
  [`docs/configuration.md`](docs/configuration.md) lists the keys.
- **A note with no `title:` and no heading takes its file name as its title.** It used to fail
  the class gate (`missing required field(s) ['title']`), so since the quarantine change a vault of
  daily notes, quick captures, lists and clippings lost each one, and refused the whole scope past
  the ceiling. The order is a declared `title:`, then the first level-1 heading, then the first
  heading of any level, then the file name, and `run.json` records `title_source: filename`. A
  note with a `title:` or a heading keeps the title it has; a `reference-versioned` note still
  needs its version. The name loses its `.md`, is put in NFC, and has each run of control
  characters and line breaks made one space and surrounding whitespace removed; a name with
  nothing visible left gives no title, and the note is quarantined as before. `ingest` and `amend`
  judge a note under the file name it is written to, so removing a note's only heading with
  `amend` makes its file name its title. `ingest-md --md` takes the file name too, as a `.txt` and
  a converted file already did. The title is not indexed text, as a declared `title:` is not.
  Such a note that declares no class is `unclassified` like any other: retrieved by default and
  published in agent snapshots, so a transcript with no frontmatter and no heading, quarantined
  until now, is no longer withheld. Declare `class: conversation` on a transcript.
- An empty note, or one holding only frontmatter, composes as a document with no passage, as a
  heading-only note already did: `documents` lists it by its file name, and `search` never
  returns it. Any number of them compose (above).
- **`ingest` and `amend` write in any folder of a vault that declares no `writable_folders`,**
  except a folder its `skip` list skips, a hidden one, a `passages/` folder, and `_sources/` and
  `_archive/` at the top of the vault. They used to write only under `02-areas/`,
  `03-references/` and `04-synthesis/`, and `ingest` only directly in one of them. So in such a
  vault `00-index/`, the root, nested folders and any new folder now take a write. `_sources/` and
  `_archive/` stay unwritable because that list was all that protected them, and in `_sources/`
  only a class-frozen call was protected otherwise. A vault that lists them in `writable_folders`
  opens them. A `conversation` or `reference-frozen` note is still never amended, wherever it is.
  To keep today's folders, declare
  `writable_folders = ["02-areas", "03-references", "04-synthesis"]`. That restores `amend`
  exactly. `ingest` then also takes a folder below one of them.
- Because `ingest` now creates folders a client names, it refuses what that would open:
  - a folder whose name ends in `.md`, which compose would read as a note and refuse the scope;
  - a skipped or `passages` folder, or a skipped file name, in another letter case;
  - a name holding a tab or a character a viewer would hide;
  - a new folder named in decomposed Unicode;
  - a name too long for the volume, or a path too long for compose to read.
- `ingest` and `amend` write nothing in a project vault that holds a vault its scope inherits:
  one that any `inherits` in the chain names at or inside the project vault's folder, whether it
  exists yet or not. The refusal names the entry and says to move that vault out. A layout that
  skips the nested folder composes, and still takes no write.
- `ingest` and `amend` write nothing in a project vault whose `.substrate.toml` is missing. They
  used to fall back to the three fixed folders. Now the fallback would be the much wider default,
  so they refuse instead.
- The `ingest` tool's `folder` is any folder path, not one of three names, and the tool's
  description says which folders take a note. A refusal for a folder outside a declared list names
  the writable ones.
- `writable_folders` is a reserved manifest key. Written inside a `[table]`, it fails the compose.
  A `writable_folders` the engine can't apply is refused by `ingest` and `amend`, which then write
  nothing: one that isn't a list of folder paths, or names a folder that doesn't exist, is reached
  through a symlink or is skipped. It doesn't affect what compose indexes.
- A per-note failure no longer fails the compose. What still refuses the whole scope: a duplicate
  `doc_id`, a missing declared vault, a manifest the engine can't read
  or a key it doesn't define, a skipped file that declares a spine, a `_meta.md` the vault layer can't read, an ingest folder it
  can't write safely, a note the filesystem refuses to read, more failures than the ceiling, and
  a compose whose every note failed.
  A note that is not UTF-8 or is over the size cap is quarantined, where the vault layer used to
  refuse the scope for it.

- `skip` is a reserved manifest key. Written inside a `[table]`, it fails the compose, as the
  other top-level keys do, so a reference pin can no longer be named `skip`. A `skip` that is not
  a list of exact names fails the compose too.
- `ingest` without a `folder` writes to `04-synthesis` only where that folder exists and is
  writable. Elsewhere it refuses and asks for a folder. It used to create `04-synthesis`.
- `ingest` and `amend` refuse a name or a folder the vault's own `skip` list names, in place of
  the fixed list. A default name the list leaves out can be written, so under `skip = []` an
  `amend` of `log.md` works. `_meta.md` is still refused in every vault, whatever its list says.
  They now read the project vault's manifest, and write nothing while it doesn't parse or its
  `skip` is refused.
- **A note with no `status` or no `doc_type` composes with the defaults.** It is indexed as
  `active` and as `reference`, the defaults a standalone `ingest-md` already used. Before, one
  such note failed the compose and froze its scope, so a vault of plain notes could not compose.
  `substrate status --scope <name>` and the `status` tool report how many notes were defaulted on
  each field, as `defaulted` (`{"status": n, "doc_type": n}`). It is `null` for an index
  composed before this release, until its next compose. An empty value (`status:`) is read as no
  value, as it always was. A declared value outside the vocabulary still fails the note, as does
  `superseded` with no `superseded_by`, and the note is quarantined (above). The `ingest` and
  `amend` tools still require both fields.
  The index schema stays at 11 and no index is rebuilt: the record is a table the next compose
  adds.
  - A default does not stand in for frontmatter the engine could not read. A note with no
    `status` or no `doc_type` that starts with a `---` line the engine does not read as
    frontmatter still fails, and is quarantined. That covers a byte-order mark or an invisible character
    before it, fences that smart punctuation turned into `—-` or `—`, a block that never closes,
    a line that is not a plain key (a flow mapping, a YAML tag or anchor), a key the engine reads
    written twice, and a horizontal rule as the first line. An empty `---`/`---` block is empty
    frontmatter and composes, and so does a list written without indenting it when YAML reads the
    block as the same mapping, with the same value for each key that decides the spine or class.
    The same holds for a passage's `_meta.md`.
  - **Frontmatter only.** The engine reads a note's spine and class only from its YAML
    frontmatter. A `key:: value` line in the body is body text, whatever Dataview or Logseq make
    of it, so a note that declares its class only that way is `unclassified`.
  - **Exact spelling only.** The engine reads `status`, `doc_type`, `class`, `document_class` and
    `superseded_by` under exactly those names. A note that writes one of them another way
    (`Status:`, `doc-type:`, `docType:`, `"class":`, `superseded-by:`, or with an invisible
    character) fails
    and is quarantined, naming the note, the key as written and the spelling to use, whatever else it
    declares. `Status: archived` is never indexed as live, and `Class: conversation` never as an
    ordinary note. A vault that keeps a `Status` or `Class` property of its own has those notes
    quarantined until it renames the field with `[engine_keys]`. Keys of their own such as Pandoc's
    `documentclass`, Marp's `_class` and `doctype` are not taken for spellings, and compose.
    Values are unchanged: they are compared exactly as written.
  - A YAML tag or alias on a key the engine reads, in a block read as YAML, is now refused by
    name. Before, the key was left out without a word.
  - A key under another key is not the note's, and does not stop it composing.
  - **Withholding follows the class a note declares, never its folder.** A note with no
    frontmatter under `_sources/` used to freeze its scope. It now composes as `unclassified`
    and `active`, is retrieved by default, and is copied into agent snapshots. Declare
    `class: conversation` on a transcript before it goes into a vault.
- **Wire 4.** The new `status` keys (`defaulted`, `quarantined`, `identical`, `engine_keys`),
  `drift.quarantined` and `refresh.quarantined` are reply keys a newer client relies on, so
  `VERSION` is 4. `MIN_VERSION` stays 1: older clients ignore the keys, the write tools' new
  refusals reach them as ordinary tool refusals, and notes that refused a scope now compose. One
  change is breaking by the contract's own rule and was kept anyway: a note that answered at 0.2.2
  can now be quarantined, and drops out of an older client's answers with only `refresh.note` to
  say so.
  [`docs/client-contract.md`](docs/client-contract.md) lists each change.
- `contract/contract.json` declares three objects it left out: `defaulted`, `quarantined_note`
  (each row of `status.quarantined`) and `engine_health_arms` (`retrieval_mode.health.arms`). The
  suite now fails on any object a reply carries that the contract does not declare, except the
  two it names: the `by_*` maps, keyed by data, and each row of `status.entities`.

### Removed

- **`amend` no longer edits a tier 1 or 2 note.** A client never adds or edits a note whose path
  gives it the operator or reference tier (a name starting `00-operator` or `10-reference`, in any
  letter case or Unicode form), in any vault, whatever `writable_folders` says: those notes are promoted by hand.
  `amend` used to edit one under a nested folder such as `02-areas/00-operator/`. Edit those by
  hand.

### Fixed

- `ingest` refuses a folder that is a symlink, wherever it points. It used to follow the link and
  check only that the result was inside the vault, so a writable folder linked to a skipped or
  hidden folder of the same vault took a note compose would never index. It now walks the folder
  one component at a time without following a link, as `amend` does, and writes relative to the
  folder it reached.
- A refresh pass no longer reads compose's stdout for its report. It kept every line that started
  `pruned:`, and compose printed note names raw, so a note whose file name held a line break
  followed by `pruned: …` added a removal that never happened to the pass's report. Compose now
  writes its report to the pass as one JSON record per line, and the pass renders the lines with
  names escaped. A refused compose's report says why, and compose's stderr goes only to the
  engine's log, each line under a fixed `compose stderr |` prefix. A report that is missing, cut
  short or not records is said in the pass's report rather than read as nothing pruned. Compose's
  own output escapes names too. A compose that refuses before it starts removing ingest folders
  removes none and reports nothing pruned or quarantined.
- `ingest` and `amend` refuse `_META.md` and every other spelling of `_meta.md` that a volume can
  serve as it: any letter case, and on HFS+ the code points it leaves out of a name. On such a
  volume the file is read as its source's `_meta.md` and sets the spine of the passages beside it,
  and the refusal compared the name exactly.
- `substrate-mcp --hyde-model`, `substrate serve --hyde-model` and `substrate eval --hyde-model`
  refuse a model that runs in Ollama's cloud at launch, exit 2, as `--rerank-model` already did.
  Before, the server started with HyDE dropped, and `eval` measured the run without HyDE.
- **Agent snapshots are withdrawn when their entry goes.** Removing a `[publish.<scope>]` entry,
  emptying `agent-publish.toml`, or moving an entry's `dir` removes the snapshot at the next
  refresh. The engine records each folder it publishes into for an entry, before writing there,
  in `agent-published.json` beside the registry. A folder's record is dropped only once its copy
  is actually removed. A missing or unreadable `agent-publish.toml`, which can happen mid-save,
  withdraws nothing; leave it empty to withdraw everything. A snapshot published before this
  release whose entry is already gone is not known to the engine; remove that one by hand.
- A malformed `[publish.<scope>]` entry no longer stops every other scope's publish and
  withdrawal. A relative `dir`, a `~user` that doesn't exist, and a non-bool `include_sources`
  are each that entry's own error. The entry is refused, and a copy it left that
  may hold calls is withdrawn. So is any such copy when the whole file stops parsing.
- A snapshot without calls no longer changes its `fingerprint`, `index_version` or `published`
  when a call changes. Its `index_version` is now the copy's own, the value the agent's engine
  reports. Every snapshot is rewritten once after upgrading.
- `substrate eval --multi-query` refuses a model that the listing alone marks as cloud, as HyDE
  does.
- Compose, and `substrate ingest` for every format but PDF, write each of the four ingest files
  as a new `0600` file and rename it into place. They do this through a descriptor on the ingest
  folder, held from before the original is stored until the last write. A link or hard link
  planted at one of those names is replaced, not written through. A link, a folder of another
  user's, or a folder every user can write, at the ingest folder's own name, is refused before
  anything is stored, and compose refuses the scope. On those paths a `--out` that is itself a
  symlink is refused too. A group-writable folder is still accepted.
- An agent snapshot no longer carries vectors that no passage in it holds.

## 0.2.2

### Fixed

- The golden frames in `contract/frames/` are one capture again. In 0.2.1 the `status`,
  `documents` and `agent-status` frames came from an earlier capture than the rest, so the
  `status` frame named a different `index_version` from the `search` frame. A client that reads
  them as one index, as Scripta's decode tests do, failed. A test now holds the demo capture's
  `search`, `search-sources`, `expand`, `documents` and `status` frames to one `index_version`.

## 0.2.1

### Added

- **An engine write log, `~/.substrate/writes.jsonl`.** For each write the engine runs for a
  granted client, it appends an `intent` line, fsynced before the note is touched, then a `done`
  line after it, or a `void` line when the write was refused inside and nothing was written. A
  write the log can't record isn't run. Each line names the client, a prefix of its grant, the
  scope, the note, and the note's checksum before and after; never its content. The owner's own
  writes aren't logged. Nothing rewrites a line, and there is no rotation yet.
- **`provenance` on every passage**, in `search`, `expand` and `query --json`:
  - `state` is `engine` (the indexed bytes are what the engine last wrote, for `client`), `edited`
    (the engine wrote the note and it has changed since), `none` (no engine write) or `unknown`;
  - `stamp` says whether the note's own `amended_by`, else `written_by`, as the engine's reader
    reads the key, agrees with the log: `matches`, `contradicted`, or `unchecked` when there is
    no engine write to compare it with (a note stamped before the log, or by hand) or the note
    was not read. It is null when the note has none.

  `state` is judged against the write that produced the bytes the index holds, so an index that
  lags a write isn't reported as an edit, and the stamp is read from those same bytes. A note is
  found by its path only: one that moved reads `none`. Looked up at query time from the index's
  checksum and the log, so no recompose is needed. A note's stamp is remembered by the file's
  size and mtime, so an unchanged note isn't read again.
- **`substrate writes check [--scope S] [--json]`**, under the log's lock:
  - decides each write a crash left with neither `done` nor `void`: `done` when the note holds the
    bytes the write named, or a later write of the note replaced exactly those bytes; `void` when
    the note still holds what it held before (or a new note is absent);
  - lists damaged lines, and `seq` gaps, repeats and reorderings;
  - lists the notes whose stamp the log contradicts, as the engine's reader reads the key, each
    once however many scopes inherit it;
  - counts refused writes and stamps with no engine write to compare, and names the scopes it
    skipped (not composed yet, or an index it can't read).

  **It exits 1 for one thing only:** a write it could not decide, because its note holds neither
  the bytes it had before nor the bytes the write named. Everything else is history, reported
  and exiting 0, since nothing may rewrite the log to clear it.
- The `list_scopes` server block names the log as `writes_log`, null only in agent mode, which
  reads no log.

### Changed

- **Agent-mode snapshots leave out every call unless the scope's entry opts in.** A
  conversation-class note is removed from the snapshot, not filtered, for every published scope,
  and `include_sources = true` under `[publish.<scope>]` in `agent-publish.toml` publishes them.
  Before, the guard removed a guarded vault's own notes and every other scope's calls were
  published. An entry with only `dir` keeps working, now without its calls, except for a scope
  that holds nothing but calls, which refuses to publish. Any value of `include_sources` other
  than `true` or `false` is refused. The snapshot manifest records the choice, and a caller asking
  for sources from a snapshot without them is told so in `filters`.
- A published snapshot that may hold calls its entry does not let in is removed before anything
  else is tried, on a failed refresh too. That includes every snapshot an earlier engine
  published, so after upgrading, a scope that does not opt in serves nothing until its next
  successful refresh. `publish-snapshot --dir` into a folder other than the entry's takes no calls.
- A snapshot's rowids are renumbered, so their gaps no longer show how many documents and
  passages were left out of it.
- **Wire 3.** `provenance` and `writes_log` are new reply keys, so `VERSION` is 3. `MIN_VERSION`
  stays 1, and older clients ignore the keys.

### Removed

- **The vault guard.** `guard_state` is no longer a manifest key, and a manifest that still
  declares it composes, as with any key the engine does not read. Nothing is withheld for it:
  the MCP server returns the vault's own notes through `search`, `documents` and `expand` whatever
  the state file says, and `ingest` and `amend` no longer refuse the vault.
- The refusal of a stamp key spelled another way (`Written_By`, `"amended_by"`). It was a denylist
  that two review rounds showed can't be complete. The stamp is a label, and the write log is the
  record. A stamp key written exactly as the engine writes it is still refused when its value is
  wrong.

## 0.2.0

### Fixed

- A YAML block list in frontmatter (`key:` then `  - item` lines, which Obsidian's Properties panel
  writes) no longer makes the reader drop the note's whole frontmatter. Before, the note lost its
  `status` and every other field, and `compose` refused it as having no status, which froze its
  whole scope.
- The reader now reads any frontmatter block that is not flat `key: value` lines through YAML
  (PyYAML, already a dependency, resolving no types). A flat block, which every engine and app
  writer emits, is read line by line exactly as before. So a block list, a comment, an indented
  key, a folded or quoted value over two lines all read as YAML reads them, to ingest, `amend`, a
  `_meta.md`, the skipped-file gate and `frontmatter check`.
- A value YAML reads that one line can't hold (a nested mapping, a multi-line scalar, a list of
  mappings), or that the line readers would read otherwise (an alias, a tag, a merge key `<<`, a
  key written twice), is left out. Under a key the engine reads, vault ingest refuses the note,
  and compose a `_meta.md` its passages inherit from, naming the key and the line. Before, an
  indented `key:` under another key was read as a top-level one, so `  status: archived` under
  `meta:` set the status.
- A block YAML can't read that names a key the engine reads is refused naming YAML's line. One that
  names none is body text, as a divider always was, and a vault note refused for its missing
  status now says why its block was not read.
- `frontmatter check` reports a value the engine leaves unread. Before, it called a note whose
  frontmatter the engine dropped clean.
- **Frontmatter ends only at a whole `---` line**, with nothing after it but spaces or tabs. A line
  that merely starts with `---` (`---x: 1`, `----`, `--- x`) is inside the block, as Obsidian
  reads it. Before, it ended the block for the engine, so the lines after it were body to the
  engine and properties to Obsidian, and a granted client could put a stamp key there unchecked.
  `--- ` still closes for the engine while Obsidian reads on past it, so that one fence still
  reads differently. `frontmatter check` and `fix` use the reader's own rule, so `fix` no longer
  refuses a `--- ` fence.
- A note whose block ended only at such a line now reads the longer block when a whole `---` line
  follows, and has no frontmatter when none does. If that block names a key the engine reads,
  vault ingest refuses the note and `frontmatter check` lists it, naming the line. Run `substrate
  frontmatter check` after upgrading: the older check uses the old fence and can't see them.

### Changed

- **Note format 2** (`notes.version`; `min_version` stays 1, since a flat block, the only shape a
  format-1 writer emits, reads exactly as before): a writer may use block lists and any YAML that
  holds one-line values.
- Newly refused on the vault path, each naming the file and the line: a note or `_meta.md` with
  such a value under a key the engine reads; a note whose non-flat block YAML can't read (or is
  over 1 MiB, or holds a NEL, LS or PS) and that names such a key; `amend` of a note whose
  unreadable value is its `doc_id`, `status`, `superseded_by` or class; and a navigation file
  (`MEMORY.md`, `log.md`, `structure.md`, `WRITING.md`) whose non-flat frontmatter declares a
  spine field, as the same file in flow form always was. A note that opens
  with a divider whose block YAML reads as a mapping is frontmatter now, as Obsidian shows it. Run
  `substrate frontmatter check` before upgrading to list them.

### Consented writes

- `substrate serve --writes=granted` runs `ingest` and `amend` on the shared socket for an MCP
  client the user has granted by name, and refuses every other. `substrate grant <client>
  [--scopes a,b]` issues (or rotates) the client's token, `substrate revoke <client>` withdraws
  it, and `substrate grants [--json]` lists who may write and who asked. The engine re-reads the
  grants on every write, so neither needs a restart. See `docs/security.md` for what a grant does
  and does not protect against.
- **`substrate-mcp --connect` forwards `ingest` and `amend` to the engine** with the client's name
  and token, and no longer writes in its own process. With no engine, or no grant, nothing is
  written. Its check that the bridge and the engine read the same registry is gone with it.
  A bridge from an older release (wire 1, engine 0.1.x, such as the one Scripta 0.3.5 bundles)
  still writes in its own process, ungated and unstamped, beside a granted engine, until the
  client's helper is switched to this release's bridge (`tools/dev-engine helper dev`).
- A granted write is stamped in the note's frontmatter, `written_by: <client>` on a new note and
  `amended_by: <client>` on an edit, before it is planned, so the plan's diff and checksum include
  it. A note declaring another value is refused. A plan is bound to the client's grant token, so
  a rotation, or a revoke and a new grant, leaves no old plan redeemable. The stamp is a label,
  not proof: anyone who can edit the file can change it (`docs/security.md`).
- After a write, an engine that refreshes runs a pass for it, and the reply says when the note
  will be found, instead of telling the reader to compose.
- `--client NAME` on `--connect` names the client a write is granted and stamped as, where two
  hosts would otherwise send the same `clientInfo.name`.
- `tools/dev-engine` runs a source tree as the shared engine (`start`, `stop`, `status`) and
  switches the `substrate-mcp` helper script MCP clients run between it and the app's bundled
  bridge (`helper dev|app`).

### Security

- **The MCP `ingest` tool no longer reads a file by path.** It takes the note as `content`, with a
  `filename`, both now required, and refuses `source_path` with "read the file yourself and pass
  its text as `content`". Before, a model could name any file the engine could read, such as an
  SSH private key, and a plan and a commit copied it into a vault that may sync. The `--connect`
  bridge refuses it too, so it never forwards one. A client that reads a file itself can still
  pass its text; that read is the client's, under its own permissions. `substrate ingest <file>`
  on the command line still takes a path.

### Contract

- `ingest`'s `source_path` argument is removed, and `content` and `filename` are required, with no
  wire bump: no socket client sends `source_path`. A wire-1 bridge never forwards a write, a
  wire-2 bridge sends only `content`, and Scripta calls no write tool over the socket.
- Wire version 2; the engine serves 1 to 2. New request headers (`Substrate-Client`,
  `Substrate-Grant`, `Substrate-Client-Info`) and write tools on the socket, which a wire-2 client
  relies on. `contract.json` gains `grants`.
- **The `server` block's `read_only` is redefined** as "runs no write without a grant": true for
  `writes` `none` and `granted`, false only for `open`. The new `writes` key says which. No wire-1
  client acts on the engine refusing writes, so the range still starts at 1.
- No index schema change, and no note format change beyond note format 2 above: the stamp is a
  frontmatter key the reader ignores.

## 0.1.0

The first release from this repository. The engine was developed inside
[Scripta](https://github.com/Ronan-Wood/Scripta) and split out with its history. This is the
version Scripta pins when it stops carrying its own copy.

It speaks **wire version 1** and **note format 1**, and builds **index schema 11**.
`uv run python -m substrate.cli version` prints the wire version and the schema.

### Vaults and scopes

- `compose` reads a project vault's `.substrate.toml`, resolves `inherits`, and indexes the vaults
  as one named scope. A missing inherited vault, a malformed manifest or an invalid spine fails the
  compose rather than narrowing the scope.
- The spine on every note: `status` (active, complete, archived, superseded), `doc_type`,
  `confidence` and `domains`. Archived and superseded notes are excluded by default, and a
  superseded note surfaces as the `supersedes` link on its replacement.
- Tiers from a note's location, document classes with their own chunk policies, and versioned
  references made current by `reference_pins`.
- The scope registry (`scopes.toml`) and `register`, which records a scope for the engine's next
  refresh pass to compose. By default an index lives in `~/.substrate/index/<scope>.db`.
- `status`, `digests` and `frontmatter check|fix`: what a scope holds, which digests are behind,
  and which notes' frontmatter isn't YAML or reads differently from the engine.

### Ingestion

- Markdown through the standard library. PDF, Office, HTML, EPUB, CSV, WebVTT, AsciiDoc, LaTeX,
  email and images through docling, the PDF text layer and Apple's on-device recognizer.
  `substrate formats` lists what is accepted and why the rest is refused.
- A content-addressed store for the originals behind derived notes (`substrate blobs`), with a
  plan-then-confirm `gc`.

### Retrieval

- Lexical search with SQLite FTS5, with nothing else installed.
- Optional local arms through Ollama on loopback: an embedder (`qwen3-embedding:0.6b`), whose
  results are fused with the lexical ones by RRF, HyDE query expansion, and listwise or
  cross-encoder reranking.
- Every reply states which arms ran, what was filtered out, the index version, and the last
  refresh. Conversations are withheld unless the caller includes sources.

### Serving

- An MCP server with the tools `search`, `expand`, `documents`, `list_scopes`, `status`, `refresh`,
  and the write tools `ingest` and `amend`, over stdio, a Unix socket or loopback HTTP.
- `substrate serve`: the shared engine on `~/.substrate/run/substrate.sock`, refreshing every
  registered scope on a timer, configured by `~/.substrate/serve.toml`.
- `substrate-mcp --connect`: a stdio bridge to the shared socket.
- Agent mode: chosen scopes served read-only to another local user, from published snapshots.

### Security

- Model requests go only to loopback, with no proxy and no redirects, and the connected peer is
  checked.
- The socket sits in a `0700` directory, and both ends check the other's uid. The socket and HTTP
  transports are read-only by default, and HTTP refuses foreign `Host`, any `Origin`, and
  non-JSON bodies.
- Everything the engine writes is private to its user, except agent-mode snapshots, which are
  readable by their folder's group by design. A vault can declare a guard, an opt-in
  check designed to withhold its notes from the MCP server unless the guarded app vouches for them.

### For clients

- `contract/contract.json` and the golden frames in `contract/frames/`, both generated and checked
  by the test suite.
- A versioned socket wire, served as a range from `MIN_VERSION` to `VERSION`.

### Platform

- macOS on Apple silicon, Python 3.14. The core test suite also runs on Ubuntu in CI.
- OpenCV is built from source without FFmpeg, so no GPL FFmpeg is bundled.
