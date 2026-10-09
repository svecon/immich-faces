#!/usr/bin/env python3
"""Stage 1 -- load a Postgres CSV export into SQLite and prove it is faithful.

Run once on the export taken before the reset (`--label before`) and again on
the one taken after (`--label after`). Both databases get the same shape, so
the comparison stages simply ATTACH the two.

The CSVs are the snapshot, not the API: ~107k faces would be one API call per
asset. The API is used only to cross-check the CSVs against the live server
and to fetch person thumbnails, which the reset deletes.

Checks, any of which fails the import:
  - every CSV row landed in SQLite
  - references resolve: face -> asset, face -> person_group, tag_asset -> tag
  - the API's people match person.csv: same ids, names, birthdays, flags
  - a random sample of assets has the same faces, assigned to the same people

A mismatch means the server changed since the export (or the export is from
another instance). Re-export rather than override: this database is the only
record of the old names once the reset runs.
"""

import argparse
import concurrent.futures as cf
import random
import sqlite3
import time
from pathlib import Path

import lib

SAMPLE_ASSETS = 40

INDEXES = [
    ("asset_face", "assetId"), ("asset_face", "personGroupId"),
    ("person", "personGroupId"), ("person", "ownerId"),
    ("tag_asset", "assetId"), ("tag_asset", "tagId"),
]


def load(db: sqlite3.Connection, src: Path) -> dict[str, int]:
    counts = {}
    for table in lib.TABLES:
        path = src / f"{table}.csv"
        if not path.exists():
            lib.die(f"{path} missing -- see PLAN.md for the export commands")
        header, rows = lib.read_csv(path)
        bad = [i for i, r in enumerate(rows) if len(r) != len(header)]
        if bad:
            lib.die(f"{path}: {len(bad)} row(s) with the wrong column count, "
                    f"first at data row {bad[0] + 1}")
        cols = ", ".join(f'"{c}" TEXT' for c in header)
        db.execute(f'CREATE TABLE "{table}" ({cols})')
        marks = ", ".join("?" * len(header))
        db.executemany(f'INSERT INTO "{table}" VALUES ({marks})', rows)
        n = db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        if n != len(rows):
            lib.die(f"{table}: {len(rows)} CSV rows but {n} in SQLite")
        counts[table] = n
        lib.log(f"  {table:13} {n:>8} rows")
    for table, col in INDEXES:
        db.execute(f'CREATE INDEX "ix_{table}_{col}" ON "{table}" ("{col}")')
    return counts


def check_references(db: sqlite3.Connection) -> list[str]:
    q = {
        "faces on an unknown asset":
            'SELECT count(*) FROM asset_face f LEFT JOIN asset a ON a.id = f.assetId '
            'WHERE a.id IS NULL',
        "faces in an unknown person group":
            "SELECT count(*) FROM asset_face f LEFT JOIN person_group g "
            "ON g.id = f.personGroupId WHERE f.personGroupId != '' AND g.id IS NULL",
        "people in an unknown person group":
            'SELECT count(*) FROM person p LEFT JOIN person_group g '
            'ON g.id = p.personGroupId WHERE g.id IS NULL',
        "tag links to an unknown tag":
            'SELECT count(*) FROM tag_asset t LEFT JOIN tag ON tag.id = t.tagId '
            'WHERE tag.id IS NULL',
        "tag links to an unknown asset":
            'SELECT count(*) FROM tag_asset t LEFT JOIN asset a ON a.id = t.assetId '
            'WHERE a.id IS NULL',
    }
    problems = []
    for what, sql in q.items():
        n = db.execute(sql).fetchone()[0]
        if n:
            problems.append(f"{n} {what}")
    return problems


def check_people(db: sqlite3.Connection, api_people: list[dict]) -> tuple[str, list[str]]:
    """Compare the API's people with person.csv. Returns (ownerId, problems)."""
    ids = {p["id"] for p in api_people}
    owners = {r["ownerId"] for r in db.execute("SELECT ownerId, personGroupId FROM person")
              if r["personGroupId"] in ids}
    if len(owners) != 1:
        return "", [f"API people map to {len(owners)} owners in person.csv, expected 1"]
    owner = owners.pop()

    rows = {r["personGroupId"]: r for r in
            db.execute("SELECT * FROM person WHERE ownerId = ?", (owner,))}
    problems = []
    only_api = ids - rows.keys()
    only_csv = rows.keys() - ids
    if only_api:
        problems.append(f"{len(only_api)} people in the API but not in person.csv")
    # The API lists a person only if named or with >= `minimumFaces` (a user
    # preference, default 3) visible faces on timeline assets. The rest are
    # small unnamed clusters -- real data, just not listed. A NAMED person
    # missing from the API is a real mismatch.
    named_hidden = [g for g in only_csv if rows[g]["name"]]
    if named_hidden:
        problems.append(f"{len(named_hidden)} named people in person.csv but not in the API")
    if only_csv:
        lib.log(f"  people: {len(only_csv)} small unnamed clusters are not listed "
                f"by the API (below minimumFaces) -- expected")
    changed = 0
    for p in api_people:
        r = rows.get(p["id"])
        if r is None:
            continue
        if (p["name"] != r["name"]
                or (p["birthDate"] or "") != r["birthDate"]
                or p["isHidden"] != (r["isHidden"] == "t")
                or bool(p.get("isFavorite")) != (r["isFavorite"] == "t")):
            changed += 1
    if changed:
        problems.append(f"{changed} people differ between the API and person.csv "
                        f"(name, birthday, hidden or favourite)")
    return owner, problems


