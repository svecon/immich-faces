#!/usr/bin/env python3
"""Stage 3 -- reverse writes made by serve.py, newest first.

    python3 undo.py --list                 # what the journal holds
    python3 undo.py --last 5               # plan: reverse the last 5 writes
    python3 undo.py --last 5 --yes         # ...and do it
    python3 undo.py --seq 12 --seq 14 --yes
    python3 undo.py --pairing <old person id> --yes   # everything one pairing did

How each write reverses (see writer.py for why):
  person.update   the previous name / birthday / flags are written back
  face.reassign   the face goes back to its previous person -- unless it had
                  none: Immich cannot un-assign a face, so that one is reported
  person.merge    each merged-away person is created again, with its old name
                  and flags, and its recorded faces are moved back onto it.
                  The recreated person has a NEW id.
  person.create   not reversed (an empty person is harmless; delete in the UI)

Undo writes go through the same journal, so an undo can itself be undone.
The decision in state.db is cleared too, so the web app offers it again.
"""

import argparse
import sqlite3

import lib
import writer


def describe(e: dict) -> str:
    op = e["op"]
    if op == "person.update":
        return f"person {e['person'][:8]}: {e['before']} -> {e['after']}"
    if op == "face.reassign":
        return f"face {e['face'][:8]}: {str(e['before'])[:8]} -> {e['after'][:8]}"
    if op == "person.merge":
        n = sum(len(v["faces"]) for v in (e.get("before") or {}).values())
        return f"merge {len(e['sources'])} person(s), {n} faces, into {e['target'][:8]}"
    if op == "person.create":
        return f"create {e.get('created', '?')}"
    return op


def reverse(w: writer.Writer, e: dict) -> str:
    why = f"undo #{e['seq']}"
    op = e["op"]
    if op == "person.update":
        w.update_person(e["person"], {k: e["before"].get(k) for k in e["after"]}, why=why)
        return "restored"
    if op == "face.reassign":
        if not e["before"]:
            return "SKIPPED: the face was unassigned before, and Immich cannot un-assign"
        w.reassign(e["face"], e["asset"], e["before"], why=why)
        return "moved back"
    if op == "person.merge":
        out = []
        for src, b in (e.get("before") or {}).items():
            fields = {k: v for k, v in b["person"].items() if v not in (None, "")}
            new = w.create_person(fields, why=why)
            for f in b["faces"]:
                w.reassign(f["face"], f["asset"], new, why=why)
            out.append(f"{src[:8]} recreated as {new} with {len(b['faces'])} faces")
        return "; ".join(out)
    return "SKIPPED: not reversible"


def clear_decision(e: dict, state_path) -> None:
    ref = e.get("ref") or {}
    if not ref or not state_path.exists():
        return
    db = sqlite3.connect(state_path)
    if "pairing" in ref:
        db.execute("DELETE FROM pairing WHERE old = ?", (ref["pairing"],))
    if "face" in ref:
        db.execute("DELETE FROM face WHERE face = ?", (ref["face"],))
    db.commit()
    db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default=str(writer.JOURNAL))
    ap.add_argument("--state", default=str(lib.DATA / "state.db"))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--last", type=int)
    ap.add_argument("--seq", type=int, action="append", default=[])
    ap.add_argument("--pairing", help="old person id: undo everything its pairing did")
    ap.add_argument("--yes", action="store_true", help="actually write; default is a plan")
    args = ap.parse_args()

    from pathlib import Path
    journal, state = Path(args.journal), Path(args.state)
    entries = writer.read_journal(journal)

    if args.list:
        for e in entries:
            print(f"#{e['seq']:<5} {e['status']:16} {e['ts']}  {describe(e)}  [{e.get('why', '')}]")
        return

    done = [e for e in entries if e["status"] == "done" and not e.get("dryRun")
            and not str(e.get("why", "")).startswith("undo")]
    if args.seq:
        pick = [e for e in done if e["seq"] in args.seq]
    elif args.pairing:
        pick = [e for e in done if (e.get("ref") or {}).get("pairing") == args.pairing]
    elif args.last:
        pick = done[-args.last:]
    else:
        ap.error("choose --list, --last N, --seq N or --pairing ID")
    if not pick:
        lib.die("nothing to undo (only completed, live, not-yet-undone writes qualify)")

    pick.sort(key=lambda e: -e["seq"])
    for e in pick:
        print(f"#{e['seq']:<5} {describe(e)}")
    if not args.yes:
        print(f"\n{len(pick)} write(s) would be reversed. Re-run with --yes.")
        return
    if not lib.ml_idle():
        lib.die("ML queues are busy; wait until they are idle")

    w = writer.Writer(dry_run=False, journal=journal)
    for e in pick:
        try:
            result = reverse(w, e)
            if not result.startswith("SKIPPED"):
                w.mark_undone(e["seq"], "undo.py")
                clear_decision(e, state)
            lib.log(f"#{e['seq']}: {result}")
        except Exception as ex:
            lib.log(f"#{e['seq']}: FAILED {ex}")
    lib.log("restart serve.py so the page reloads its state")


if __name__ == "__main__":
    main()
