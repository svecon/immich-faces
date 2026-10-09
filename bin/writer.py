"""Every write to Immich goes through here.

Each change is appended to data/journal.jsonl *before* it is sent, carrying
the live value it replaces, and again with its outcome once verified. undo.py
replays the journal backwards. Immich has reported success without acting
(immich-av1 DEFECTS.md), so nothing is trusted until read back.

API shapes that are easy to get wrong, read from the v3.3 source:
  PUT  /faces/{PERSON id}  body {"id": FACE id}     -- path is the person
  POST /people/merge       body {"ids": [target, *sources]}
                           first defined name/birthday wins; also merges the
                           other users' people in the cluster group
  There is no endpoint that unassigns a face.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import threading
import time
from pathlib import Path

import lib

JOURNAL = lib.DATA / "journal.jsonl"
ATTEMPTS = 3
PERSON_FIELDS = ("name", "birthDate", "isHidden", "isFavorite")


class WriteError(RuntimeError):
    pass


class Writer:
    def __init__(self, dry_run: bool, journal: Path = JOURNAL):
        self.dry_run = dry_run
        self.journal = journal
        self._lock = threading.Lock()
        self._seq = 0
        if journal.exists():
            for line in journal.read_text().splitlines():
                if line.strip():
                    self._seq = max(self._seq, json.loads(line).get("seq", 0))

    # ------------------------------------------------------------------
    def _log(self, entry: dict) -> dict:
        with self._lock:
            if entry.get("status") == "intent":
                self._seq += 1
                entry["seq"] = self._seq
            entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            entry["dryRun"] = self.dry_run
            with self.journal.open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        return entry

    def _run(self, intent: dict, send, verify) -> dict:
        """Journal, send, read back; retry the whole send+verify on a no-op."""
        entry = self._log({**intent, "status": "intent"})
        if self.dry_run:
            self._log({"seq": entry["seq"], "status": "skipped-dry-run"})
            return entry
        last = None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                send()
                problem = verify()
            except Exception as e:
                problem = str(e)
            if not problem:
                self._log({"seq": entry["seq"], "status": "done", "attempts": attempt})
                return entry
            last = problem
            time.sleep(0.5 * attempt)
        self._log({"seq": entry["seq"], "status": "failed", "error": last})
        raise WriteError(f"{intent['op']} failed after {ATTEMPTS} attempts: {last}")

    # ------------------------------------------------------------------
    def get_person(self, person_id: str) -> dict:
        p = lib.api("GET", f"/people/{person_id}")
        return {k: p.get(k) for k in PERSON_FIELDS}

    def update_person(self, person_id: str, fields: dict, *, why: str = "",
                      ref: dict | None = None) -> dict | None:
        """Set name / birthDate / isHidden / isFavorite. Only sends what differs."""
        before = self.get_person(person_id) if not self.dry_run else {}
        change = {k: v for k, v in fields.items()
                  if k in PERSON_FIELDS and (self.dry_run or before.get(k) != v)}
        if not change:
            return None

        def verify():
            now = self.get_person(person_id)
            bad = {k: now.get(k) for k, v in change.items() if now.get(k) != v}
            return f"read back {bad}" if bad else ""

        return self._run(
            {"op": "person.update", "person": person_id, "before": before,
             "after": change, "why": why, "ref": ref},
            lambda: lib.api("PUT", f"/people/{person_id}", change), verify)

    def person_faces(self, person_id: str) -> list[dict]:
        """Every face of a person, as [{face, asset}], from the live server."""
        assets, page = [], 1
        while True:
            r = lib.api("POST", "/search/metadata",
                        {"personIds": [person_id], "size": 1000, "page": page,
                         "withDeleted": False})
            assets += [a["id"] for a in r["assets"]["items"]]
            nxt = r["assets"].get("nextPage")
            if not nxt:
                break
            page = int(nxt)
        def of(asset):
            return [{"face": f["id"], "asset": asset} for f in lib.api("GET", f"/faces?id={asset}")
                    if (f.get("person") or {}).get("id") == person_id]

        # One call per photo: 8,500 of them for the largest person, which took
        # minutes serially.
        with cf.ThreadPoolExecutor(8) as ex:
            return [f for found in ex.map(of, assets) for f in found]

    def merge(self, target: str, sources: list[str], *, why: str = "",
              ref: dict | None = None) -> dict | None:
        """Merge `sources` into `target`. Records every source face for undo."""
        sources = [s for s in sources if s != target]
        if not sources:
            return None
        before = {}
        if not self.dry_run:
            for s in sources:
                before[s] = {"person": self.get_person(s), "faces": self.person_faces(s)}

        def verify():
            left = []
            for s in sources:
                try:
                    lib.api("GET", f"/people/{s}")
                    left.append(s)
                except RuntimeError as e:
                    if "HTTP 4" not in str(e):
                        raise
            return f"still exists: {left}" if left else ""

        return self._run(
            {"op": "person.merge", "target": target, "sources": sources,
             "before": before, "why": why, "ref": ref},
            lambda: lib.api("POST", "/people/merge", {"ids": [target, *sources]}),
            verify)

    def face_person(self, face_id: str, asset_id: str) -> str | None:
        for f in lib.api("GET", f"/faces?id={asset_id}"):
            if f["id"] == face_id:
                return (f.get("person") or {}).get("id")
        raise WriteError(f"face {face_id} is no longer on asset {asset_id}")

    def reassign(self, face_id: str, asset_id: str, person_id: str, *, why: str = "",
                 ref: dict | None = None) -> dict | None:
        before = None if self.dry_run else self.face_person(face_id, asset_id)
        if before == person_id:
            return None

        def verify():
            now = self.face_person(face_id, asset_id)
            return "" if now == person_id else f"face is on {now}"

        return self._run(
            {"op": "face.reassign", "face": face_id, "asset": asset_id,
             "before": before, "after": person_id, "why": why, "ref": ref},
            lambda: lib.api("PUT", f"/faces/{person_id}", {"id": face_id}), verify)


    def create_person(self, fields: dict, *, why: str = "") -> str:
        """Create an empty person (used to rebuild one a merge removed)."""
        created = {}

        def send():
            created.update(lib.api("POST", "/people", fields))

        def verify():
            return "" if created.get("id") else "no id returned"

        entry = self._run({"op": "person.create", "fields": fields, "why": why}, send, verify)
        self._log({"seq": entry["seq"], "status": "created", "person": created.get("id")})
        return created.get("id")

    def mark_undone(self, seq: int, by: str) -> None:
        self._log({"seq": seq, "status": "undone", "by": by})


def read_journal(path: Path = JOURNAL) -> list[dict]:
    """Intent entries with their final status folded in, oldest first."""
    if not path.exists():
        return []
    entries: dict[int, dict] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if e.get("status") == "intent":
            entries[e["seq"]] = e
        elif e.get("seq") in entries and e["status"] == "created":
            entries[e["seq"]]["created"] = e.get("person")
        elif e.get("seq") in entries:
            entries[e["seq"]]["status"] = e["status"]
            if "error" in e:
                entries[e["seq"]]["error"] = e["error"]
    return [entries[k] for k in sorted(entries)]
