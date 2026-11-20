import Quarantine.Model
import Quarantine.Proofs

/-!
# What leaving out copies guarantees

`every_passing_note_is_indexed` (in `Proofs`) is about the notes `verdict` decides, which are the
notes composed. A copy is a note that passed and is not among them, so the guarantee for every note
discovery finds is `every_passing_note_is_indexed_or_a_copy`: a note that passed is indexed, or it
is a copy and a note composed in its place, indexed or quarantined, derives the same `doc_id`.
With `a_copy_is_never_indexed_or_quarantined`, `one_note_per_doc_id` and
`without_shared_ids_no_copy`, that is the whole rule: a copy is left out and not quarantined,
exactly one note is decided for each id, and notes with ids of their own are each decided as before.

`every_passing_note_is_indexed_or_its_twin_is` is the stronger statement, that the note composed
in a passing copy's place is indexed. It ASSUMES a copy fails exactly when that note does
(`twins`). The code makes that likely rather than true: two notes are copies only when their bytes
and their sources' `_meta.md` bytes are the same, but a failure that depends on the path alone
(a name with nothing visible titles no note) can still tell them apart. Then the copy is left out
and its note is quarantined, and compose and `status` say neither is indexed.

The found notes are numbered without repeats, and their ranks are too (`nums`, `ranks`): compose
keys a note by its path, and first-by-path order is total on paths.
-/

namespace Quarantine

def nums (s : Scope) : List Nat := s.found.map Found.num

def ranks (s : Scope) : List Nat := s.found.map Found.rank

