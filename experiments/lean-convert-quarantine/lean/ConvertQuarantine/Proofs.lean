import ConvertQuarantine.Model

/-!
# What the rule guarantees

The headline is `withheld_only_two_ways`: a file is blocked or quarantined only with no attempt
made, a matching record and no retry asked for, or after `retries + 1` faults in a row.
`blocked_iff` and `quarantined_iff` are its two halves as equivalences, so neither way is met by
withholding nothing. `accepts_iff_reached` says the rule decides exactly the sequences a driver
can hand it, and rejects the rest.

Every theorem but the last two is about `decideWith retries` for any `retries`; the last two are
`quarantineDecision`, at the code's `RETRIES`. The first eight are lemmas the others use.
-/

namespace ConvertQuarantine

theorem answersLast_snoc (xs : List Outcome) (o : Outcome) :
    answersLast (xs ++ [o]) = xs.all Outcome.isFault := by
  induction xs with
  | nil => rfl
  | cons x xs ih =>
    have h : (xs ++ [o]).isEmpty = false := by cases xs <;> rfl
    rw [List.cons_append]
    simp only [answersLast, ih, h, List.all_cons]
    simp [Bool.and_comm]

theorem decide_nil (retries : Nat) (r : Record) (b : Bool) :
    decideWith retries [] r b =
      if blocks r b then some ⟨.blocked, .keep⟩ else some ⟨.attempt, .keep⟩ := by
  unfold decideWith
  by_cases h : blocks r b <;> simp [h, answersLast]

theorem decide_snoc (retries : Nat) (xs : List Outcome) (o : Outcome) (r : Record) (b : Bool) :
    decideWith retries (xs ++ [o]) r b =
      if !(xs.all Outcome.isFault) || retries < xs.length || blocks r b then none
      else match o with
        | .answer a => some ⟨a.verdict, if a = .ok ∧ r ≠ .absent then .delete else .keep⟩
        | .fault _ =>
            if xs.length + 1 ≤ retries then some ⟨.attempt, .keep⟩
            else some ⟨.quarantined, .write⟩ := by
  unfold decideWith
  rw [answersLast_snoc, List.getLast?_concat]
  by_cases ha : xs.all Outcome.isFault <;> by_cases hl : retries < xs.length <;>
    by_cases hb : blocks r b <;> cases o <;> simp_all <;> omega

theorem blocks_iff {r : Record} {b : Bool} : blocks r b = true ↔ r = .matching ∧ b = false := by
  cases r <;> cases b <;> simp [blocks]

theorem nil_or_snoc (os : List Outcome) : os = [] ∨ ∃ xs o, os = xs ++ [o] := by
  rcases List.eq_nil_or_concat os with h | ⟨xs, o, h⟩
  · exact Or.inl h
  · exact Or.inr ⟨xs, o, by simpa using h⟩

theorem all_snoc {xs : List Outcome} {o : Outcome} :
    (∀ x ∈ xs ++ [o], x.isFault = true) ↔ (∀ x ∈ xs, x.isFault = true) ∧ o.isFault = true := by
  simp [or_imp, forall_and]

/-- The full shape of a decision on a non-empty sequence: rejected, or decided by its last
outcome. -/
theorem snoc_cases {retries : Nat} {xs : List Outcome} {o : Outcome} {r : Record} {b : Bool}
    {d : Decision} (h : decideWith retries (xs ++ [o]) r b = some d) :
    (∀ x ∈ xs, x.isFault = true) ∧ xs.length ≤ retries ∧ blocks r b = false ∧
      ((∃ a, o = .answer a ∧
          d = ⟨a.verdict, if a = .ok ∧ r ≠ .absent then .delete else .keep⟩) ∨
        (∃ f, o = .fault f ∧ xs.length + 1 ≤ retries ∧ d = ⟨.attempt, .keep⟩) ∨
        (∃ f, o = .fault f ∧ retries < xs.length + 1 ∧ d = ⟨.quarantined, .write⟩)) := by
  rw [decide_snoc] at h
  by_cases ha : xs.all Outcome.isFault <;> by_cases hl : retries < xs.length <;>
    by_cases hb : blocks r b <;> simp [ha, hl, hb] at h
  refine ⟨by simpa using ha, by omega, by simpa using hb, ?_⟩
  cases o with
  | answer a => exact Or.inl ⟨a, rfl, (Option.some.inj h).symm⟩
  | fault f =>
    by_cases hr : xs.length + 1 ≤ retries
    · simp [hr] at h; exact Or.inr (Or.inl ⟨f, rfl, hr, h.symm⟩)
    · simp [hr] at h; exact Or.inr (Or.inr ⟨f, rfl, by omega, h.symm⟩)

