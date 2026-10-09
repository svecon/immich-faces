#!/usr/bin/env python3
"""Stage 0 -- check everything the later stages assume, change nothing.

  - the API key works, and which of the project's scopes it lacks
  - the server version (the schema this project reads is v3.3's)
  - the ML queues, and whether they are idle
"""

import lib


def main():
    ok = True

    try:
        version = lib.api("GET", "/server/version")
    except Exception as e:
        lib.die(f"cannot reach {lib.BASE_URL}: {e}")
    v = f"{version['major']}.{version['minor']}.{version['patch']}"
    lib.log(f"server {lib.BASE_URL} is v{v}")
    if (version["major"], version["minor"]) < (3, 3):
        lib.log("  !! older than v3.3: person_group and personGroupId may not exist")
        ok = False

    me = lib.api("GET", "/api-keys/me")
    granted = set(me["permissions"])
    lib.log(f"API key '{me['name']}': {len(granted)} permission(s)")
    if "all" in granted:
        lib.log("  key has every permission")
    else:
        missing_read = [s for s in lib.READ_SCOPES if s not in granted]
        missing_write = [s for s in lib.WRITE_SCOPES if s not in granted]
        if missing_read:
            lib.log(f"  !! missing, needed now (stage 1): {', '.join(missing_read)}")
            ok = False
        if missing_write:
            lib.log(f"  missing, needed later (stage 4 writes): {', '.join(missing_write)}")
        if not (granted & {"queue.read", "job.read"}):
            lib.log("  !! missing queue.read (or job.read): cannot tell when the "
                    "ML queues are idle")
            ok = False

    try:
        state = lib.queue_state()
        for name, s in state.items():
            lib.log(f"  queue {name:20} active {s['active']:>6}  waiting {s['waiting']:>6}  "
                    f"delayed {s['delayed']:>4}  failed {s['failed']:>4}"
                    + ("  PAUSED" if s["isPaused"] else ""))
        lib.log("ML queues idle" if lib.ml_idle() else "ML queues BUSY -- wait before comparing")
    except Exception as e:
        lib.log(f"  !! cannot read queues: {e}")
        ok = False

    lib.log("preflight OK" if ok else "preflight found problems (see !! above)")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