theorem map_inj_of_nodup {α : Type} (key : α → Nat) : ∀ {l : List α}, (l.map key).Nodup →
    ∀ {a b : α}, a ∈ l → b ∈ l → key a = key b → a = b
  | [], _, _, _, ha, _, _ => by cases ha
  | x :: xs, hn, a, b, ha, hb, he => by
    simp only [List.map_cons, List.nodup_cons, List.mem_map, not_exists, not_and] at hn
    rcases List.mem_cons.mp ha with rfl | ha'
    · rcases List.mem_cons.mp hb with rfl | hb'
      · rfl
      · exact absurd he.symm (hn.1 b hb')
    · rcases List.mem_cons.mp hb with rfl | hb'
      · exact absurd he (hn.1 a ha')
      · exact map_inj_of_nodup key hn.2 ha' hb' he

theorem kept_iff {s : Scope} {f : Found} :
    kept s f = true ↔ ∀ g ∈ s.found, g.ident = f.ident → f.rank ≤ g.rank := by
  unfold kept
  simp only [List.all_eq_true, Bool.or_eq_true, bne_iff_ne, ne_eq, decide_eq_true_eq]
  constructor
  · intro h g hg he
    rcases h g hg with h' | h'
    · exact absurd he h'
    · exact h'
  · intro h g hg
    by_cases he : g.ident = f.ident
    · exact Or.inr (h g hg he)
    · exact Or.inl he

theorem mem_composed {s : Scope} {k : Nat} {b : Bool} :
    (k, b) ∈ (composed s).notes ↔ ∃ f ∈ s.found, kept s f = true ∧ f.num = k ∧ f.failed = b := by
  unfold composed
  simp only [List.mem_map, List.mem_filter, Prod.mk.injEq]
  constructor
  · rintro ⟨f, ⟨hf, hk⟩, h1, h2⟩
    exact ⟨f, hf, hk, h1, h2⟩
  · rintro ⟨f, hf, hk, h1, h2⟩
    exact ⟨f, ⟨hf, hk⟩, h1, h2⟩

/-- A note left out has a note composed before it with the same `doc_id`. -/
theorem a_copy_has_its_twin_composed {s : Scope} :
    ∀ (n : Nat) (f : Found), f.rank = n → f ∈ s.found → kept s f = false →
      ∃ g ∈ s.found, kept s g = true ∧ g.ident = f.ident ∧ g.rank < f.rank := by
  intro n
  induction n using Nat.strongRecOn with
  | ind n ih =>
    intro f hr hf hk
    have : ∃ g ∈ s.found, g.ident = f.ident ∧ g.rank < f.rank := by
      apply Classical.byContradiction
      intro hne
      have hkt : kept s f = true := kept_iff.mpr (fun g hg he =>
        Nat.le_of_not_lt (fun hlt => hne ⟨g, hg, he, hlt⟩))
      simp [hkt] at hk
    obtain ⟨g, hg, he, hlt⟩ := this
    cases hkg : kept s g
    · obtain ⟨h, hh, hkh, heh, hlh⟩ := ih g.rank (hr ▸ hlt) g rfl hg hkg
      exact ⟨h, hh, hkh, heh.trans he, Nat.lt_trans hlh hlt⟩
    · exact ⟨g, hg, hkg, he, hlt⟩

/-- **A copy is never indexed, and never quarantined.** -/
theorem a_copy_is_never_indexed_or_quarantined {s : Scope} {ix q : List Nat}
    (hn : (nums s).Nodup) (h : verdict (composed s) = .index ix q) {f : Found}
    (hf : f ∈ s.found) (hk : kept s f = false) : f.num ∉ ix ∧ f.num ∉ q := by
  obtain ⟨_, _, _, rfl, rfl⟩ := verdict_index h
  have hnot : ∀ b, (f.num, b) ∉ (composed s).notes := by
    intro b hm
    obtain ⟨g, hg, hkg, hgn, _⟩ := mem_composed.mp hm
    have := map_inj_of_nodup Found.num hn hg hf hgn
    subst this
    simp [hkg] at hk
  exact ⟨fun hp => hnot false (mem_passed.mp hp), fun hq => hnot true (mem_failed.mp hq)⟩

/-- **Exactly one note answers for each `doc_id`**: of the notes composed, no two share one. -/
theorem one_note_per_doc_id {s : Scope} (hr : (ranks s).Nodup) {f g : Found}
    (hf : f ∈ s.found) (hg : g ∈ s.found) (hkf : kept s f = true) (hkg : kept s g = true)
    (he : f.ident = g.ident) : f = g := by
  have h1 := kept_iff.mp hkf g hg he.symm
  have h2 := kept_iff.mp hkg f hf he
  exact map_inj_of_nodup Found.rank hr hf hg (Nat.le_antisymm h1 h2)

/-- **Every note that passed is indexed, or is a copy and a note composed in its place derives the
same `doc_id`**, unless the scope is refused. That note is indexed or quarantined. -/
theorem every_passing_note_is_indexed_or_a_copy {s : Scope} {ix q : List Nat}
    (h : verdict (composed s) = .index ix q) {f : Found} (hf : f ∈ s.found)
    (hp : f.failed = false) :
    f.num ∈ ix ∨ (kept s f = false ∧
      ∃ g ∈ s.found, kept s g = true ∧ g.ident = f.ident ∧ (g.num ∈ ix ∨ g.num ∈ q)) := by
  obtain ⟨_, _, _, hix, hq⟩ := verdict_index h
  cases hk : kept s f
  · obtain ⟨g, hg, hkg, he, _⟩ := a_copy_has_its_twin_composed f.rank f rfl hf hk
    refine Or.inr ⟨rfl, g, hg, hkg, he, ?_⟩
    cases hgf : g.failed
    · exact Or.inl (hix ▸ mem_passed.mpr (mem_composed.mpr ⟨g, hg, hkg, rfl, hgf⟩))
    · exact Or.inr (hq ▸ mem_failed.mpr (mem_composed.mpr ⟨g, hg, hkg, rfl, hgf⟩))
  · exact Or.inl (hix ▸ mem_passed.mpr (mem_composed.mpr ⟨f, hf, hk, rfl, hp⟩))

/-- **The stronger statement, under an assumption the code does not guarantee** (see above): when
a copy fails exactly when the note composed in its place does (`twins`), every note that passed is
indexed or is a copy of a note that is. -/
theorem every_passing_note_is_indexed_or_its_twin_is {s : Scope} {ix q : List Nat}
    (twins : ∀ f ∈ s.found, ∀ g ∈ s.found, f.ident = g.ident → f.failed = g.failed)
    (h : verdict (composed s) = .index ix q) {f : Found} (hf : f ∈ s.found)
    (hp : f.failed = false) :
    f.num ∈ ix ∨ ∃ g ∈ s.found, kept s g = true ∧ g.ident = f.ident ∧ g.num ∈ ix := by
  have hix : ∀ g ∈ s.found, kept s g = true → g.failed = false → g.num ∈ ix := by
    intro g hg hkg hpg
    exact every_passing_note_is_indexed h (mem_passed.mpr (mem_composed.mpr ⟨g, hg, hkg, rfl, hpg⟩))
  cases hk : kept s f
  · obtain ⟨g, hg, hkg, he, _⟩ := a_copy_has_its_twin_composed f.rank f rfl hf hk
    exact Or.inr ⟨g, hg, hkg, he, hix g hg hkg ((twins g hg f hf he).trans hp)⟩
  · exact Or.inl (hix f hf hk hp)

/-- **Notes whose `doc_id`s are their own are each decided as before**: none is a copy. Notes with
the same bytes under their own names are this case. -/
theorem without_shared_ids_no_copy {s : Scope} (hi : (s.found.map Found.ident).Nodup) :
    copies s = [] ∧ (composed s).notes = s.found.map (fun f => (f.num, f.failed)) := by
  have hall : ∀ f ∈ s.found, kept s f = true := by
    intro f hf
    refine kept_iff.mpr (fun g hg he => ?_)
    rw [map_inj_of_nodup Found.ident hi hg hf he]
    exact Nat.le_refl _
  constructor
  · unfold copies
    rw [List.filter_eq_nil_iff.mpr (fun f hf => by simp [hall f hf])]
    rfl
  · unfold composed
    simp only
    rw [List.filter_eq_self.mpr (fun f hf => hall f hf)]

/-- Not vacuous: two notes with one `doc_id`, and the one first by path is indexed. -/
theorem a_copy_is_left_out :
    outcome ⟨[⟨0, false, 7, 1⟩, ⟨1, false, 7, 0⟩, ⟨2, false, 8, 2⟩], false, .absent⟩ =
      (.index [1, 2] [], [0]) := by
  decide

/-- Not vacuous: a copy of a failing note is left out, and the note it copies is quarantined. -/
theorem a_copy_of_a_failure_is_not_quarantined :
    outcome ⟨[⟨0, true, 7, 1⟩, ⟨1, true, 7, 0⟩, ⟨2, false, 8, 2⟩], false, .absent⟩ =
      (.index [2] [1], [0]) := by
  decide

end Quarantine
