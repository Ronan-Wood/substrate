import Quarantine.Model

/-!
# What the decision guarantees

The headline is `a_failed_note_is_never_indexed`: whenever the scope is indexed, no note that
failed is among the notes indexed. `every_passing_note_is_indexed` is the other half, so the
guarantee is not met by indexing nothing. `refuses_exactly_when` says the scope is refused for a
scope fault, a count over the ceiling, or no note left to index, and for nothing else. `the_notes_are_partitioned` says
every note is indexed or quarantined, never both and never neither.

The notes are numbered without repeats (`ids_nodup`): compose keys each note by its path, which
names one note in a scope.
-/

namespace Quarantine

def ids (w : World) : List Nat := w.notes.map Prod.fst

theorem verdict_index {w : World} {ix q : List Nat} (h : verdict w = .index ix q) :
    w.fault = false ∧ overCeiling w = false ∧ nothingLeft w = false ∧ ix = passed w ∧
      q = failed w := by
  unfold verdict at h
  split at h
  · cases h
  · rename_i hc
    cases h
    simp only [Bool.or_eq_true, not_or, Bool.not_eq_true] at hc
    exact ⟨hc.1.1, hc.1.2, hc.2, rfl, rfl⟩

theorem mem_failed {w : World} {k : Nat} : k ∈ failed w ↔ (k, true) ∈ w.notes := by
  unfold failed
  simp only [List.mem_map, List.mem_filter]
  constructor
  · rintro ⟨⟨a, b⟩, ⟨hm, hb⟩, rfl⟩
    simp at hb
    simpa [hb] using hm
  · intro hm
    exact ⟨(k, true), ⟨hm, rfl⟩, rfl⟩

theorem mem_passed {w : World} {k : Nat} : k ∈ passed w ↔ (k, false) ∈ w.notes := by
  unfold passed
  simp only [List.mem_map, List.mem_filter]
  constructor
  · rintro ⟨⟨a, b⟩, ⟨hm, hb⟩, rfl⟩
    simp at hb
    simpa [hb] using hm
  · intro hm
    exact ⟨(k, false), ⟨hm, rfl⟩, rfl⟩

