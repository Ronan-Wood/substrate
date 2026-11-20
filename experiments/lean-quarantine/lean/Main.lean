import Quarantine
import Lean.Data.Json

/-!
The model as a process. It reads one JSON world per line on stdin and writes one JSON result per
line on stdout. The harness keeps it running for the whole differential run.

Input: `{"notes": [[id, failed, doc_id, rank], ...], "fault": bool, "decl": "absent" | "none" | n}`.
Output: `{"refuse": true}` or `{"indexed": [...], "quarantined": [...], "copies": [...]}`.
-/

open Lean Quarantine

def noteOf (j : Json) : Except String Found := do
  match (← j.getArr?).toList with
  | [i, f, d, r] => pure ⟨← i.getNat?, ← f.getBool?, ← d.getNat?, ← r.getNat?⟩
  | _ => throw "note shape"

def declOf (j : Json) : Except String Decl :=
  match j.getStr? with
  | .ok "absent" => pure .absent
  | .ok "none" => pure .unlimited
  | .ok s => throw s!"decl {s}"
  | .error _ => do pure (.count (← j.getNat?))

def scopeOf (j : Json) : Except String Scope := do
  pure { found := ← (← (← j.getObjVal? "notes").getArr?).toList.mapM noteOf
         fault := ← (← j.getObjVal? "fault").getBool?
         decl := ← declOf (← j.getObjVal? "decl") }

def arr (l : List Nat) : Json := Json.arr (l.map (fun n : Nat => toJson n)).toArray

def resultJson : Result × List Nat → Json
  | (.refuse, _) => Json.mkObj [("refuse", Json.bool true)]
  | (.index ix q, c) => Json.mkObj [("indexed", arr ix), ("quarantined", arr q), ("copies", arr c)]

partial def loop (stdin : IO.FS.Stream) (stdout : IO.FS.Stream) : IO Unit := do
  let line ← stdin.getLine
  if line.isEmpty then return
  let out := match Json.parse line >>= scopeOf with
    | .ok s => resultJson (outcome s)
    | .error e => Json.mkObj [("error", Json.str e)]
  stdout.putStrLn out.compress
  stdout.flush
  loop stdin stdout

def main : IO Unit := do
  loop (← IO.getStdin) (← IO.getStdout)
