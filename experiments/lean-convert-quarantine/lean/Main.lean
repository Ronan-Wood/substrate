import ConvertQuarantine
import Lean.Data.Json

/-!
The model as a process. It reads one JSON case per line on stdin and writes one JSON result per
line on stdout. The harness keeps it running for the whole differential run.

Input: `{"outcomes": ["hang", "ok", ...], "record": "absent" | "matching" | "other",
"retry": bool}`, in the code's own spellings. Output: `{"verdict": ..., "record": "keep" | "write"
| "delete"}`, or `{"reject": true}` where the code raises `ValueError`: for a sequence the rule
rejects, and for an outcome or record state the code does not know, which the reader rejects
because the model's types cannot hold one.
-/

open Lean ConvertQuarantine

def outcomeOf : String → Option Outcome
  | "hang" => some (.fault .hang)
  | "crash" => some (.fault .crash)
  | "protocol" => some (.fault .protocol)
  | "ok" => some (.answer .ok)
  | "refused" => some (.answer .refused)
  | "needs_extra" => some (.answer .needsExtra)
  | "unsupported" => some (.answer .unsupported)
  | "changed" => some (.answer .changed)
  | _ => none

def recordOf : String → Option Record
  | "absent" => some .absent
  | "matching" => some .matching
  | "other" => some .other
  | _ => none

def verdictName : Verdict → String
  | .blocked => "blocked"
  | .attempt => "attempt"
  | .converted => "converted"
  | .refused => "refused"
  | .needsExtra => "needs_extra"
  | .unsupported => "unsupported"
  | .changed => "changed"
  | .quarantined => "quarantined"

def actionName : Action → String
  | .keep => "keep"
  | .write => "write"
  | .delete => "delete"

def answer (j : Json) : Except String Json := do
  let os ← (← (← j.getObjVal? "outcomes").getArr?).toList.mapM (·.getStr?)
  let r ← (← j.getObjVal? "record").getStr?
  let b ← (← j.getObjVal? "retry").getBool?
  let reject := Json.mkObj [("reject", Json.bool true)]
  match os.mapM outcomeOf, recordOf r with
  | some os, some r =>
    match quarantineDecision os r b with
    | some d => pure (Json.mkObj [("verdict", Json.str (verdictName d.verdict)),
                                  ("record", Json.str (actionName d.record))])
    | none => pure reject
  | _, _ => pure reject

partial def loop (stdin : IO.FS.Stream) (stdout : IO.FS.Stream) : IO Unit := do
  let line ← stdin.getLine
  if line.isEmpty then return
  let out := match Json.parse line >>= answer with
    | .ok o => o
    | .error e => Json.mkObj [("error", Json.str e)]
  stdout.putStrLn out.compress
  stdout.flush
  loop stdin stdout

def main : IO Unit := do
  loop (← IO.getStdin) (← IO.getStdout)
