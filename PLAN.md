# Immich face reset — snapshot, restore, audit

Immich 3.2 added **cluster groups**: users in one group share face clustering, so
a person recognised in one library is recognised in the other. Getting there on
an existing library means **regenerating people**, which deletes every person —
names, birthdays, hidden and favourite flags — and clusters the faces again.

This project snapshots the people beforehand, then uses the snapshot to restore
the names and to audit the new clustering face by face.

Code only in git. Everything under `snapshot/`, `after/` and `data/` holds
names, birthdays and faces of real people; the `.gitignore` is a whitelist so
none of it can be committed by accident.

---

## What the reset does — read from the source, v3.3

`ClusterGroupService.regeneratePeople` queues `FacialRecognitionQueueAll` with
`force: true`. In `PersonService.handleQueueRecognizeFaces` that:

1. **unassigns** every face with `sourceType = machine-learning` in the group
   (`asset_face.personGroupId = NULL`). The face rows stay — **same face ids,
   same bounding boxes, same embeddings**;
2. **deletes every person left with no faces** (`handlePersonCleanup`) — this
   is where names and birthdays go;
3. re-runs recognition over the same faces, creating new people with new ids.

Consequences this project is built on:

- **Face ids survive.** Old and new assignments are compared by face id —
  exact, no bounding-box matching.
- **Manual faces are not unassigned**, so their people survive with their
  names. 13 named people have manual faces; each will keep its name and its
  manual faces while its machine-learning faces most likely land in a new,
  unnamed cluster — a *merge into the survivor*, not a rename.
- **Joining a cluster group wipes nothing** (`acceptRequest` only re-homes
  `person_group.clusterGroupId`). *Regenerate* is the destructive step.
- **Never run Jobs → Face Detection → All.** That one *deletes* the face rows
  (`handleQueueDetectFaces`, `deleteFaces`), and every face id this project
  relies on would be gone.
- In the API, a person's `id` **is** its `personGroupId`.
- `GET /people` lists a person only if named or with ≥ `minimumFaces` (user
  preference, default 3) visible faces on timeline assets. 835 of the 1,491
  people are small unnamed clusters it never lists. Use the database, not the
  API, for completeness.

## The library at snapshot time (2026-10-09)

| | |
|---|---|
| Assets | 100,953 (two owners) |
| Faces | 73,922 ML assigned · 32,723 ML unassigned · 55 manual |
| Your people | 1,491 — 88 named, 11 with birthday, 17 hidden |
| Second user's people | 183, none named |
| `People/*` tags | 88 tags on 31,076 assets (Google Takeout) |

---

## Export commands (on the Immich host)

```bash
mkdir -p ~/immich-faces-<label> && cd ~/immich-faces-<label>

# Full backup — the undo-everything button. Only needed before the reset.
docker exec -t immich_postgres pg_dumpall --clean --if-exists -U postgres \
  | gzip > immich-full-$(date +%F).sql.gz

for t in person person_group asset_face tag tag_asset; do
  docker exec immich_postgres psql -U postgres -d immich \
    -c "\copy \"$t\" to stdout with csv header" > "$t.csv"
done
docker exec immich_postgres psql -U postgres -d immich \
  -c "\copy (select id, \"ownerId\", \"originalFileName\", \"originalPath\", type, visibility, \"deletedAt\" from asset) to stdout with csv header" > asset.csv
```

Copy into `snapshot/` (before) or `after/` (after).

---

## Stages

```
0  preflight     key scopes, server version, ML queues            read-only
1  import        CSVs -> data/<label>.db, cross-checked with API   read-only
   ── you: regenerate people in the cluster group, wait until idle ──
1  import        --label after
2  serve         local web app: pair people, verify tags, review   writes, journaled
3  undo          replay the journal backwards                      writes
```

### 0 · preflight
API key scopes via `GET /api-keys/me`, server version, ML queue counts.

### 1 · import — **done for `before`**
Loads each CSV into SQLite as text, indexes the join columns, then refuses to
write the database unless:
- every CSV row landed,
- every reference resolves (face → asset, face → person group, tag link → tag),
- the API's people match `person.csv` (ids, names, birthdays, flags; unlisted
  people must all be unnamed),
- 40 random assets have the same face ids assigned to the same people live.

Also saves every one of your people's thumbnails to `data/thumbs/<label>/` —
the reset deletes them, and they are the quickest way to recognise a cluster.

### 2 · the web app (`serve.py`, localhost only)

Keyboard-driven. Face crops are cut from the asset preview by the stored
bounding box. Three views:

**People pairing — restores names.** For each old named person A and new
cluster B, over the faces A had:
- *coverage* — share of A's faces that landed in B
- *purity* — share of B's faces (yours, from before) that belonged to A rather
  than to another named person. Faces that were unassigned before, and the
  second user's faces, are neutral: they are what the reset is meant to add.

A pairing is **confident** when coverage ≥ 90 %, purity ≥ 98 %, it is
one-to-one, and A had ≥ 10 faces. Confident pairings are listed with a face
strip each and applied with one confirmation; everything else — splits (A
spread over several new clusters → merge), blends (B holding two old people),
the 13 manual-face survivors — is decided one at a time. Thresholds are
constants, tuned on the real numbers once `after` exists.

**Google tags.** For each `People/Firstname Lastname` tag: proposed Immich
person by name (confirm or alias), then the assets where tag and face disagree.
Verdict: trustworthy or not. Only trusted tags count as a third opinion below.

**Face review.** Every face whose new person is not the pairing of its old
person: changed person, now unassigned, newly assigned. Shows old, new and the
tag's opinion; you pick, the app reassigns (`PUT /faces/{id}`).

### 3 · undo

Every write is appended to `data/journal.jsonl` *before* it is sent, with the
before and after values, and verified after (Immich writes have quietly no-op'd
before — see immich-av1 `DEFECTS.md`).

| Action | Reversible? |
|---|---|
| rename, birthday, hidden, favourite | yes — write the old value back |
| reassign a face | yes — the old person is in the journal |
| merge two clusters | not in Immich; rebuilt from the journal by reassigning faces |
| anything else | `pg_dumpall` restore — the whole instance, a last resort |

---

## Rules carried over from immich-av1

- Follow pagination to the last page; a short page is not missing data.
- Verify every write and retry; Immich has reported success without acting.
- Compare only when the ML queues are idle.
- Compare sets per face, never "had people / has none".
