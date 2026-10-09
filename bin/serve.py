#!/usr/bin/env python3
"""Stage 2 -- the local web app: pair people, verify tags, review faces.

    python3 serve.py                      # data/after.db, writes to Immich
    python3 serve.py --dry-run            # everything journaled, nothing sent
    python3 serve.py --after data/after-sim.db   # simulated: always dry-run

Listens on 127.0.0.1 only. The API key never reaches the browser: images are
proxied (and cached under data/cache/) and every write goes through writer.py.

Decisions live in data/state.db, separate from the journal: a decision is
what you chose, the journal is what was sent. Reloading the page or
restarting the server loses neither.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import threading
import time
import traceback
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import analysis
import lib
import writer

WEB = Path(__file__).resolve().parent / "web"
CACHE = lib.DATA / "cache" / "preview"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
UUID = r"[0-9a-f-]{36}"


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------

class State:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS pairing (
        old TEXT PRIMARY KEY, status TEXT, targets TEXT, fields TEXT,
        error TEXT, ts TEXT);
    CREATE TABLE IF NOT EXISTS tag (
        tag TEXT PRIMARY KEY, person TEXT, trusted INTEGER, ts TEXT);
    CREATE TABLE IF NOT EXISTS face (
        face TEXT PRIMARY KEY, action TEXT, target TEXT, error TEXT, ts TEXT);
    CREATE TABLE IF NOT EXISTS featured_keep (
        person TEXT PRIMARY KEY, ts TEXT);
    """

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(self.SCHEMA)
        self.lock = threading.RLock()

    def now(self):
        return time.strftime("%Y-%m-%dT%H:%M:%S")

    def pairings(self) -> dict[str, dict]:
        with self.lock:
            return {r["old"]: {"status": r["status"], "targets": json.loads(r["targets"] or "[]"),
                               "fields": json.loads(r["fields"] or "{}"), "error": r["error"]}
                    for r in self.db.execute("SELECT * FROM pairing")}

    def set_pairing(self, old, status, targets=(), fields=None, error=None):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO pairing VALUES (?,?,?,?,?,?)",
                            (old, status, json.dumps(list(targets)), json.dumps(fields or {}),
                             error, self.now()))
            self.db.commit()

    def clear_pairing(self, old):
        with self.lock:
            self.db.execute("DELETE FROM pairing WHERE old = ?", (old,))
            self.db.commit()

    def tags(self) -> dict[str, dict]:
        with self.lock:
            return {r["tag"]: {"person": r["person"], "trusted": r["trusted"]}
                    for r in self.db.execute("SELECT * FROM tag")}

    def set_tag(self, tag, person, trusted):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO tag VALUES (?,?,?,?)",
                            (tag, person, trusted, self.now()))
            self.db.commit()

    def kept_featured(self) -> dict[str, str]:
        """People whose current featured photo you chose to keep."""
        with self.lock:
            return dict(self.db.execute("SELECT person, ts FROM featured_keep").fetchall())

    def keep_featured(self, person):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO featured_keep VALUES (?,?)", (person, self.now()))
            self.db.commit()

    def faces(self) -> dict[str, dict]:
        with self.lock:
            return {r["face"]: dict(r) for r in self.db.execute("SELECT * FROM face")}

    def set_face(self, face, action, target, error=None):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO face VALUES (?,?,?,?,?)",
                            (face, action, target, error, self.now()))
            self.db.commit()


# --------------------------------------------------------------------------
# The application
# --------------------------------------------------------------------------

