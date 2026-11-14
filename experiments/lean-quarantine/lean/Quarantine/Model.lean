/-!
# The model of compose's quarantine decision

`verdict` is `substrate.quarantine.decide` with `ceiling_for` folded in: from each note's outcome,
whether the scope has a fault that is not one note's own, and the project manifest's declared
ceiling, to a refusal or the notes indexed and the notes quarantined. A scope with notes of which
none passed is refused whatever the ceiling (`nothingLeft`).

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

end Quarantine
