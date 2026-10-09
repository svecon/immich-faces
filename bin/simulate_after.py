#!/usr/bin/env python3
"""Fabricate an after-reset database from before.db, for developing the web app.

Mimics what the regeneration does (see PLAN.md): machine-learning faces get new
person groups, people without faces disappear, manual-face survivors stay.
Then seeds every kind of disagreement the review has to handle:

  split    a named person's faces spread over two new clusters
  blend    two named people's faces in one new cluster
  stray    single faces moved to an unrelated cluster
  lost     faces left unassigned
  gained   previously unassigned faces now recognised as a named person

Output: data/after-sim.db, marked `simulated` in its meta table. serve.py
refuses to write to Immich while it is using a simulated database.
"""

import argparse
import random
import shutil
import sqlite3
import uuid

import lib

SPLIT_SHARE = 0.06      # of named people
BLENDS = 2              # pairs of named people
STRAY = 0.02            # of assigned faces
LOST = 0.015            # of assigned faces
GAINED = 0.03           # of unassigned faces


def new_id(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    src, out = lib.DATA / "before.db", lib.DATA / "after-sim.db"
    if not src.exists():
        lib.die(f"{src} missing -- run 01_import.py --label before first")
    shutil.copyfile(src, out)
    db = lib.connect(out)
    # before.db from an older importer lacks these; harmless if present.
    for t in ("asset_face", "asset", "tag"):
        db.execute(f'CREATE INDEX IF NOT EXISTS "ix_{t}_id" ON "{t}" ("id")')
    owner = db.execute("SELECT value FROM meta WHERE key = 'ownerId'").fetchone()[0]

    survivors = {r[0] for r in db.execute(
        "SELECT DISTINCT personGroupId FROM asset_face "
        "WHERE sourceType = 'manual' AND deletedAt = ''")}
    named = [r[0] for r in db.execute(
        "SELECT personGroupId FROM person WHERE ownerId = ? AND name != ''", (owner,))]

    # Every old group gets a fresh id, except that its ML faces are what moves:
    # survivors keep their old id for their manual faces only.
    remap = {g: new_id(rng) for (g,) in db.execute(
        "SELECT DISTINCT personGroupId FROM asset_face WHERE personGroupId != ''")}
    for a, b in zip(*[iter(rng.sample(named, BLENDS * 2))] * 2):
        remap[b] = remap[a]
    split = {g: new_id(rng) for g in rng.sample(named, max(1, int(len(named) * SPLIT_SHARE)))}

    faces = db.execute("SELECT id, personGroupId, sourceType FROM asset_face").fetchall()
    targets = list(set(remap.values()))
    named_targets = [remap[g] for g in named]
    updates = []
    for fid, g, source in faces:
        if source != "machine-learning":
            continue
        if not g:
            new = rng.choice(named_targets) if rng.random() < GAINED else ""
        else:
            r = rng.random()
            if r < LOST:
                new = ""
            elif r < LOST + STRAY:
                new = rng.choice(targets)
            elif g in split and rng.random() < 0.35:
                new = split[g]
            else:
                new = remap[g]
        updates.append((new, fid))
    db.executemany("UPDATE asset_face SET personGroupId = ? WHERE id = ?", updates)

    # People: drop every group left without faces, create one person per owner
    # for each new group that owner has faces in -- as handleRecognizeFaces does.
    live = {r[0] for r in db.execute(
        "SELECT DISTINCT personGroupId FROM asset_face WHERE personGroupId != ''")}
    db.execute("DELETE FROM person WHERE personGroupId NOT IN "
               "(SELECT DISTINCT personGroupId FROM asset_face WHERE personGroupId != '')")
    db.execute("DELETE FROM person_group WHERE id NOT IN "
               "(SELECT DISTINCT personGroupId FROM asset_face WHERE personGroupId != '')")
    cluster = db.execute("SELECT clusterGroupId FROM person_group LIMIT 1").fetchone()[0]
    have = {(r[0], r[1]) for r in db.execute("SELECT personGroupId, ownerId FROM person")}
    pairs = db.execute(
        "SELECT DISTINCT f.personGroupId, a.ownerId FROM asset_face f "
        "JOIN asset a ON a.id = f.assetId WHERE f.personGroupId != ''").fetchall()
    groups = {r[0] for r in db.execute("SELECT id FROM person_group")}
    for g in live - groups:
        db.execute("INSERT INTO person_group (id, clusterGroupId) VALUES (?, ?)", (g, cluster))
    for g, o in pairs:
        if (g, o) not in have:
            db.execute("INSERT INTO person (ownerId, name, isHidden, birthDate, isFavorite, "
                       "personGroupId) VALUES (?, '', 'f', '', 'f', ?)", (o, g))

    db.execute("UPDATE meta SET value = 'after' WHERE key = 'label'")
    db.execute("INSERT INTO meta VALUES ('simulated', ?)", (f"seed {args.seed}",))
    db.commit()
    n = db.execute("SELECT count(*), sum(name != '') FROM person WHERE ownerId = ?",
                   (owner,)).fetchone()
    lib.log(f"wrote {out}: {n[0]} of your people ({n[1]} named survivors), "
            f"{len(split)} split, {BLENDS} blended pairs")


if __name__ == "__main__":
    main()