class App:
    def __init__(self, before: Path, after: Path, dry_run: bool):
        self.model = analysis.Model(before, after)
        forced = bool(self.model.simulated)
        # A simulation keeps its own decisions and journal, so nothing chosen
        # against fabricated data can leak into the real run.
        suffix = "-sim" if forced else ""
        self.state = State(lib.DATA / f"state{suffix}.db")
        self.dry_run = dry_run or forced
        self.writer = writer.Writer(self.dry_run, lib.DATA / f"journal{suffix}.jsonl")
        self.sync_tag_names()
        self.write_lock = threading.Lock()
        self._replay_faces()
        lib.log(f"model: {len(self.model.faces)} faces, "
                f"after={'yes' if self.model.has_after else 'no'}"
                + (f" (SIMULATED: {self.model.simulated})" if forced else "")
                + (", DRY RUN" if self.dry_run else ", LIVE WRITES"))

    def _replay_faces(self):
        """Faces moved in earlier sessions: after.db predates them."""
        pairing = self.confirmed_pairing()
        for fid, d in self.state.faces().items():
            f = self.model.faces.get(fid)
            if f and d["action"] == "move" and not d["error"] and d["target"] in pairing:
                f.new = pairing[d["target"]][0]

    # -- pairings ------------------------------------------------------
    def confirmed_pairing(self) -> dict[str, list[str]]:
        """Old person -> clusters, for every pairing you applied."""
        return {old: d["targets"] for old, d in self.state.pairings().items()
                if d["status"] == "applied" and d["targets"]}

    def name_of(self, old: str) -> str:
        if old not in self.model.old:
            return ""
        return self.model.display_name(old) or "(unnamed)"

    def sync_tag_names(self):
        """Trusted tags on unnamed old clusters name them (see analysis)."""
        names = {}
        for tid, v in self.state.tags().items():
            p = self.model.old.get(v["person"] or "")
            if v["trusted"] == 1 and p and not p.name and tid in self.model.tags:
                names[p.id] = self.model.tags[tid]["value"][len(analysis.TAG_PREFIX):]
        self.model.tag_names = names

    def pairings(self):
        self.sync_tag_names()
        decided = self.state.pairings()
        props = self.model.proposals() if self.model.has_after else []
        for p in props:
            d = decided.get(p["old"]["id"])
            p["decision"] = d
            p["samples"] = {c["id"]: self.model.sample_faces(p["old"]["id"], c["id"], limit=8)
                            for c in p["clusters"][:6]}
        return {"pairings": props, "hasAfter": self.model.has_after}

    def apply_pairing(self, old: str, targets: list[str], fields: dict) -> dict:
        """Merge the targets into the first one, then restore the fields."""
        if not targets:
            raise ValueError("no clusters chosen")
        claimed = {c: o for o, cs in self.confirmed_pairing().items() for c in cs if o != old}
        clash = [self.name_of(claimed[c]) for c in targets if c in claimed]
        if clash:
            raise ValueError(f"cluster already paired with {', '.join(clash)}")
        with self.write_lock:
            # Checked again under the lock: a second click queued behind a slow
            # merge would otherwise try to merge clusters that no longer exist.
            if (self.state.pairings().get(old) or {}).get("status") == "applied":
                raise ValueError("already applied")
            try:
                primary, rest = targets[0], targets[1:]
                why = f"pairing {self.name_of(old)}"
                ref = {"pairing": old}
                if rest:
                    self.writer.merge(primary, rest, why=why, ref=ref)
                self.writer.update_person(primary, fields, why=why, ref=ref)
                self.state.set_pairing(old, "applied", targets, fields)
            except Exception as e:
                self.state.set_pairing(old, "failed", targets, fields, str(e))
                raise
        return self.state.pairings()[old]

    # -- tags ----------------------------------------------------------
    def tags(self):
        verdicts = self.state.tags()
        out = []
        for t in self.model.people_tags():
            v = verdicts.get(t["id"])
            cands = self.model.tag_candidates(t["id"])
            person = v["person"] if v else next((c["id"] for c in cands if c.get("suggested")), None)
            out.append({"id": t["id"], "value": t["value"],
                        "name": t["value"][len(analysis.TAG_PREFIX):],
                        "candidates": cands, "person": person,
                        "personName": self.name_of(person) if person else None,
                        "verdict": v, "stats": self.model.tag_stats(t["id"], person)})
        out.sort(key=lambda t: (t["verdict"] is not None, -t["stats"]["tagged"]))
        return {"tags": out}

    def tag_detail(self, tag_id: str, person: str | None):
        st = self.model.tag_stats(tag_id, person)
        ex = {}
        if person:
            for kind in ("tagOnly", "faceOnly", "both"):
                ex[kind] = self.label_faces(self.model.tag_examples(tag_id, person, kind))
        return {"stats": st, "examples": ex}

    def trusted_tags(self) -> dict[str, str]:
        return {t: v["person"] for t, v in self.state.tags().items()
                if v["trusted"] == 1 and v["person"]}

    # -- review --------------------------------------------------------
    def label_faces(self, faces: list[dict]) -> list[dict]:
        """Attach readable names for the old and new person of each face."""
        pairing = self.confirmed_pairing()
        identity = {c: old for old, cs in pairing.items() for c in cs}
        trusted = self.trusted_tags()
        done = self.state.faces()
        for f in faces:
            f["oldName"] = self.name_of(f["old"]) if f["old"] else "unassigned"
            if not f["new"]:
                f["newName"] = "unassigned"
            elif f["new"] in identity:
                f["newName"] = self.name_of(identity[f["new"]])
            else:
                n = len(self.model.new_faces.get(f["new"], ()))
                f["newName"] = f"unnamed cluster ({n})"
            f["tags"] = [self.name_of(p) for p in self.model.tag_opinion(f["asset"], trusted)]
            f["done"] = done.get(f["id"])
        return faces

    def review(self):
        pairing = self.confirmed_pairing()
        groups = self.model.review(pairing)
        done = self.state.faces()
        out = []
        for old, g in groups.items():
            todo = lambda fs: sum(1 for f in fs if f.id not in done)
            out.append({"id": old, "name": self.name_of(old),
                        "left": len(g["left"]), "joined": len(g["joined"]),
                        "todo": todo(g["left"]) + todo(g["joined"])})
        out.sort(key=lambda r: (r["todo"] == 0, -r["todo"]))
        return {"people": out, "paired": len(pairing)}

    def review_person(self, old: str):
        g = self.model.review(self.confirmed_pairing()).get(old, {"left": [], "joined": []})
        return {"id": old, "name": self.name_of(old),
                "left": self.label_faces([self.model.face_json(f) for f in g["left"]]),
                "joined": self.label_faces([self.model.face_json(f) for f in g["joined"]])}

    def decide_faces(self, decisions: list[dict]) -> dict:
        """[{face, action, person}]. move: reassign to `person`'s primary
        cluster. keep: accept the face where it is now."""
        pairing = self.confirmed_pairing()
        results = {"ok": 0, "failed": []}
        with self.write_lock:
            for d in decisions:
                fid, action, person = d["face"], d["action"], d.get("person")
                f = self.model.faces.get(fid)
                if f is None:
                    continue
                if action == "keep":
                    self.state.set_face(fid, "keep", f.new)
                    results["ok"] += 1
                    continue
                if person not in pairing:
                    results["failed"].append({"face": fid, "error": "person not paired"})
                    continue
                target = pairing[person][0]
                try:
                    self.writer.reassign(fid, f.asset, target,
                                         why=f"review {self.name_of(person)}",
                                         ref={"face": fid})
                    f.new = target
                    self.state.set_face(fid, "move", person)
                    results["ok"] += 1
                except Exception as e:
                    self.state.set_face(fid, "move", person, str(e))
                    results["failed"].append({"face": fid, "error": str(e)})
        return results

    # -- featured photo --------------------------------------------------
    def featured(self):
        pairing = self.confirmed_pairing()
        # Done = a photo was set (journal) or the current one kept (state.db).
        done = {e["person"]: e["ts"] for e in writer.read_journal(self.writer.journal)
                if e["op"] == "person.featured" and e["status"] in ("done", "skipped-dry-run")}
        done.update(self.state.kept_featured())
        out = [{"id": old, "name": self.name_of(old), "person": cs[0], "set": done.get(cs[0])}
               for old, cs in pairing.items()]
        out.sort(key=lambda p: (p["set"] is not None, p["name"] == "(unnamed)",
                                p["name"].casefold()))
        return {"people": out}

    def _live_assets(self, person: str) -> dict[str, dict]:
        """This person's photos as Immich has them now: {asset id: date, type}."""
        cache = self.__dict__.setdefault("_asset_cache", {})
        if person not in cache:
            found, page = {}, 1
            while True:
                r = lib.api("POST", "/search/metadata",
                            {"personIds": [person], "size": 1000, "page": page})
                for a in r["assets"]["items"]:
                    found[a["id"]] = {"date": (a.get("localDateTime") or a.get("fileCreatedAt") or "")[:10],
                                      "type": a.get("type"), "visibility": a.get("visibility")}
                nxt = r["assets"].get("nextPage")
                if not nxt:
                    break
                page = int(nxt)
            cache[person] = found
        return cache[person]

    def featured_detail(self, old: str):
        """Every face of this person on a timeline photo, with what scoring needs.

        Scoring itself is in the page, so the weight slider re-ranks instantly.
        """
        pairing = self.confirmed_pairing()
        if old not in pairing:
            raise ValueError("person has no applied pairing")
        person = pairing[old][0]
        live = self._live_assets(person)
        clusters = set(pairing[old])
        faces = []
        for f in self.model.faces.values():
            a = live.get(f.asset)
            if not a or f.new not in clusters or a["visibility"] != "timeline" or not a["date"]:
                continue
            h = (f.box[3] - f.box[1]) / f.size[1] if f.size[1] else 0
            faces.append({**self.model.face_json(f), "date": a["date"], "type": a["type"],
                          "height": round(h, 4)})
        p = self.model.old[old]
        return {"id": old, "name": self.name_of(old), "person": person,
                "birthDate": p.birth or None, "faces": faces}

    def set_featured(self, old: str, asset: str):
        person = self.confirmed_pairing()[old][0]
        with self.write_lock:
            self.writer.set_featured(person, asset, why=f"featured {self.name_of(old)}")
        return {"ok": True}

    def people(self):
        pairing = self.confirmed_pairing()
        return {"people": sorted(({"id": o, "name": self.name_of(o),
                                   "paired": o in pairing}
                                  for o in {p.id for p in self.model.relevant_old()}),
                                 key=lambda p: p["name"].casefold())}

    def status(self):
        jr = writer.read_journal(self.writer.journal)
        return {"simulated": self.model.simulated, "dryRun": self.dry_run,
                "hasAfter": self.model.has_after, "server": lib.BASE_URL,
                "journal": {"entries": len(jr),
                            "failed": sum(1 for e in jr if e.get("status") == "failed")}}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def fetch_preview(asset_id: str) -> tuple[bytes, str]:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / asset_id
    if not path.exists():
        req = urllib.request.Request(
            f"{lib.BASE_URL}/api/assets/{asset_id}/thumbnail?size=preview",
            headers={"x-api-key": lib.api_key()})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.rename(path)
    data = path.read_bytes()
    kind = ("image/webp" if data[:4] == b"RIFF" else
            "image/png" if data[:4] == b"\x89PNG" else "image/jpeg")
    return data, kind