/-- The converse: a sequence of that shape is decided so. -/
theorem snoc_decided {retries : Nat} {xs : List Outcome} {o : Outcome} {r : Record} {b : Bool}
    (hf : ∀ x ∈ xs, x.isFault = true) (hl : xs.length ≤ retries) (hb : blocks r b = false) :
    decideWith retries (xs ++ [o]) r b =
      match o with
      | .answer a => some ⟨a.verdict, if a = .ok ∧ r ≠ .absent then .delete else .keep⟩
      | .fault _ =>
          if xs.length + 1 ≤ retries then some ⟨.attempt, .keep⟩
          else some ⟨.quarantined, .write⟩ := by
  have ha : xs.all Outcome.isFault = true := by simpa using hf
  have hl' : ¬ retries < xs.length := by omega
  rw [decide_snoc]
  simp [ha, hl', hb]

/-! ## A file is withheld in exactly two ways -/

/-- **Blocked exactly when no attempt has been made, the record is this converter's, and no
retry was asked for.** -/
theorem blocked_iff (retries : Nat) (os : List Outcome) (r : Record) (b : Bool) :
    (∃ a, decideWith retries os r b = some ⟨.blocked, a⟩) ↔
      os = [] ∧ r = .matching ∧ b = false := by
  rcases nil_or_snoc os with rfl | ⟨xs, o, rfl⟩
  · rw [decide_nil, ← blocks_iff]
    by_cases hb : blocks r b <;> simp [hb]
  · constructor
    · rintro ⟨a, h⟩
      obtain ⟨-, -, -, ⟨a', -, hd⟩ | ⟨-, -, -, hd⟩ | ⟨-, -, -, hd⟩⟩ := snoc_cases h
      · cases a' <;> simp [Answer.verdict] at hd
      · simp at hd
      · simp at hd
    · rintro ⟨h, -⟩; simp at h

/-- **Quarantined exactly at `retries + 1` faults in a row**, on a file the record does not
block. -/
theorem quarantined_iff (retries : Nat) (os : List Outcome) (r : Record) (b : Bool) :
    (∃ a, decideWith retries os r b = some ⟨.quarantined, a⟩) ↔
      os.length = retries + 1 ∧ (∀ o ∈ os, o.isFault = true) ∧ ¬ (r = .matching ∧ b = false) := by
  rcases nil_or_snoc os with rfl | ⟨xs, o, rfl⟩
  · rw [decide_nil]
    by_cases hb : blocks r b <;> simp [hb]
  · rw [all_snoc, ← blocks_iff]
    constructor
    · rintro ⟨a, h⟩
      obtain ⟨hf, hl, hb, ⟨a', -, hd⟩ | ⟨-, -, -, hd⟩ | ⟨f, rfl, hr, -⟩⟩ := snoc_cases h
      · cases a' <;> simp [Answer.verdict] at hd
      · simp at hd
      · exact ⟨by simp; omega, ⟨hf, rfl⟩, by simp [hb]⟩
    · rintro ⟨hl, ⟨hf, ho⟩, hb⟩
      simp at hl hb
      cases o with
      | answer a => simp [Outcome.isFault] at ho
      | fault f =>
        refine ⟨.write, ?_⟩
        rw [snoc_decided hf (by omega) (by simpa using hb)]
        simp; omega

/-- **The headline: a file is withheld only in the two documented ways.** -/
theorem withheld_only_two_ways {retries : Nat} {os : List Outcome} {r : Record} {b : Bool}
    {d : Decision} (h : decideWith retries os r b = some d)
    (hw : d.verdict = .blocked ∨ d.verdict = .quarantined) :
    (os = [] ∧ r = .matching ∧ b = false) ∨
      (os.length = retries + 1 ∧ ∀ o ∈ os, o.isFault = true) := by
  obtain ⟨v, a⟩ := d
  simp at hw
  rcases hw with rfl | rfl
  · exact Or.inl ((blocked_iff retries os r b).mp ⟨a, h⟩)
  · obtain ⟨hl, hf, -⟩ := (quarantined_iff retries os r b).mp ⟨a, h⟩
    exact Or.inr ⟨hl, hf⟩

/-! ## Docling's answer is final -/

/-- **An answer ends the run with its own verdict: it is never retried, never quarantines, and
never blocks.** -/
theorem an_answer_is_final {retries : Nat} {xs : List Outcome} {a : Answer} {r : Record}
    {b : Bool} {d : Decision} (h : decideWith retries (xs ++ [.answer a]) r b = some d) :
    d.verdict = a.verdict ∧ d.verdict ≠ .attempt ∧ d.verdict ≠ .quarantined ∧
      d.verdict ≠ .blocked := by
  obtain ⟨-, -, -, ⟨a', ha, rfl⟩ | ⟨_, hf, -⟩ | ⟨_, hf, -⟩⟩ := snoc_cases h
  · cases ha; cases a <;> simp [Answer.verdict]
  · cases hf
  · cases hf

/-- **No sequence holding an answer anywhere quarantines.** -/
theorem an_answer_never_quarantines {retries : Nat} {os : List Outcome} {a : Answer}
    {r : Record} {b : Bool} {d : Decision} (ha : .answer a ∈ os)
    (h : decideWith retries os r b = some d) : d.verdict ≠ .quarantined := by
  intro hq
  obtain ⟨v, act⟩ := d
  simp at hq
  subst hq
  obtain ⟨-, hf, -⟩ := (quarantined_iff retries os r b).mp ⟨act, h⟩
  simpa [Outcome.isFault] using hf _ ha

/-- **Converted exactly when the last attempt went through: a fault never yields a
conversion.** -/
theorem converted_iff {retries : Nat} {os : List Outcome} {r : Record} {b : Bool} {d : Decision}
    (h : decideWith retries os r b = some d) :
    d.verdict = .converted ↔ ∃ xs, os = xs ++ [.answer .ok] := by
  rcases nil_or_snoc os with rfl | ⟨xs, o, rfl⟩
  · rw [decide_nil] at h
    by_cases hb : blocks r b <;> simp [hb] at h <;> subst h <;> simp
  · obtain ⟨-, -, -, ⟨a, rfl, rfl⟩ | ⟨f, rfl, -, rfl⟩ | ⟨f, rfl, -, rfl⟩⟩ := snoc_cases h
    · cases a <;> simp [Answer.verdict]
    · simp
    · simp

/-! ## The record -/

/-- **A record is written exactly when the file is quarantined.** -/
theorem write_iff {retries : Nat} {os : List Outcome} {r : Record} {b : Bool} {d : Decision}
    (h : decideWith retries os r b = some d) : d.record = .write ↔ d.verdict = .quarantined := by
  rcases nil_or_snoc os with rfl | ⟨xs, o, rfl⟩
  · rw [decide_nil] at h
    by_cases hb : blocks r b <;> simp [hb] at h <;> subst h <;> simp
  · obtain ⟨-, -, -, ⟨a, rfl, rfl⟩ | ⟨f, rfl, -, rfl⟩ | ⟨f, rfl, -, rfl⟩⟩ := snoc_cases h
    · cases a <;> cases r <;> simp [Answer.verdict]
    · simp
    · simp

/-- **A record is deleted exactly when a conversion went through and there was one.** -/
theorem delete_iff {retries : Nat} {os : List Outcome} {r : Record} {b : Bool} {d : Decision}
    (h : decideWith retries os r b = some d) :
    d.record = .delete ↔ d.verdict = .converted ∧ r ≠ .absent := by
  rcases nil_or_snoc os with rfl | ⟨xs, o, rfl⟩
  · rw [decide_nil] at h
    by_cases hb : blocks r b <;> simp [hb] at h <;> subst h <;> simp
  · obtain ⟨-, -, -, ⟨a, rfl, rfl⟩ | ⟨f, rfl, -, rfl⟩ | ⟨f, rfl, -, rfl⟩⟩ := snoc_cases h
    · cases a <;> cases r <;> simp [Answer.verdict]
    · simp
    · simp

/-! ## `--retry-quarantined` and the record state -/

/-- **The flag matters only for a matching record.** -/
theorem retry_matters_only_for_a_matching_record (retries : Nat) (os : List Outcome) {r : Record}
    (hr : r ≠ .matching) (b b' : Bool) : decideWith retries os r b = decideWith retries os r b' := by
  have hb : ∀ c, blocks r c = false := fun c => by cases r <;> simp_all [blocks]
  unfold decideWith
  simp [hb]

/-- **With the flag, a matching record is decided as another converter's record is**: on every
sequence, and whether or not that other run asked for a retry. -/
theorem retry_treats_a_matching_record_as_another_s (retries : Nat) (os : List Outcome)
    (b : Bool) : decideWith retries os .matching true = decideWith retries os .other b := by
  unfold decideWith
  simp [blocks]

/-- **Without the flag, a matching record admits no attempt**: the only sequence decided is the
empty one, and it is blocked. -/
theorem without_retry_a_matching_record_is_never_attempted (retries : Nat) (os : List Outcome) :
    (decideWith retries os .matching false).isSome ↔ os = [] := by
  rcases nil_or_snoc os with rfl | ⟨xs, o, rfl⟩
  · simp [decide_nil, blocks]
  · simp only [List.append_eq_nil_iff, List.cons_ne_self, and_false, iff_false]
    cases h : decideWith retries (xs ++ [o]) .matching false with
    | none => simp
    | some d => obtain ⟨-, -, hb, -⟩ := snoc_cases h; simp [blocks] at hb

/-- **Another converter's record never blocks**, and neither does no record. -/
theorem only_a_matching_record_blocks {retries : Nat} {os : List Outcome} {r : Record} {b : Bool}
    {d : Decision} (hr : r ≠ .matching) (h : decideWith retries os r b = some d) :
    d.verdict ≠ .blocked := by
  intro hv
  obtain ⟨v, a⟩ := d
  simp at hv
  subst hv
  exact hr ((blocked_iff retries os r b).mp ⟨a, h⟩).2.1

/-! ## Which sequences the rule decides -/

/-- An `attempt` is made only on faults, at most `retries` of them, on a file not blocked, and
it keeps the record. -/
theorem attempt_iff (retries : Nat) (os : List Outcome) (r : Record) (b : Bool) :
    decideWith retries os r b = some ⟨.attempt, .keep⟩ ↔
      (∀ o ∈ os, o.isFault = true) ∧ os.length ≤ retries ∧ ¬ (r = .matching ∧ b = false) := by
  rw [← blocks_iff]
  rcases nil_or_snoc os with rfl | ⟨xs, o, rfl⟩
  · rw [decide_nil]
    by_cases hb : blocks r b <;> simp [hb]
  · rw [all_snoc]
    constructor
    · intro h
      obtain ⟨hf, -, hb, ⟨a, -, hd⟩ | ⟨f, rfl, hr, -⟩ | ⟨-, -, -, hd⟩⟩ := snoc_cases h
      · cases a <;> simp [Answer.verdict] at hd
      · exact ⟨⟨hf, rfl⟩, by simp; omega, by simp [hb]⟩
      · simp at hd
    · rintro ⟨⟨hf, ho⟩, hl, hb⟩
      simp at hl hb
      cases o with
      | answer a => simp [Outcome.isFault] at ho
      | fault f =>
        rw [snoc_decided hf (by omega) (by simpa using hb)]
        simp; omega

/-- **The rule decides exactly**: the empty sequence, and faults followed by one more outcome,
with at most `retries` faults before it, on a file the record does not block. Everything else
is rejected, as the code raises `ValueError`. -/
theorem accepts_iff (retries : Nat) (os : List Outcome) (r : Record) (b : Bool) :
    (decideWith retries os r b).isSome ↔
      os = [] ∨ ∃ xs o, os = xs ++ [o] ∧ (∀ x ∈ xs, x.isFault = true) ∧ xs.length ≤ retries ∧
        ¬ (r = .matching ∧ b = false) := by
  rw [← blocks_iff]
  rcases nil_or_snoc os with rfl | ⟨xs, o, rfl⟩
  · rw [decide_nil]; by_cases hb : blocks r b <;> simp [hb]
  · constructor
    · intro h
      cases hd : decideWith retries (xs ++ [o]) r b with
      | none => simp [hd] at h
      | some d =>
        obtain ⟨hf, hl, hb, -⟩ := snoc_cases hd
        exact Or.inr ⟨xs, o, rfl, hf, hl, by simp [hb]⟩
    · rintro (h | ⟨ys, p, he, hf, hl, hb⟩)
      · simp at h
      · obtain ⟨rfl, hp⟩ := List.append_inj' he rfl
        simp only [List.cons.injEq, and_true] at hp
        subst hp
        rw [snoc_decided hf hl (by simpa using hb)]
        cases o with
        | answer a => simp
        | fault f => by_cases hr : xs.length + 1 ≤ retries <;> simp [hr]

/-- **The rule decides exactly the sequences a driver can hand it**, a driver being a loop that
asks the rule before the first attempt and after each one, and attempts again only on
`attempt` (`Reached`). So it is total on what a driver produces and rejects the rest. -/
theorem accepts_iff_reached (retries : Nat) (os : List Outcome) (r : Record) (b : Bool) :
    (decideWith retries os r b).isSome ↔ Reached retries r b os := by
  constructor
  · intro h
    suffices ∀ n (os : List Outcome), os.length = n → (decideWith retries os r b).isSome →
        Reached retries r b os from this _ os rfl h
    intro n
    induction n with
    | zero => intro os hl _; rw [List.length_eq_zero_iff.mp hl]; exact .start
    | succ n ih =>
      intro os hl h
      rcases (accepts_iff retries os r b).mp h with h0 | ⟨ys, p, rfl, hf, hl', hb⟩
      · subst h0; simp at hl
      · have ha := (attempt_iff retries ys r b).mpr ⟨hf, hl', hb⟩
        exact .step p (ih ys (by simp at hl; omega) (by simp [ha])) ha
  · intro h
    induction h with
    | start => rw [decide_nil]; split <;> simp
    | step o _ ha _ =>
      obtain ⟨hf, hl, hb⟩ := (attempt_iff retries _ r b).mp ha
      exact (accepts_iff retries _ r b).mpr (Or.inr ⟨_, o, rfl, hf, hl, hb⟩)

/-- **A driver starts at most `retries + 1` workers**: no sequence it reaches is longer. -/
theorem a_run_is_bounded {retries : Nat} {r : Record} {b : Bool} {os : List Outcome}
    (h : Reached retries r b os) : os.length ≤ retries + 1 := by
  cases h with
  | start => simp
  | step o _ ha =>
    have := ((attempt_iff retries _ r b).mp ha).2.1
    simp; omega

/-! ## At the code's `RETRIES` -/

/-- **A first fault is retried.** -/
theorem a_first_fault_is_retried (f : Fault) {r : Record} {b : Bool}
    (hb : ¬ (r = .matching ∧ b = false)) :
    quarantineDecision [.fault f] r b = some ⟨.attempt, .keep⟩ :=
  (attempt_iff RETRIES _ r b).mpr ⟨by simp [Outcome.isFault], by simp [RETRIES], hb⟩

/-- **A second fault in a row quarantines, and writes the record.** -/
theorem a_second_fault_quarantines (f g : Fault) {r : Record} {b : Bool}
    (hb : ¬ (r = .matching ∧ b = false)) :
    quarantineDecision [.fault f, .fault g] r b = some ⟨.quarantined, .write⟩ := by
  have hb' : blocks r b = false := by
    cases h : blocks r b
    · rfl
    · exact absurd (blocks_iff.mp h) hb
  have := snoc_decided (retries := RETRIES) (xs := [.fault f]) (o := .fault g)
    (by simp [Outcome.isFault]) (by simp [RETRIES]) hb'
  simpa [quarantineDecision, RETRIES] using this

end ConvertQuarantine
