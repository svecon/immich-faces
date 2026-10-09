"""The comparison itself: before.db vs after.db, held in memory.

~107k faces fit comfortably, and every view the web app shows is a few dict
lookups away, so nothing here touches SQLite after loading.

Vocabulary (see PLAN.md):
  old person   a person group in before.db (named, or carrying a flag)
  new cluster  a person group in after.db
  pairing      old person -> the new cluster(s) that are now the same person
  identity     what a new cluster *is*, via the pairings: an old person or none
"""

from __future__ import annotations

import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import lib

# A pairing is confident only when all of these hold. Tuned on the real
# numbers once after.db exists -- see PLAN.md.
MIN_COVERAGE = 0.90
MIN_PURITY = 0.98
MIN_FACES = 10
# A second cluster counts as part of a split when it holds this many of the
# old person's faces (both conditions).
SPLIT_MIN_FACES = 3
SPLIT_MIN_SHARE = 0.05

TAG_PREFIX = "People/"
TAKEOUT_PREFIX = "takeout-"


def fold(s: str) -> str:
    """Case- and accent-insensitive form for matching tag names to people."""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).casefold().strip()


@dataclass
class Face:
    id: str
    asset: str
    source: str
    box: tuple[int, int, int, int]
    size: tuple[int, int]
    old: str            # person group before, '' if unassigned
    new: str = ""       # person group after, '' if unassigned
    gone: bool = False  # deleted, or missing from after.db


@dataclass
class Person:
    id: str
    name: str
    birth: str
    hidden: bool
    favorite: bool
    faces: list[str] = field(default_factory=list)      # face ids, this owner's assets

    def as_json(self):
        return {"id": self.id, "name": self.name, "birthDate": self.birth or None,
                "isHidden": self.hidden, "isFavorite": self.favorite,
                "faces": len(self.faces)}


def _people(db: sqlite3.Connection, owner: str) -> dict[str, Person]:
    return {r["personGroupId"]: Person(r["personGroupId"], r["name"], r["birthDate"],
                                       r["isHidden"] == "t", r["isFavorite"] == "t")
            for r in db.execute("SELECT * FROM person WHERE ownerId = ?", (owner,))}


