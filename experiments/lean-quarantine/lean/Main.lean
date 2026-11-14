import Quarantine
import Lean.Data.Json

/-!
The model as a process. It reads one JSON world per line on stdin and writes one JSON result per
line on stdout. The harness keeps it running for the whole differential run.

Input: `{"notes": [[id, failed], ...], "fault": bool, "decl": "absent" | "none" | n}`.
Output: `{"refuse": true}` or `{"indexed": [...], "quarantined": [...]}`.
-/

open Lean Quarantine

def noteOf (j : Json) : Except String (Nat × Bool) := do
  match (← j.getArr?).toList with
  | [i, f] => pure (← i.getNat?, ← f.getBool?)
  | _ => throw "note shape"

def declOf (j : Json) : Except String Decl :=
  match j.getStr? with
  | .ok "absent" => pure .absent
  | .ok "none" => pure .unlimited
  | .ok s => throw s!"decl {s}"
  | .error _ => do pure (.count (← j.getNat?))

def worldOf (j : Json) : Except String World := do
  pure { notes := ← (← (← j.getObjVal? "notes").getArr?).toList.mapM noteOf
         fault := ← (← j.getObjVal? "fault").getBool?
         decl := ← declOf (← j.getObjVal? "decl") }

def arr (l : List Nat) : Json := Json.arr (l.map (fun n : Nat => toJson n)).toArray

def resultJson : Result → Json
  | .refuse => Json.mkObj [("refuse", Json.bool true)]
  | .index ix q => Json.mkObj [("indexed", arr ix), ("quarantined", arr q)]

partial def loop (stdin : IO.FS.Stream) (stdout : IO.FS.Stream) : IO Unit := do
  let line ← stdin.getLine
  if line.isEmpty then return
  let out := match Json.parse line >>= worldOf with
    | .ok w => resultJson (verdict w)
    | .error e => Json.mkObj [("error", Json.str e)]
  stdout.putStrLn out.compress
  stdout.flush
  loop stdin stdout

def main : IO Unit := do
  loop (← IO.getStdin) (← IO.getStdout)
