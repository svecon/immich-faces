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
from collections import Counter, defaultdict
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
CLUSTER_MIN_FACES = 3

# Missing faces (PLAN.md stage 4).
ROUND_FIRST = 50        # photos per round until a tag has this many checked
ROUND_MAX = 100
STOP_WRONG = 3          # suggest stopping a tag at this many wrong...
STOP_SHARE = 0.05       # ...or this share of the checked photos
SUGGEST_SCORE = 0.4     # cluster-to-person similarity to preselect a face
DETECT_MAX_SCORE = 0.5  # detection runs only while the threshold is at most this
DETECT_NORMAL = 0.4     # the score Immich is normally left at; below it shows a banner
DETECT_FIRST = 5        # the first photos ever refreshed go one at a time
DETECT_BATCH = 100
DETECT_CANARY = 5       # stop if this many photos gained no face at all
DETECT_TIMEOUT = 3600   # seconds to wait for the queues per batch


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
    -- Missing faces: one row per tag and photo looked at. verdict is face,
    -- wrong, cant, notdetected (queued for detection), undetectable, present
    -- (the person's face was already there) or NULL (refreshed, to review).
    CREATE TABLE IF NOT EXISTS missing (
        tag TEXT, asset TEXT, verdict TEXT, face TEXT, refreshed INTEGER DEFAULT 0,
        added TEXT, error TEXT, ts TEXT, PRIMARY KEY (tag, asset));
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

    def missing(self, tag=None) -> dict[tuple[str, str], dict]:
        with self.lock:
            q, args = "SELECT * FROM missing", ()
            if tag:
                q, args = q + " WHERE tag = ?", (tag,)
            return {(r["tag"], r["asset"]): dict(r) for r in self.db.execute(q, args)}

    def set_missing(self, tag, asset, **cols):
        cols["ts"] = self.now()
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO missing (tag, asset) VALUES (?, ?)", (tag, asset))
            sets = ", ".join(f"{k} = ?" for k in cols)
            self.db.execute(f"UPDATE missing SET {sets} WHERE tag = ? AND asset = ?",
                            (*cols.values(), tag, asset))
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
            if f and d["action"] == "notface" and not d["error"]:
                f.gone = True

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
            for kind in ("faceOnly", "both"):
                ex[kind] = self.label_faces(self.model.tag_examples(tag_id, person, kind))
            ex["tagOnly"] = self.model.tag_examples(tag_id, person, "tagOnly")
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
        cluster. keep: accept the face where it is now. notface: delete the
        face detection (not reversible)."""
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
                if action == "notface":
                    try:
                        self.writer.delete_face(fid, f.asset, why="not a face", ref={"face": fid})
                        f.gone = True
                        self.state.set_face(fid, "notface", None)
                        results["ok"] += 1
                    except Exception as e:
                        self.state.set_face(fid, "notface", None, str(e))
                        results["failed"].append({"face": fid, "error": str(e)})
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

    # -- unnamed clusters ----------------------------------------------
    def clusters(self):
        """Clusters no pairing claimed: where false detections collect."""
        claimed = {c for cs in self.confirmed_pairing().values() for c in cs}
        out = []
        for c, fids in self.model.new_faces.items():
            mine = [fid for fid in fids if self.model.mine(self.model.faces[fid])]
            if c in claimed or len(mine) < CLUSTER_MIN_FACES:
                continue
            out.append({"id": c, "faces": len(mine), "others": len(fids) - len(mine)})
        out.sort(key=lambda r: -r["faces"])
        return {"clusters": out, "server": lib.BASE_URL}

    def cluster_detail(self, cid: str):
        done = self.state.faces()
        faces = [f for f in self.model.faces.values()
                 if f.new == cid and self.model.mine(f)
                 and (not f.gone or (done.get(f.id) or {}).get("action") == "notface")]
        faces.sort(key=lambda f: self.model.assets[f.asset]["originalFileName"])
        return {"id": cid, "others": sum(1 for fid in self.model.new_faces.get(cid, ())
                                          if not self.model.mine(self.model.faces[fid])),
                "faces": self.label_faces([self.model.face_json(f) for f in faces])}

    # -- missing faces of tagged people (PLAN.md stage 4) ---------------
    def _centroids(self) -> dict:
        """Cluster -> normalised mean embedding, from after/centroids.csv."""
        if not hasattr(self, "_centroid_cache"):
            import numpy as np
            path = lib.ROOT / "after" / "centroids.csv"
            out = {}
            if path.exists():
                _, rows = lib.read_csv(path)
                for gid, emb, n in rows:
                    v = np.array(json.loads(emb), dtype=np.float32)
                    out[gid] = (v / (np.linalg.norm(v) or 1), int(n))
            self._centroid_cache = out
        return self._centroid_cache

    def _person_vector(self, clusters: list[str]):
        """A person's fingerprint: the face-weighted mean of their clusters."""
        import numpy as np
        cs = self._centroids()
        parts = [(cs[c][0], cs[c][1]) for c in clusters if c in cs]
        if not parts:
            return None
        v = sum(vec * n for vec, n in parts)
        return v / (np.linalg.norm(v) or 1)

    def _missing_tags(self) -> list[dict]:
        """Tags with a person that has an applied pairing: the only ones a
        face can be put on."""
        pairing = self.confirmed_pairing()
        out = []
        for t in self.tags()["tags"]:
            if t["person"] in pairing:
                out.append({"id": t["id"], "name": t["name"], "old": t["person"],
                            "personName": self.name_of(t["person"]),
                            "clusters": pairing[t["person"]]})
        return out

    @staticmethod
    def _tally(rows) -> dict:
        c = Counter(r["verdict"] for r in rows)
        t = {"confirmed": c["face"], "wrong": c["wrong"], "cant": c["cant"],
             "queued": c["notdetected"], "undetectable": c["undetectable"],
             "present": c["present"], "toReview": c[None]}
        t["checked"] = t["confirmed"] + t["wrong"]
        t["stop"] = t["wrong"] >= STOP_WRONG or (
            t["checked"] >= ROUND_FIRST and t["wrong"] / t["checked"] > STOP_SHARE)
        t["round"] = (ROUND_FIRST if t["checked"] < ROUND_FIRST or t["wrong"]
                      else min(ROUND_MAX, t["checked"]))
        return t

    def missing(self):
        rows = self.state.missing()
        by_tag = defaultdict(list)
        for (tag, _), r in rows.items():
            by_tag[tag].append(r)
        out = []
        for t in self._missing_tags():
            done = {r["asset"] for r in by_tag[t["id"]]}
            todo = [a for a in self.model.tagged_without(t["id"], set(t["clusters"])) if a not in done]
            out.append({"id": t["id"], "name": t["name"], "personName": t["personName"],
                        "untested": len(todo), "tally": self._tally(by_tag[t["id"]])})
        out.sort(key=lambda r: (r["untested"] == 0 and not r["tally"]["toReview"],
                                -r["tally"]["checked"], -r["untested"]))
        return {"tags": out, "detect": self.detect_status()}

    def missing_round(self, tag: str):
        """The next photos to review for a tag: those a detection round came
        back with first, then a round of untested ones. Each with its live
        faces, ranked, and a default verdict."""
        t = next((x for x in self._missing_tags() if x["id"] == tag), None)
        if t is None:
            raise ValueError("tag has no paired person")
        rows = self.state.missing(tag)
        tally = self._tally(rows.values())
        target = t["clusters"][0]
        vec = self._person_vector(t["clusters"])
        cents = self._centroids()

        def score(face):
            if vec is None or face["person"] not in cents:
                return None
            return round(float(cents[face["person"]][0] @ vec), 3)

        order = self.model.tagged_without(tag, set(t["clusters"]))
        refreshed = [a for a in order if (r := rows.get((tag, a)))
                     and r["verdict"] is None and r["refreshed"]][:tally["round"]]
        untested = [a for a in order if (tag, a) not in rows]
        photos = []

        def build(asset, added):
            faces = self._faces_or_none(asset)
            if faces is None:
                return None
            for f in faces:
                f["score"] = score(f)
                f["added"] = f["id"] in added
                f["named"] = bool(f["personName"])
                f["isTarget"] = f["person"] == target
            faces.sort(key=lambda f: (not f["isTarget"], f["named"], -(f["score"] or -1),
                                      -(f["box"][2] - f["box"][0]) * (f["box"][3] - f["box"][1])))
            return faces

        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(8) as ex:
            for asset, faces in zip(refreshed, ex.map(
                    lambda a: build(a, set(json.loads(rows[(tag, a)]["added"] or "[]"))), refreshed)):
                if faces is None:
                    self.state.set_missing(tag, asset, verdict="gone")
                    continue
                photos.append({"asset": asset, "refreshed": True, "faces": faces})
            # Untested photos, until the round is full. A photo whose face is
            # already on the person (moved since the export) is not shown.
            i = 0
            while len(photos) < tally["round"] and i < len(untested):
                chunk = untested[i:i + 16]
                i += 16
                for asset, faces in zip(chunk, ex.map(lambda a: build(a, set()), chunk)):
                    if faces is None:
                        self.state.set_missing(tag, asset, verdict="gone")
                        continue
                    if any(f["isTarget"] for f in faces):
                        self.state.set_missing(tag, asset, verdict="present")
                        continue
                    if len(photos) < tally["round"]:
                        photos.append({"asset": asset, "refreshed": False, "faces": faces})

        for ph in photos:
            ph["file"] = self.model.assets[ph["asset"]]["originalFileName"]
            # Suggest a face only when it is convincing: already the person,
            # in a cluster close to them, or the photo's only unnamed face.
            # Otherwise the photo waits for your choice.
            unnamed = [f for f in ph["faces"] if not f["named"]]
            pick = (next((f for f in ph["faces"] if f["isTarget"]), None)
                    or next((f for f in unnamed if (f["score"] or 0) >= SUGGEST_SCORE), None)
                    or (unnamed[0] if len(unnamed) == 1 else None))
            ph["default"] = ({"verdict": "face", "face": pick["id"]} if pick else
                             {"verdict": None, "face": None} if unnamed else
                             {"verdict": "notdetected", "face": None})
        return {"id": tag, "name": t["name"], "personName": t["personName"],
                "target": target, "tally": tally, "photos": photos,
                "hasCentroids": vec is not None}

    def missing_decide(self, tag: str, verdicts: list[dict]) -> dict:
        t = next((x for x in self._missing_tags() if x["id"] == tag), None)
        if t is None:
            raise ValueError("tag has no paired person")
        target = t["clusters"][0]
        rows = self.state.missing(tag)
        results = {"ok": 0, "failed": []}
        with self.write_lock:
            for v in verdicts:
                asset, verdict, face = v["asset"], v["verdict"], v.get("face")
                if verdict not in ("face", "wrong", "cant", "notdetected"):
                    raise ValueError(f"unknown verdict {verdict}")
                refreshed = bool((rows.get((tag, asset)) or {}).get("refreshed"))
                if verdict == "notdetected" and refreshed:
                    verdict = "undetectable"    # detection already had its chance
                if verdict == "face":
                    try:
                        self.writer.reassign(face, asset, target, why=f"missing face {t['name']}",
                                             ref={"missing": [tag, asset]})
                    except Exception as e:
                        self.state.set_missing(tag, asset, error=str(e))
                        results["failed"].append({"asset": asset, "error": str(e)})
                        continue
                self.state.set_missing(tag, asset, verdict=verdict, face=face, error=None)
                results["ok"] += 1
        return results

    @staticmethod
    def _faces_or_none(asset):
        """Live faces, or None for a photo deleted since the snapshot."""
        try:
            return lib.live_faces(asset)
        except RuntimeError as e:
            if "HTTP 400" in str(e) or "HTTP 404" in str(e):
                return None
            raise

    def missing_sync(self):
        """Re-check every open photo against Immich: faces appear outside
        this app too (a Face Detection -> Missing run, Immich's own
        recognition). Photos that now have the person's face become
        `present`; queued ones that gained faces go back to the rounds."""
        rows = self.state.missing()
        work = []   # (tag, asset, row or None, target)
        for t in self._missing_tags():
            target = t["clusters"][0]
            for a in self.model.tagged_without(t["id"], set(t["clusters"])):
                r = rows.get((t["id"], a))
                if r is None or r["verdict"] in ("notdetected", "undetectable", None):
                    work.append((t["id"], a, r, target))
        import concurrent.futures as cf
        assets = sorted({a for _, a, _, _ in work})
        with cf.ThreadPoolExecutor(8) as ex:
            live = dict(zip(assets, ex.map(self._faces_or_none, assets)))
        out = Counter()
        for tag, a, r, target in work:
            faces = live[a]
            if faces is None:
                self.state.set_missing(tag, a, verdict="gone")
                out["gone"] += 1
            elif any(f["person"] == target for f in faces):
                self.state.set_missing(tag, a, verdict="present")
                out["present"] += 1
            elif r and r["verdict"] in ("notdetected", "undetectable") and faces and not r["refreshed"]:
                # Faces appeared since it was queued: back to the rounds.
                with self.state.lock:
                    self.state.db.execute("DELETE FROM missing WHERE tag = ? AND asset = ?", (tag, a))
                    self.state.db.commit()
                out["requeued"] += 1
            else:
                out["open"] += 1
        return {"checked": len(assets), **out}

    # -- detection rounds ---------------------------------------------
    def _detect_targets(self) -> dict[str, list[tuple[str, str]]]:
        """Photos a detection round refreshes: the queued ones (`not detected`)
        and every untested one, so each photo reaches the review already
        refreshed and is classified once. asset -> [(tag, target person)]."""
        rows = self.state.missing()
        out = defaultdict(list)
        for t in self._missing_tags():
            for a in self.model.tagged_without(t["id"], set(t["clusters"])):
                r = rows.get((t["id"], a))
                if r is None or r["verdict"] == "notdetected":
                    out[a].append((t["id"], t["clusters"][0]))
        return out

    def detect_status(self):
        d = self.__dict__.setdefault("_detect", {"state": "idle"})
        queued = (range(d["total"] - d["done"]) if d["state"] == "running"
                  else self._detect_targets())
        try:
            score = lib.min_detection_score()
        except Exception as e:
            score = None
            d.setdefault("configError", str(e))
        return {**d, "queued": len(queued), "minScore": score,
                "canRun": score is not None and score <= DETECT_MAX_SCORE,
                "normal": DETECT_NORMAL, "maxScore": DETECT_MAX_SCORE}

    def detect_run(self):
        st = self.detect_status()
        if st["state"] == "running":
            raise ValueError("a detection round is already running")
        if not st["canRun"]:
            raise ValueError(f"lower the min detection score to {DETECT_MAX_SCORE} or less first "
                             f"(now {st['minScore']})")
        if not st["queued"]:
            raise ValueError("no photos are queued for detection")
        if not lib.ml_idle():
            raise ValueError("the ML queues are busy -- wait until they are idle")
        self._detect = {"state": "running", "done": 0, "total": st["queued"], "added": 0}
        threading.Thread(target=self._detect_thread, daemon=True).start()
        return self.detect_status()

    def _detect_thread(self):
        d = self._detect
        try:
            targets = self._detect_targets()
            assets = sorted(targets)
            prior = sum(len(e["assets"]) for e in writer.read_journal(self.writer.journal)
                        if e["op"] == "face.refresh" and e["status"] == "done")
            i = 0
            while i < len(assets):
                size = 1 if prior + i < DETECT_FIRST else DETECT_BATCH
                batch = assets[i:i + size]
                i += len(batch)
                if lib.min_detection_score() > DETECT_MAX_SCORE:
                    raise RuntimeError("the min detection score was raised during the round; stopped")
                baseline = {a: f for a in batch if (f := self._faces_or_none(a)) is not None}
                for a in batch:
                    if a not in baseline:
                        for tag, _ in targets[a]:
                            self.state.set_missing(tag, a, verdict="gone")
                batch = list(baseline)
                if not batch:
                    continue
                with self.write_lock:
                    entry = self.writer.refresh_faces(baseline, why="missing faces")
                if self.dry_run:
                    after = baseline
                else:
                    self._wait_idle()
                    after = {a: lib.live_faces(a) for a in batch}
                added, removed = {}, {}
                for a in batch:
                    was = {f["id"] for f in baseline[a]}
                    now = {f["id"] for f in after[a]}
                    if now - was:
                        added[a] = sorted(now - was)
                    if was - now:
                        removed[a] = sorted(was - now)
                self.writer.record_diff(entry["seq"], added, removed)
                # Every refreshed photo comes back for review, even with nothing
                # added: a failed detection job looks exactly like "no face
                # found" from here, so closing a photo is your call, not ours.
                # Only a photo where Immich put the person's face itself closes.
                # Photos you answered while the batch ran keep your verdict.
                rows = self.state.missing()
                for a in batch:
                    for tag, target in targets[a]:
                        r = rows.get((tag, a))
                        if r is not None and r["verdict"] != "notdetected":
                            continue
                        found = any(f["person"] == target for f in after[a])
                        self.state.set_missing(tag, a, verdict="present" if found else None,
                                               refreshed=1, added=json.dumps(added.get(a, [])))
                d["done"] += len(batch)
                d["added"] += sum(len(v) for v in added.values())
                if d["done"] >= DETECT_CANARY and not d["added"]:
                    raise RuntimeError(
                        f"detection added no face to the first {d['done']} photos -- Immich's "
                        f"detection jobs may be failing (check the immich_machine_learning "
                        f"logs); stopped")
                if removed:
                    n = sum(len(v) for v in removed.values())
                    raise RuntimeError(f"{n} existing face(s) disappeared on {len(removed)} photo(s) "
                                       f"(journal #{entry['seq']}); stopped")
            d["state"] = "done"
        except Exception as e:
            traceback.print_exc()
            d.update(state="error", error=str(e))

    @staticmethod
    def _wait_idle():
        """Until detection and the recognition it queues are both finished:
        idle on two polls in a row, since one job hands over to the next."""
        start, quiet = time.time(), 0
        time.sleep(3)
        while quiet < 2:
            if time.time() - start > DETECT_TIMEOUT:
                raise RuntimeError("the ML queues did not go idle within an hour; stopped")
            quiet = quiet + 1 if lib.ml_idle() else 0
            time.sleep(3)

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
        try:
            score = lib.min_detection_score()
        except Exception:
            score = None
        return {"simulated": self.model.simulated, "dryRun": self.dry_run,
                "minScore": score, "normalScore": DETECT_NORMAL,
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
            f"{lib.BASE_URL}/api/assets/{asset_id}/thumbnail?size=preview&edited=true",
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
                "/api/featured": app.featured, "/api/clusters": app.clusters,
                "/api/missing": app.missing, "/api/detect": app.detect_status,
            }
            if p in routes:
                return self._json(routes[p]())
            if m := re.fullmatch(rf"/api/tags/({UUID})", p):
                return self._json(app.tag_detail(m[1], q.get("person")))
            if m := re.fullmatch(rf"/api/review/({UUID})", p):
                return self._json(app.review_person(m[1]))
            if m := re.fullmatch(rf"/api/featured/({UUID})", p):
                return self._json(app.featured_detail(m[1]))
            if m := re.fullmatch(rf"/api/clusters/({UUID})", p):
                return self._json(app.cluster_detail(m[1]))
            if m := re.fullmatch(rf"/api/missing/({UUID})", p):
                return self._json(app.missing_round(m[1]))
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
            if m := re.fullmatch(rf"/api/missing/({UUID})", p):
                return self._json(app.missing_decide(m[1], b["verdicts"]))
            if p == "/api/detect":
                return self._json(app.detect_run())
            if p == "/api/missing-sync":
                return self._json(app.missing_sync())

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
