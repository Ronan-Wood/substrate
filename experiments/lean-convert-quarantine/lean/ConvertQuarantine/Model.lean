/-!
# The model of the conversion quarantine rule

`decideWith retries os r retry` is `quarantine_decision` in
`substrate/extract/conversion_quarantine.py`, with its `RETRIES` as a parameter: from the outcomes
of the attempts so far, the record state for the file and `--retry-quarantined`, to a decision, or
`none` where the code raises `ValueError` for a sequence no driver produces. `quarantineDecision`
is it at the code's `RETRIES`, and it is what the harness runs.

Simpler than the code in two ways, each checked by the differential rather than assumed:

* outcomes and record states are types, so a string the code does not know cannot be written
  here. The model's JSON reader rejects one, as the code raises for it;
* a rejection carries no message.
-/

namespace ConvertQuarantine

/-- What a worker can do wrong: a FAULT. The engine kills it and may try again. -/
inductive Fault where
  | hang
  | crash
  | protocol
  deriving Repr, DecidableEq

/-- How an attempt ends with docling's own answer, or with the file changed under it. -/
inductive Answer where
  | ok
  | refused
  | needsExtra
  | unsupported
  | changed
  deriving Repr, DecidableEq

inductive Outcome where
  | fault (f : Fault)
  | answer (a : Answer)
  deriving Repr, DecidableEq

/-- The record for this file as the decision sees it: none, this converter's, another's. -/
inductive Record where
  | absent
  | matching
  | other
  deriving Repr, DecidableEq

inductive Verdict where
  | blocked
  | attempt
  | converted
  | refused
  | needsExtra
  | unsupported
  | changed
  | quarantined
  deriving Repr, DecidableEq

/-- What becomes of the record: kept as it is, written with the faults so far, or deleted. -/
inductive Action where
  | keep
  | write
  | delete
  deriving Repr, DecidableEq

structure Decision where
  verdict : Verdict
  record : Action
  deriving Repr, DecidableEq

/-- The code's `RETRIES`: attempts after the first. -/
def RETRIES : Nat := 1

def Outcome.isFault : Outcome → Bool
  | .fault _ => true
  | .answer _ => false

def Answer.verdict : Answer → Verdict
  | .ok => .converted
  | .refused => .refused
  | .needsExtra => .needsExtra
  | .unsupported => .unsupported
  | .changed => .changed

/-- No outcome follows an answer: every outcome but the last is a fault. -/
def answersLast : List Outcome → Bool
  | [] => true
  | o :: os => (os.isEmpty || o.isFault) && answersLast os

/-- A matching record blocks the file unless the operator asked to retry it. -/
def blocks (r : Record) (retry : Bool) : Bool := r == .matching && !retry

def decideWith (retries : Nat) (os : List Outcome) (r : Record) (retry : Bool) :
    Option Decision :=
  if !answersLast os || retries + 1 < os.length then none
  else if blocks r retry then (if os.isEmpty then some ⟨.blocked, .keep⟩ else none)
  else match os.getLast? with
    | none => some ⟨.attempt, .keep⟩
    | some (.answer a) =>
        some ⟨a.verdict, if a = .ok ∧ r ≠ .absent then .delete else .keep⟩
    | some (.fault _) =>
        if os.length ≤ retries then some ⟨.attempt, .keep⟩ else some ⟨.quarantined, .write⟩

/-- The rule as the code runs it. -/
def quarantineDecision : List Outcome → Record → Bool → Option Decision := decideWith RETRIES

/-!
## The driver, abstractly

`through_worker` asks the rule before the first attempt and after each one, and starts a worker
only on `attempt`, whose outcome may be anything. `Reached` is every sequence such a loop can hand
the rule, for one record state and flag: the empty one, and any one an `attempt` extends.
-/

inductive Reached (retries : Nat) (r : Record) (retry : Bool) : List Outcome → Prop
  | start : Reached retries r retry []
  | step {os : List Outcome} (o : Outcome) : Reached retries r retry os →
      decideWith retries os r retry = some ⟨.attempt, .keep⟩ → Reached retries r retry (os ++ [o])

end ConvertQuarantine