class Model:
    def __init__(self, before: Path, after: Path | None):
        b = lib.connect(before)
        self.owner = b.execute("SELECT value FROM meta WHERE key = 'ownerId'").fetchone()[0]
        self.assets = {r["id"]: dict(r) for r in b.execute(
            "SELECT id, ownerId, originalFileName, type, visibility FROM asset "
            "WHERE deletedAt = ''")}
        self.faces: dict[str, Face] = {}
        for r in b.execute("SELECT * FROM asset_face WHERE deletedAt = ''"):
            a = self.assets.get(r["assetId"])
            if a is None:
                continue
            self.faces[r["id"]] = Face(
                r["id"], r["assetId"], r["sourceType"],
                (int(r["boundingBoxX1"]), int(r["boundingBoxY1"]),
                 int(r["boundingBoxX2"]), int(r["boundingBoxY2"])),
                (int(r["imageWidth"]), int(r["imageHeight"])),
                r["personGroupId"])
        self.old = _people(b, self.owner)
        self.faces_by_asset: dict[str, list[str]] = defaultdict(list)
        for f in self.faces.values():
            self.faces_by_asset[f.asset].append(f.id)
            if f.old in self.old and self.mine(f):
                self.old[f.old].faces.append(f.id)

        self.tags = {r["id"]: dict(r) for r in b.execute("SELECT * FROM tag")}
        self.assets_by_tag: dict[str, set[str]] = defaultdict(set)
        self.tags_by_asset: dict[str, set[str]] = defaultdict(set)
        for r in b.execute("SELECT * FROM tag_asset"):
            self.assets_by_tag[r["tagId"]].add(r["assetId"])
            self.tags_by_asset[r["assetId"]].add(r["tagId"])
        b.close()

        self.after_path = after
        self.simulated = None
        self.new: dict[str, Person] = {}
        self.new_faces: dict[str, list[str]] = defaultdict(list)
        if after and after.exists():
            self._load_after(after)

    # ------------------------------------------------------------------
    def _load_after(self, after: Path) -> None:
        a = lib.connect(after)
        meta = dict(a.execute("SELECT key, value FROM meta").fetchall())
        if meta.get("ownerId") != self.owner:
            lib.die(f"{after} belongs to another owner than before.db")
        self.simulated = meta.get("simulated")
        present = set()
        for r in a.execute("SELECT id, personGroupId FROM asset_face WHERE deletedAt = ''"):
            f = self.faces.get(r["id"])
            if f is None:
                continue        # detected after the snapshot: no old opinion
            f.new = r["personGroupId"]
            present.add(f.id)
        for f in self.faces.values():
            if f.id not in present:
                f.gone = True
        self.new = _people(a, self.owner)
        for f in self.faces.values():
            if f.new and not f.gone:
                self.new_faces[f.new].append(f.id)
                if f.new in self.new and self.mine(f):
                    self.new[f.new].faces.append(f.id)
        a.close()

    @property
    def has_after(self) -> bool:
        return bool(self.new)

    def mine(self, f: Face) -> bool:
        return self.assets[f.asset]["ownerId"] == self.owner

    # ------------------------------------------------------------------
    # People pairing
    # ------------------------------------------------------------------
    # Names for old people that had none, from trusted Google tags. Set by the
    # app; makes those clusters pairable like any named person.
    tag_names: dict[str, str] = {}

    def relevant_old(self) -> list[Person]:
        """Old people worth restoring: anything set by hand, or named by a tag."""
        return [p for p in self.old.values()
                if p.name or p.hidden or p.favorite or p.id in self.tag_names]

    def display_name(self, pid: str) -> str:
        p = self.old.get(pid)
        return (p.name or self.tag_names.get(pid, "")) if p else ""

    def transitions(self) -> dict[str, Counter]:
        """old person -> Counter(new cluster -> faces), ML faces on your assets."""
        t: dict[str, Counter] = defaultdict(Counter)
        for f in self.faces.values():
            if f.old and not f.gone and f.source == "machine-learning" and self.mine(f):
                t[f.old][f.new] += 1
        return t

    def composition(self, cluster: str) -> Counter:
        """new cluster -> Counter(old person or '' -> faces), your assets only."""
        return Counter(self.faces[fid].old for fid in self.new_faces.get(cluster, ())
                       if self.mine(self.faces[fid]))

    def proposals(self) -> list[dict]:
        """One proposal per relevant old person: where its faces went, and how sure."""
        trans = self.transitions()
        relevant = {p.id for p in self.relevant_old()}
        primary_of: dict[str, list[str]] = defaultdict(list)
        out = []
        for p in self.relevant_old():
            moved = trans.get(p.id, Counter())
            total = sum(moved.values())
            clusters = []
            for c, n in moved.most_common():
                if not c:
                    continue
                comp = self.composition(c)
                named_here = sum(v for k, v in comp.items() if k in relevant)
                clusters.append({
                    "id": c, "faces": n,
                    "coverage": n / total if total else 0,
                    "purity": n / named_here if named_here else 0,
                    "size": len(self.new_faces.get(c, ())),
                    "others": [{"id": k, "name": self.display_name(k), "faces": v}
                               for k, v in comp.most_common(6) if k in relevant and k != p.id],
                    "current": self.new[c].as_json() if c in self.new else None,
                })
            survivor = p.id in self.new
            old = p.as_json()
            if not old["name"] and p.id in self.tag_names:
                old["name"] = self.tag_names[p.id]
                old["nameFromTag"] = True
            out.append({"old": old, "total": total,
                        "unassigned": moved.get("", 0), "clusters": clusters,
                        "survivor": survivor})
            if clusters:
                primary_of[clusters[0]["id"]].append(p.id)

        for prop in out:
            prop["kind"], prop["why"] = self._classify(prop, primary_of)
            prop["targets"] = self._default_targets(prop)
        order = {"confident": 0, "survivor": 1, "split": 2, "blend": 3,
                 "weak": 4, "lost": 5}
        out.sort(key=lambda x: (order[x["kind"]], -x["total"]))
        return out

    def _classify(self, prop, primary_of) -> tuple[str, str]:
        cl = prop["clusters"]
        if not cl:
            return "lost", "no face of this person is assigned to anyone now"
        top = cl[0]
        if prop["survivor"]:
            return "survivor", ("kept its name through manual faces; its "
                                "recognised faces need merging back in")
        shared = [x for x in primary_of[top["id"]] if x != prop["old"]["id"]]
        if shared:
            return "blend", "another old person ended up mostly in the same cluster"
        if top["purity"] < MIN_PURITY:
            return "blend", f"cluster is only {top['purity']:.0%} this person"
        parts = [c for c in cl[1:] if c["faces"] >= SPLIT_MIN_FACES
                 and c["coverage"] >= SPLIT_MIN_SHARE]
        if parts:
            return "split", f"spread over {len(parts) + 1} clusters"
        if prop["total"] < MIN_FACES:
            return "weak", f"only {prop['total']} faces to judge by"
        if top["coverage"] < MIN_COVERAGE:
            return "weak", f"only {top['coverage']:.0%} of faces in one cluster"
        return "confident", (f"{top['coverage']:.0%} of faces in one cluster, "
                             f"which is {top['purity']:.0%} this person")

    def _default_targets(self, prop) -> list[str]:
        """Clusters to merge into this person, primary first."""
        cl = prop["clusters"]
        if not cl:
            return []
        parts = [c["id"] for c in cl if c["faces"] >= SPLIT_MIN_FACES
                 and c["coverage"] >= SPLIT_MIN_SHARE and c["purity"] >= MIN_PURITY]
        if prop["survivor"]:
            return [prop["old"]["id"]] + [c for c in parts if c != prop["old"]["id"]]
        return parts or [cl[0]["id"]]

    def sample_faces(self, old: str | None, new: str | None, limit=24,
                     only_mine=True) -> list[dict]:
        """Faces that were in `old` and are now in `new` (None = any)."""
        out = []
        pool = (self.old[old].faces if old in self.old else
                self.new_faces.get(new, []) if new else self.faces.keys())
        for fid in pool:
            f = self.faces[fid]
            if (old is None or f.old == old) and (new is None or f.new == new) \
                    and (not only_mine or self.mine(f)) and not f.gone:
                out.append(self.face_json(f))
                if len(out) >= limit:
                    break
        return out

    def face_json(self, f: Face) -> dict:
        return {"id": f.id, "asset": f.asset, "box": f.box, "size": f.size,
                "old": f.old, "new": f.new,
                "file": self.assets[f.asset]["originalFileName"]}

    # ------------------------------------------------------------------
    # Google People/* tags -- before data only, so usable before the reset
    # ------------------------------------------------------------------
    def people_tags(self) -> list[dict]:
        return [t for t in self.tags.values()
                if t["value"].startswith(TAG_PREFIX) and t["userId"] == self.owner]

    def takeout_assets(self) -> set[str]:
        out = set()
        for tid, t in self.tags.items():
            if t["value"].startswith(TAKEOUT_PREFIX) and t["userId"] == self.owner:
                out |= self.assets_by_tag[tid]
        return out

    def assets_of(self, person: str) -> set[str]:
        return {self.faces[f].asset for f in self.old[person].faces}

    def tag_candidates(self, tag_id: str, n=4) -> list[dict]:
        """People this tag could mean, by how many tagged photos show them.

        Overlap decides; the name only breaks ties. A first-name match is
        weak evidence -- "People/Anna" chose the wrong Anna on the first try --
        and the right answer is often an old cluster you never named.
        `suggested` marks the one candidate that clearly dominates, if any.
        """
        name = fold(self.tags[tag_id]["value"][len(TAG_PREFIX):])
        tagged = self.assets_by_tag[tag_id]
        overlap: dict[str, set] = defaultdict(set)
        for a in tagged:
            for fid in self.faces_by_asset.get(a, ()):
                f = self.faces[fid]
                if f.old in self.old:
                    overlap[f.old].add(a)
        with_faces = sum(1 for a in tagged if self.faces_by_asset.get(a))
        cands = []
        for pid, assets in overlap.items():
            pn = fold(self.old[pid].name)
            match = ("name" if pn and pn == name else
                     "first name" if pn and name and pn.split()[0] == name.split()[0] else "")
            cands.append({"id": pid, "name": self.old[pid].name, "match": match,
                          "overlap": len(assets),
                          "share": len(assets) / with_faces if with_faces else 0})
        cands.sort(key=lambda c: (-c["overlap"], c["match"] != "name"))
        top = cands[:n]
        if top:
            second = top[1]["overlap"] if len(top) > 1 else 0
            if top[0]["share"] >= 0.5 and top[0]["overlap"] >= 2 * second:
                top[0]["suggested"] = True
        return top

    def tag_stats(self, tag_id: str, person: str | None) -> dict:
        tagged = self.assets_by_tag[tag_id]
        with_faces = {a for a in tagged if self.faces_by_asset.get(a)}
        st = {"tagged": len(tagged), "taggedWithFaces": len(with_faces)}
        if not person or person not in self.old:
            return st
        has = self.assets_of(person)
        takeout = self.takeout_assets()
        both = tagged & has
        st.update({
            "both": len(both),
            "tagOnly": len(with_faces - has),
            "tagNoFaces": len(tagged - with_faces),
            # Only takeout assets can be expected to carry the tag at all.
            "faceOnly": len((has & takeout) - tagged),
            "agreement": len(both) / len(with_faces) if with_faces else 0,
        })
        return st

    def tag_examples(self, tag_id: str, person: str, kind: str, limit=24) -> list[dict]:
        """Faces illustrating a disagreement between a tag and a person."""
        tagged = self.assets_by_tag[tag_id]
        has = self.assets_of(person)
        out = []
        if kind == "tagOnly":
            for a in sorted(tagged - has):
                for fid in self.faces_by_asset.get(a, ()):
                    out.append(self.face_json(self.faces[fid]))
                if len(out) >= limit:
                    break
        elif kind == "faceOnly":
            pool = (has & self.takeout_assets()) - tagged
            for fid in self.old[person].faces:
                if self.faces[fid].asset in pool:
                    out.append(self.face_json(self.faces[fid]))
                    if len(out) >= limit:
                        break
        elif kind == "both":
            for fid in self.old[person].faces:
                if self.faces[fid].asset in tagged:
                    out.append(self.face_json(self.faces[fid]))
                    if len(out) >= limit:
                        break
        return out[:limit]

    def tag_opinion(self, asset: str, trusted: dict[str, str]) -> list[str]:
        """Old people the trusted tags on this asset vouch for."""
        return [trusted[t] for t in self.tags_by_asset.get(asset, ()) if t in trusted]

    # ------------------------------------------------------------------
    # Face review
    # ------------------------------------------------------------------
    def review(self, pairing: dict[str, list[str]]) -> dict[str, dict[str, list[Face]]]:
        """Faces that disagree with the confirmed pairings, grouped per person.

        `pairing`: old person -> [new clusters], primary first.
        Returns {old person: {"left": [...], "joined": [...]}}:
          left    faces this person had, now elsewhere or unassigned
          joined  faces now in this person's clusters that were not theirs --
                  someone else's, an unnamed cluster's, or unassigned before

        Only machine-learning faces on your assets: those are the ones the
        reset moved and the ones your key can reassign.
        """
        identity = {c: old for old, cs in pairing.items() for c in cs}
        out: dict[str, dict[str, list[Face]]] = defaultdict(lambda: {"left": [], "joined": []})
        for f in self.faces.values():
            if f.gone or f.source != "machine-learning" or not self.mine(f):
                continue
            was = f.old if f.old in pairing else ""
            now = identity.get(f.new, "")
            if was == now:
                continue
            if was:
                out[was]["left"].append(f)
            if now:
                out[now]["joined"].append(f)
        return out