def check_faces(db: sqlite3.Connection, owner: str) -> list[str]:
    """Faces of a random sample of the owner's assets, API vs CSV."""
    asset_ids = [r[0] for r in db.execute(
        "SELECT DISTINCT f.assetId FROM asset_face f JOIN asset a ON a.id = f.assetId "
        "WHERE a.ownerId = ? AND f.deletedAt = ''", (owner,))]
    sample = random.sample(asset_ids, min(SAMPLE_ASSETS, len(asset_ids)))
    differ = []
    for aid in sample:
        live = {(f["id"], (f.get("person") or {}).get("id") or "")
                for f in lib.api("GET", f"/faces?id={aid}")}
        snap = {(r["id"], r["personGroupId"]) for r in db.execute(
            "SELECT id, personGroupId FROM asset_face WHERE assetId = ? AND deletedAt = ''",
            (aid,))}
        if live != snap:
            differ.append(aid)
    lib.log(f"  faces: {len(sample) - len(differ)}/{len(sample)} sampled assets match")
    return [f"{len(differ)} of {len(sample)} sampled assets have different faces, "
            f"e.g. asset {differ[0]}"] if differ else []


def fetch_thumbnails(people: list[dict], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    todo = [p for p in people if not (dest / f"{p['id']}.jpg").exists()]

    def one(p):
        data = lib.api("GET", f"/people/{p['id']}/thumbnail", raw=True)
        tmp = dest / f"{p['id']}.jpg.tmp"
        tmp.write_bytes(data)
        tmp.rename(dest / f"{p['id']}.jpg")

    failed = 0
    with cf.ThreadPoolExecutor(8) as ex:
        for fut in cf.as_completed([ex.submit(one, p) for p in todo]):
            if fut.exception():
                failed += 1
    lib.log(f"  thumbnails: {len(people) - len(todo)} already present, "
            f"{len(todo) - failed} fetched, {failed} failed -> {dest}")


def summarise(db: sqlite3.Connection, owner: str) -> None:
    for r in db.execute("""
        SELECT p.ownerId = ? AS mine, count(*) AS people,
               sum(p.name != '') AS named, sum(p.birthDate != '') AS birthdays,
               sum(p.isHidden = 't') AS hidden
        FROM person p GROUP BY 1 ORDER BY 1 DESC""", (owner,)):
        who = "you" if r["mine"] else "other user(s)"
        lib.log(f"  {who:14} {r['people']:>5} people, {r['named']} named, "
                f"{r['birthdays']} with birthday, {r['hidden']} hidden")
    for r in db.execute("""
        SELECT f.sourceType, f.personGroupId != '' AS assigned, count(*) AS n
        FROM asset_face f WHERE f.deletedAt = '' GROUP BY 1, 2 ORDER BY 1, 2 DESC"""):
        lib.log(f"  faces {r['sourceType']:17} {'assigned' if r['assigned'] else 'unassigned':10} "
                f"{r['n']:>7}")
    r = db.execute("""
        SELECT count(DISTINCT t.id), count(*), count(DISTINCT ta.assetId)
        FROM tag t JOIN tag_asset ta ON ta.tagId = t.id
        WHERE t.value LIKE 'People/%'""").fetchone()
    lib.log(f"  People/* tags: {r[0]} tags on {r[2]} assets ({r[1]} links)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, choices=["before", "after"])
    ap.add_argument("--src", type=Path,
                    help="folder with the CSVs (default: snapshot/ for before, after/ for after)")
    ap.add_argument("--force", action="store_true", help="replace an existing database")
    ap.add_argument("--no-thumbnails", action="store_true")
    args = ap.parse_args()

    src = args.src or lib.ROOT / ("snapshot" if args.label == "before" else "after")
    out = lib.DATA / f"{args.label}.db"
    lib.DATA.mkdir(parents=True, exist_ok=True)
    if out.exists() and not args.force:
        lib.die(f"{out} already exists; --force replaces it")

    tmp = out.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    db = lib.connect(tmp)

    lib.log(f"importing {src} -> {out}")
    counts = load(db, src)
    problems = check_references(db)

    lib.log("cross-checking against the live API")
    version = lib.api("GET", "/server/version")
    api_people = lib.all_people()
    owner, p_problems = check_people(db, api_people)
    problems += p_problems
    lib.log(f"  people: {len(api_people)} from the API, owner {owner[:8] or '?'}")
    if owner:
        problems += check_faces(db, owner)
        summarise(db, owner)

    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    db.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("label", args.label),
        ("source", str(src)),
        ("importedAt", time.strftime("%Y-%m-%dT%H:%M:%S%z")),
        ("serverVersion", f"{version['major']}.{version['minor']}.{version['patch']}"),
        ("ownerId", owner),
        ("counts", repr(counts)),
    ])
    db.commit()
    db.close()

    if problems:
        tmp.unlink()
        lib.die("snapshot does not match the live server -- re-export, do not override:\n  "
                + "\n  ".join(problems))
    tmp.rename(out)
    lib.log(f"wrote {out}")

    if not args.no_thumbnails:
        # By id from person.csv, not from the API list: the small clusters the
        # list leaves out still have a thumbnail, and the reset deletes it.
        db = lib.connect(out)
        mine = [{"id": r[0]} for r in db.execute(
            "SELECT personGroupId FROM person WHERE ownerId = ?", (owner,))]
        db.close()
        fetch_thumbnails(mine, lib.DATA / "thumbs" / args.label)


if __name__ == "__main__":
    main()
