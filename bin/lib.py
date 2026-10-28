"""Shared library for the Immich face-reset audit.

Python 3 stdlib only. See ../PLAN.md for why each rule exists.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_URL = os.environ.get("IMMICH_URL", "https://photos.home.svec.app").rstrip("/")
KEY_FILE = Path(os.environ.get("IMMICH_KEY_FILE", "~/.immich-key")).expanduser()
ROOT = Path(os.environ.get("IMMICH_FACES_ROOT", "~/devel/immich-faces")).expanduser()

# Everything under DATA is personal (names, birthdays, faces) and is kept out
# of git by the whitelist .gitignore.
DATA = ROOT / "data"

# The CSVs exported from Postgres. One set before the reset, one after; the
# importer is the same for both, so the two databases have identical shape.
TABLES = ["asset", "asset_face", "person", "person_group", "tag", "tag_asset"]

# Scopes the whole project needs. The read ones gate stage 1; the write ones
# are only needed once names are restored and faces reassigned (stage 4).
READ_SCOPES = ["person.read", "face.read", "asset.read", "asset.view",
               "tag.read"]
WRITE_SCOPES = ["person.update", "person.merge", "face.update", "face.delete",
                "asset.update",
                "asset.edit.get", "asset.edit.create", "asset.edit.delete"]

# Queues that can still be moving faces between people. Comparing while any
# of them is busy reports work in progress as discrepancies.
ML_QUEUES = ("thumbnailGeneration", "faceDetection", "facialRecognition")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# --------------------------------------------------------------------------
# Immich API
# --------------------------------------------------------------------------

def api_key() -> str:
    if not KEY_FILE.exists():
        die(f"{KEY_FILE} not found. Create it with:\n"
            f"  printf '%s' 'YOUR_KEY' > {KEY_FILE} && chmod 600 {KEY_FILE}")
    key = KEY_FILE.read_text().strip()
    if not key:
        die(f"{KEY_FILE} is empty")
    return key


def api(method: str, path: str, body=None, raw=False, timeout=120):
    """Call the Immich API. `path` starts with '/'.

    Distinguish the two auth failures when reading errors:
      'Invalid API key'                -> the key string is wrong
      'Missing required permission: X' -> valid key, missing scope
    """
    url = f"{BASE_URL}/api{path}"
    data = None
    headers = {"x-api-key": api_key(), "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            if raw:
                return payload
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from None


def all_people(with_hidden=True) -> list[dict]:
    """Every person visible to the key's user, following pagination to the end.

    Pagination has been misread as missing data twice in immich-av1; never
    trust a single page.
    """
    people, page = [], 1
    while True:
        r = api("GET", f"/people?withHidden={'true' if with_hidden else 'false'}"
                       f"&size=1000&page={page}")
        people += r["people"]
        if not r.get("hasNextPage"):
            return people
        page += 1


def queue_state() -> dict[str, dict]:
    """{queue: statistics} for the ML queues. Raises if it cannot tell.

    /queues needs `queue.read`; the immich-av1 key only has `job.read`, which
    still reaches the deprecated /jobs. Either is fine.
    """
    try:
        queues = api("GET", "/queues")
        return {q["name"]: {**q["statistics"], "isPaused": q["isPaused"]}
                for q in queues if q["name"] in ML_QUEUES}
    except RuntimeError as e:
        if "HTTP 403" not in str(e):
            raise
    jobs = api("GET", "/jobs")
    return {name: {**jobs[name]["jobCounts"],
                   "isPaused": jobs[name]["queueStatus"].get("isPaused", False)}
            for name in ML_QUEUES if name in jobs}


def min_detection_score() -> float:
    """The live face detection threshold (needs `systemConfig.read`)."""
    return api("GET", "/system-config")["machineLearning"]["facialRecognition"]["minScore"]


def live_faces(asset_id: str) -> list[dict]:
    """An asset's faces as Immich has them now, in the shape the web app uses."""
    return [{"id": f["id"], "asset": asset_id, "source": f.get("sourceType"),
             "box": [f["boundingBoxX1"], f["boundingBoxY1"], f["boundingBoxX2"], f["boundingBoxY2"]],
             "size": [f["imageWidth"], f["imageHeight"]],
             "person": (f.get("person") or {}).get("id"),
             "personName": (f.get("person") or {}).get("name") or ""}
            for f in api("GET", f"/faces?id={asset_id}")]


def ml_idle() -> bool:
    """True when nothing is queued that could still move a face."""
    try:
        state = queue_state()
    except Exception:
        return False            # cannot tell -- assume still working
    return all(not (s["active"] or s["waiting"] or s["delayed"])
               for s in state.values())


# --------------------------------------------------------------------------
# Snapshot databases
# --------------------------------------------------------------------------

def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    """Postgres `\\copy ... csv header` output: header row, then data.

    Kept as text. NULL and the empty string both arrive as '' -- an unnamed
    person has name '' anyway, and every nullable column used here (personGroupId,
    birthDate, deletedAt) means the same thing either way.
    """
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        die(f"{path} is empty -- the export failed")
    return rows[0], rows[1:]