theorem fst_unique : ∀ {l : List (Nat × Bool)}, (l.map Prod.fst).Nodup →
    ∀ {a b : Nat × Bool}, a ∈ l → b ∈ l → a.1 = b.1 → a = b
  | [], _, _, _, ha, _, _ => by cases ha
  | x :: xs, hn, a, b, ha, hb, he => by
    simp only [List.map_cons, List.nodup_cons, List.mem_map, not_exists, not_and] at hn
    rcases List.mem_cons.mp ha with rfl | ha'
    · rcases List.mem_cons.mp hb with rfl | hb'
      · rfl
      · exact absurd he.symm (hn.1 b hb')
    · rcases List.mem_cons.mp hb with rfl | hb'
      · exact absurd he (hn.1 a ha')
      · exact fst_unique hn.2 ha' hb' he

/-- With the notes numbered without repeats, a note cannot both pass and fail. -/
theorem not_both {w : World} (hn : (ids w).Nodup) {k : Nat}
    (ht : (k, true) ∈ w.notes) (hf : (k, false) ∈ w.notes) : False := by
  have := fst_unique hn ht hf rfl
  simp at this

/-- **No note that failed is indexed.** -/
theorem a_failed_note_is_never_indexed {w : World} {ix q : List Nat} (hn : (ids w).Nodup)
    (h : verdict w = .index ix q) {k : Nat} (hk : k ∈ failed w) : k ∉ ix := by
  obtain ⟨_, _, _, rfl, _⟩ := verdict_index h
  intro hp
  exact not_both hn (mem_failed.mp hk) (mem_passed.mp hp)

/-- Every note that passed is indexed, unless the scope is refused. -/
theorem every_passing_note_is_indexed {w : World} {ix q : List Nat}
    (h : verdict w = .index ix q) {k : Nat} (hk : k ∈ passed w) : k ∈ ix := by
  obtain ⟨_, _, _, rfl, _⟩ := verdict_index h
  exact hk

/-- What is quarantined is exactly what failed. -/
theorem quarantined_is_what_failed {w : World} {ix q : List Nat}
    (h : verdict w = .index ix q) : q = failed w := (verdict_index h).2.2.2.2

/-- Every note is indexed or quarantined, and none is both. -/
theorem the_notes_are_partitioned {w : World} {ix q : List Nat} (hn : (ids w).Nodup)
    (h : verdict w = .index ix q) (k : Nat) :
    (k ∈ ids w ↔ k ∈ ix ∨ k ∈ q) ∧ ¬ (k ∈ ix ∧ k ∈ q) := by
  obtain ⟨_, _, _, rfl, rfl⟩ := verdict_index h
  refine ⟨?_, fun ⟨hp, hf⟩ => not_both hn (mem_failed.mp hf) (mem_passed.mp hp)⟩
  rw [mem_passed, mem_failed]
  unfold ids
  simp only [List.mem_map]
  constructor
  · rintro ⟨⟨a, b⟩, hm, rfl⟩
    cases b
    · exact Or.inl hm
    · exact Or.inr hm
  · rintro (hm | hm)
    · exact ⟨_, hm, rfl⟩
    · exact ⟨_, hm, rfl⟩

/-- **The scope is refused exactly when it has a scope fault, or there is a ceiling and more
notes failed than it allows, or it has notes and none of them passed.** -/
theorem refuses_exactly_when (w : World) :
    verdict w = .refuse ↔
      (w.fault = true ∨ (∃ c, ceilingFor w.decl w.notes.length = some c ∧ c < (failed w).length) ∨
        (w.notes ≠ [] ∧ passed w = [])) := by
  have hc : overCeiling w = true ↔
      ∃ c, ceilingFor w.decl w.notes.length = some c ∧ c < (failed w).length := by
    unfold overCeiling
    split
    · rename_i c hcf
      simp [hcf]
    · rename_i hcf
      simp [hcf]
  have hn : nothingLeft w = true ↔ (w.notes ≠ [] ∧ passed w = []) := by
    unfold nothingLeft
    simp [List.isEmpty_iff]
  unfold verdict
  by_cases hv : (w.fault || overCeiling w || nothingLeft w) = true
  · simp only [hv, ↓reduceIte]
    simp only [Bool.or_eq_true] at hv
    refine ⟨fun _ => ?_, fun _ => trivial⟩
    rcases hv with (hf | ho) | hl
    · exact Or.inl hf
    · exact Or.inr (Or.inl (hc.mp ho))
    · exact Or.inr (Or.inr (hn.mp hl))
  · have hv' : (w.fault || overCeiling w || nothingLeft w) = false := by simpa using hv
    simp only [hv', Bool.false_eq_true, ↓reduceIte]
    simp only [Bool.or_eq_true, not_or] at hv
    constructor
    · intro h; cases h
    · rintro (hf | ho | hl)
      · exact absurd hf hv.1.1
      · exact absurd (hc.mpr ho) hv.1.2
      · exact absurd (hn.mpr hl) hv.2

/-- A scope fault refuses whatever the notes. -/
theorem a_scope_fault_refuses (w : World) (h : w.fault = true) : verdict w = .refuse :=
  (refuses_exactly_when w).mpr (Or.inl h)

/-- `quarantine_ceiling = 0` is the all-or-nothing compose of before: any failure refuses. -/
theorem a_ceiling_of_zero_refuses_any_failure (w : World) (hd : w.decl = .count 0)
    (hf : failed w ≠ []) : verdict w = .refuse := by
  refine (refuses_exactly_when w).mpr (Or.inr (Or.inl ⟨0, by simp [ceilingFor, hd], ?_⟩))
  cases h : failed w with
  | nil => exact absurd h hf
  | cons _ _ => simp

/-- `"none"` never refuses for the count: with no scope fault and one note passed, the scope is
indexed. -/
theorem no_ceiling_never_refuses_for_the_count (w : World) (hd : w.decl = .unlimited)
    (hf : w.fault = false) (hp : passed w ≠ []) : verdict w = .index (passed w) (failed w) := by
  have hl : nothingLeft w = false := by
    simp [nothingLeft, List.isEmpty_iff, hp]
  simp [verdict, overCeiling, ceilingFor, hd, hf, hl]

/-- **A compose that would index nothing refuses**, whatever the ceiling, `"none"` included. -/
theorem nothing_left_refuses (w : World) (hn : w.notes ≠ []) (hp : passed w = []) :
    verdict w = .refuse :=
  (refuses_exactly_when w).mpr (Or.inr (Or.inr ⟨hn, hp⟩))

/-- So an index of a scope that has notes is never empty. -/
theorem an_index_is_never_empty {w : World} {ix q : List Nat} (h : verdict w = .index ix q)
    (hn : w.notes ≠ []) : ix ≠ [] := by
  obtain ⟨_, _, hl, rfl, _⟩ := verdict_index h
  intro hp
  have : nothingLeft w = true := by simp [nothingLeft, List.isEmpty_iff, hn, hp]
  simp [this] at hl

/-- The default ceiling is at least 10, and never more than 1% of the scope once that is more
than 10. -/
theorem the_default_ceiling (n c : Nat) (h : ceilingFor .absent n = some c) :
    10 ≤ c ∧ (10 < c → c * 100 ≤ n) := by
  simp only [ceilingFor, Option.some.injEq] at h
  subst h
  constructor
  · exact Nat.le_max_left _ _
  · intro hlt
    have : max 10 (n / 100) = n / 100 := by omega
    rw [this]
    exact Nat.div_mul_le_self n 100

/-- Not vacuous: a scope with one failed note and one good one indexes the good one and
quarantines the other. -/
theorem a_quarantine_exists :
    verdict ⟨[(0, false), (1, true)], false, .absent⟩ = .index [0] [1] := by
  decide

/-- Not vacuous the other way: eleven failures in a small scope refuse by default. -/
theorem the_default_refuses_eleven :
    verdict ⟨(List.range 11).map (fun i => (i, true)), false, .absent⟩ = .refuse := by
  decide

end Quarantine

namespace Quarantine

/-- Not vacuous: every note failing refuses even with no ceiling. -/
theorem all_failing_refuses_with_no_ceiling :
    verdict ⟨[(0, true), (1, true)], false, .unlimited⟩ = .refuse := by
  decide

end Quarantine
