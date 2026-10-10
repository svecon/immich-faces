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
sudo docker exec -t immich_postgres pg_dumpall --clean --if-exists -U postgres \
  | gzip > immich-full-$(date +%F).sql.gz

for t in person person_group asset_face tag tag_asset; do
  sudo docker exec immich_postgres psql -U postgres -d immich \
    -c "\copy \"$t\" to stdout with csv header" > "$t.csv"
done
sudo docker exec immich_postgres psql -U postgres -d immich \
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
| delete a face ("not a face") | no — Immich cannot restore a face; the journal keeps the box |
| faces added by a refresh | yes — delete them (`DELETE /faces/{id}`) |
| anything else | `pg_dumpall` restore — the whole instance, a last resort |

### 4 · missing faces of tagged people — **planned**

Google `People/…` tags are reliable, but on many tagged photos Immich never
detected the tagged person: too small, blurred, turned away, in the background.
The tag is right and the face is missing.

**Size, from the snapshots.** 25,736 assets carry a `People/…` tag; 3,611 of
them have more tags than detected faces (4,713 faces short, 2,557 assets with
no face at all, 3,043 short by exactly one). Of those, 854 are videos and
2,757 photos.

Two different problems, two passes:

| | Assets | Cause | Fix |
|---|---|---|---|
| A · fewer faces than tags | ~2,760 photos | face not detected | refresh faces at a lower threshold |
| B · enough faces, none is the tagged person | ~3,800 | face detected, on the wrong person or nobody | move the face (review, no detection) |

Videos are out of scope: Immich detects faces on the preview frame only, so a
person elsewhere in the clip is never found.

#### How face detection behaves — read from the v3.3.1 source

- **Face Detection → All** (`force: true`) deletes every machine-learning face,
  then every person left without faces — names and birthdays with them. This
  is the reset. **Never run it.**
- **Face Detection → Missing** only scans assets never scanned. No help here.
- **Refresh faces on one asset** (`POST /assets/jobs {name: "refresh-faces",
  assetIds}`, or the photo's menu) diffs per asset: a detection overlapping an
  existing face by IoU > 0.5 keeps that face's id and person; a new detection
  is added and queued for recognition (joins the nearest person if close
  enough, else unassigned); an existing ML face not detected again is removed.
  Safe for names and earlier work.
- The minimum detection score is a global setting, so while it is lowered,
  new uploads pick up junk too. Lower it only for the run.
- Deleted faces are likely ignored by the diff, so a face deleted as "not a
  face" may come back on a refreshed photo. Delete junk after the refresh.
- Faces added by a refresh are real ML faces with an embedding, so they also
  help Immich recognise the person later — unlike a box drawn by hand.

#### Pass A — refresh faces at a lower threshold

1. **Select** photos (not videos) where trusted `People/…` tags outnumber the
   live faces. Counted against the live server, not the snapshot.
2. **Record** each photo's current faces (id, box, person) in the journal: the
   baseline for the diff and for undo.
3. **Trial** on one tag, ~50 photos, before the full run.
4. **You lower** *Administration → Machine Learning → Facial Recognition →
   Min detection score* from 0.7 to ~0.4. The script reads the setting back
   and refuses to start otherwise.
5. **Refresh** the selected photos in batches through `POST /assets/jobs`,
   journaled. Wait until the face detection and recognition queues are idle.
6. **You restore** the score to 0.7. The script checks it before step 7.
7. **Diff** each photo against its baseline: faces added, faces removed
   (should be none — flagged if any).
8. **Review** in the web app, a new view grouped by tag. Per added face: the
   crop, the whole photo, where recognition put it, and its similarity to the
   tagged person's centroid. Actions:
   - *is the tagged person* → assign (`PUT /faces/{person}`), unless
     recognition already did;
   - *someone else* → Move to…;
   - *not a face / unknown* → delete (undoable here — these faces did not
     exist before).
   Faces most similar to the tagged person come first and are preselected.
9. **Report** what is still missing: photos where even 0.4 found nothing new.
   Left alone — nothing more can be detected.

#### Pass B — detected but misassigned

For tagged photos where the face count is enough but none is the tagged
person: show the photo's faces that are unassigned or on an unnamed cluster,
ranked by similarity to the tagged person's centroid; confirm → move. Uses the
existing face-move path; no detection, no settings change.

#### Needed

- API key permissions: `asset.update` (refresh), `face.update` (have),
  `face.delete` (delete added junk, undo).
- Tags marked trusted in the Tags view; none are yet. Untrusted tags are left
  out of both passes.
- Similarity uses `after/centroids.csv`. After the refresh, re-export
  `face_search` for the added faces only (command to be written).

#### Undo

Every refresh is undoable from the journal: delete the faces the diff found
added. Assignments and moves reverse as usual. Faces the refresh removed
cannot come back — the trial run shows whether that happens at all.

---

## Rules carried over from immich-av1

- Follow pagination to the last page; a short page is not missing data.
- Verify every write and retry; Immich has reported success without acting.
- Compare only when the ML queues are idle.
- Compare sets per face, never "had people / has none".