class Handler(BaseHTTPRequestHandler):
    app: App = None  # set in main()

    def log_message(self, fmt, *args):
        if not str(args[1] if len(args) > 1 else "").startswith(("2", "3")):
            lib.log("http " + fmt % args)

    def _send(self, code, body: bytes, kind: str, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _route(self, method: str):
        url = urlparse(self.path)
        p, q = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
        app = self.app

        if method == "GET":
            if p in STATIC:
                name, kind = STATIC[p]
                return self._send(200, (WEB / name).read_bytes(), kind)
            if m := re.fullmatch(rf"/img/({UUID})", p):
                data, kind = fetch_preview(m[1])
                return self._send(200, data, kind, cache=True)
            if m := re.fullmatch(rf"/thumb/({UUID})", p):
                f = lib.DATA / "thumbs" / "before" / f"{m[1]}.jpg"
                if f.exists():
                    return self._send(200, f.read_bytes(), "image/jpeg", cache=True)
                return self._send(404, b"", "text/plain")
            routes = {
                "/api/status": app.status, "/api/pairings": app.pairings,
                "/api/tags": app.tags, "/api/review": app.review, "/api/people": app.people,
                "/api/featured": app.featured,
            }
            if p in routes:
                return self._json(routes[p]())
            if m := re.fullmatch(rf"/api/tags/({UUID})", p):
                return self._json(app.tag_detail(m[1], q.get("person")))
            if m := re.fullmatch(rf"/api/review/({UUID})", p):
                return self._json(app.review_person(m[1]))
            if m := re.fullmatch(rf"/api/featured/({UUID})", p):
                return self._json(app.featured_detail(m[1]))
            if m := re.fullmatch(rf"/live-thumb/({UUID})", p):
                # The current thumbnail, never cached: it changes when set.
                data = lib.api("GET", f"/people/{m[1]}/thumbnail", raw=True)
                return self._send(200, data, "image/jpeg")

        if method == "POST":
            b = self._body()
            if m := re.fullmatch(rf"/api/pairings/({UUID})", p):
                if b["action"] == "apply":
                    return self._json(app.apply_pairing(m[1], b["targets"], b["fields"]))
                if b["action"] == "skip":
                    app.state.set_pairing(m[1], "skipped")
                    return self._json({"status": "skipped"})
                if b["action"] == "undecide":
                    if (app.state.pairings().get(m[1]) or {}).get("status") == "applied":
                        raise ValueError("already applied -- use undo.py to reverse it")
                    app.state.clear_pairing(m[1])
                    return self._json({})
            if m := re.fullmatch(rf"/api/tags/({UUID})", p):
                app.state.set_tag(m[1], b.get("person"), b.get("trusted"))
                app.sync_tag_names()
                return self._json({})
            if m := re.fullmatch(rf"/api/featured/({UUID})", p):
                if b.get("keep"):
                    app.state.keep_featured(app.confirmed_pairing()[m[1]][0])
                    return self._json({"ok": True})
                return self._json(app.set_featured(m[1], b["asset"]))
            if p == "/api/faces":
                return self._json(app.decide_faces(b["decisions"]))

        self._json({"error": f"no route {method} {p}"}, 404)

    def _handle(self, method):
        try:
            self._route(method)
        except ValueError as e:     # a refused decision, not a crash
            self._json({"error": str(e)}, 400)
        except Exception as e:
            lib.log(f"error on {method} {self.path}: {e}")
            traceback.print_exc()
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", type=Path, default=lib.DATA / "before.db")
    ap.add_argument("--after", type=Path, default=lib.DATA / "after.db")
    ap.add_argument("--dry-run", action="store_true",
                    help="journal everything, send nothing to Immich")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    if not args.before.exists():
        lib.die(f"{args.before} missing -- run 01_import.py --label before")
    if not args.after.exists():
        lib.log(f"{args.after} not found: only the tag view will have data")
    app = App(args.before, args.after, args.dry_run)
    if not app.dry_run and app.model.has_after and not lib.ml_idle():
        lib.die("ML queues are busy -- faces are still moving; wait, or use --dry-run")
    Handler.app = app
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    lib.log(f"open http://127.0.0.1:{args.port}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
