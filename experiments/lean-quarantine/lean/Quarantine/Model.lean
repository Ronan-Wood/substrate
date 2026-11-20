/-!
# The model of compose's quarantine decision

`verdict` is `substrate.quarantine.decide` with `ceiling_for` folded in: from each note's outcome,
whether the scope has a fault that is not one note's own, and the project manifest's declared
ceiling, to a refusal or the notes indexed and the notes quarantined. A scope with notes of which
none passed is refused whatever the ceiling (`nothingLeft`).

`outcome` puts the copies in front of it: of several notes deriving one `doc_id` from the same
bytes, only the first by path reaches `verdict` (`composed`), and the others are left out.

Simpler than the code in three ways, each checked by the differential rather than assumed:

* a note is a number, and its outcome is whether it failed. Why it failed is not modelled: every
  per-note failure is treated alike, and the harness writes each kind the code distinguishes;
* every scope fault is one bit. The code refuses most of them before any note is read
  (`vault.resolve_scope`) and the rest while ingesting, and refuses either way;
* a refusal carries no message.
-/

namespace Quarantine

/-- The project manifest's `quarantine_ceiling`: not declared, a count, or `"none"`. -/
inductive Decl where
  | absent
  | count (n : Nat)
  | unlimited
  deriving Repr, DecidableEq

structure World where
  /-- Each note, by number, and whether it failed on its own. -/
  notes : List (Nat × Bool)
  /-- Whether the scope has a fault that is not one note's own. -/
  fault : Bool
  decl : Decl
  deriving Repr

inductive Result where
  | refuse
  | index (indexed quarantined : List Nat)
  deriving Repr, DecidableEq

/-- The most notes a scope of `n` notes may quarantine and still be indexed, or `none` for no
limit. Without a declaration, the larger of 10 and 1% of `n`, rounded down. -/
def ceilingFor : Decl → Nat → Option Nat
  | .absent, n => some (max 10 (n / 100))
  | .count c, _ => some c
  | .unlimited, _ => none

def failed (w : World) : List Nat := (w.notes.filter (fun p => p.2)).map Prod.fst

def passed (w : World) : List Nat := (w.notes.filter (fun p => !p.2)).map Prod.fst

def overCeiling (w : World) : Bool :=
  match ceilingFor w.decl w.notes.length with
  | some c => decide (c < (failed w).length)
  | none => false

/-- Every note the scope discovered failed: nothing would be indexed. -/
def nothingLeft (w : World) : Bool := !w.notes.isEmpty && (passed w).isEmpty

def verdict (w : World) : Result :=
  if w.fault || overCeiling w || nothingLeft w then .refuse else .index (passed w) (failed w)

/-!
## Copies, before the decision

`vault.resolve_scope` composes the notes discovery finds, except that of several notes deriving
one `doc_id` from the same bytes only the first by path is composed; the rest are copies, left out
before any note is ingested, and named in `status`. `verdict` then decides over the notes composed.
Notes with the same bytes and their own names derive their own ids, so none of them is a copy.

A found note carries the `doc_id` it derives (`ident`, a number here) and its place in first-by-path
order (`rank`). Two notes that derive one id from DIFFERENT bytes, copies whose sources' `_meta.md`
are not the same bytes, and an id a note declares that another has, are scope faults, folded into
`fault` like the others.
-/

/-- A note as discovery finds it: its number, whether it fails on its own, the `doc_id` it derives,
and its place in first-by-path order. -/
structure Found where
  num : Nat
  failed : Bool
  ident : Nat
  rank : Nat
  deriving Repr, DecidableEq

structure Scope where
  found : List Found
  fault : Bool
  decl : Decl
  deriving Repr

/-- A note is composed when no note with its `doc_id` comes before it. -/
def kept (s : Scope) (f : Found) : Bool :=
  s.found.all (fun g => g.ident != f.ident || decide (f.rank ≤ g.rank))

/-- The world `verdict` decides: the notes composed, as numbers and outcomes. -/
def composed (s : Scope) : World :=
  { notes := (s.found.filter (kept s)).map (fun f => (f.num, f.failed)), fault := s.fault,
    decl := s.decl }

/-- The copies left out. -/
def copies (s : Scope) : List Nat := (s.found.filter (fun f => !kept s f)).map Found.num

/-- What a compose does: the decision over the notes composed, and the copies left out. -/
def outcome (s : Scope) : Result × List Nat := (verdict (composed s), copies s)

end Quarantine
