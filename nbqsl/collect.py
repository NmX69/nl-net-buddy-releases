# tools/nbqsl/collect.py
# Purpose: [TOOLS] The nbQSL COLLECTOR: move the community inbox (ntfy) into the public ledger -- 72-hour area files, the monthly archive and a fingerprinted index, in a checkout of the ledger branch.
# APIs: archive_file, archive_merge, area_file, build_index, fingerprint, main, merge, pack, read_inbox, unpack
# Uses: engine/nbqsl.py
"""THE nbQSL COLLECTOR. Design owner: docs/NB_QSL.md, section 3 (operator, 2026-11-23: "clients drop
to ntfy... the community ledger can sit on github using cron").

Runs on the PUBLIC releases repo as a GitHub Actions job (tools/nbqsl/nbqsl.yml), started hourly by
the cron-job.org timer with GitHub's own schedule as a backup -- the sky feed's arrangement. It is
installed there by tools/publish_nbqsl.py, which also copies engine/nbqsl.py beside it as
nbqsl_core.py: every rule about an entry (what is valid, which file it goes in, how long it is
kept) is the ENGINE's, so the Client, the card page and the collector cannot disagree.

THE LEDGER IS A FOLDER (--data): a checkout of the ledger branch (engine/nbqsl.LEDGER_BRANCH). The
job hands it in, this updates it in place, and the job replaces the branch with ONE commit of the
result, so the branch never grows. A web page can read a branch file (raw.githubusercontent.com sends
the CORS header) and cannot read a release file -- which is why the ledger is not on a release.

ONE RUN:
  1. read index.json and the live area files from the folder;
  2. read everything posted to the community inbox since the index's checkpoint (ntfy keeps ~12 h;
     an hourly job misses nothing);
  3. check each entry (core.valid_entry), drop duplicates, cap each call per day;
  4. add them to their area file (core.area_key of the RECIPIENT), drop anything older than
     REPLY_WINDOW_H, sort by recipient; delete an area file that emptied;
  5. add them to the ARCHIVE, archive/<QSO month>/<area>.json -- the permanent record the card page
     looks a card up in ("The point of qsl is to be verifiable", operator, 2026-11-23);
  6. write the index with a fingerprint per live file and the list of archive months.

STDLIB ONLY, so the job installs nothing.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import nbqsl_core as core                       # on the releases repo (tools/publish_nbqsl.py)
except ImportError:                                 # from this repo
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
    from engine import nbqsl as core

ARCHIVE = "archive"
#: A call may put this many entries in the ledger per UTC day; past it, the rest are dropped. Normal
#: use is a few dozen on a busy net.
MAX_PER_CALL_DAY = 300
UA = {"User-Agent": "nbqsl-collector"}


def area_file(area):
    return "%s.json" % area


def archive_file(month, area):
    """The archive file for one QSO month and one recipient area, relative to the ledger folder."""
    return "%s/%s/%s.json" % (ARCHIVE, month, area)


def pack(entries):
    """The bytes of one ledger file: compact JSON with sorted keys, so identical content gives
    identical bytes (the fingerprint must not change when nothing did)."""
    return json.dumps(entries, separators=(",", ":"), sort_keys=True).encode("utf-8")


def unpack(blob):
    return json.loads(blob.decode("utf-8")) if blob else []


def fingerprint(entries):
    return hashlib.sha256(pack(entries)).hexdigest()[:16]


def _key(e):
    return (e["id"], e["k"], e["from"])


def merge(files, messages, now):
    """Add inbox `messages` [(ntfy_time, body_text), ...] to `files` {area: [entries]}.

    Returns (files, accepted, newest_time). Every entry is checked by the engine, stamped with the
    INBOX's time (`t`, never the poster's clock), deduplicated and capped per call per day; then
    every file is pruned to the reply window and sorted by recipient."""
    seen = {_key(e) for es in files.values() for e in es}
    per_day = {}
    for es in files.values():
        for e in es:
            d = (e["from"], time.strftime("%Y%m%d", time.gmtime(e.get("t", 0))))
            per_day[d] = per_day.get(d, 0) + 1
    accepted, newest = [], 0
    for t, body in messages:
        newest = max(newest, int(t or 0))
        try:
            e = core.valid_entry(json.loads(body))
        except (ValueError, TypeError):
            e = None
        if not e or _key(e) in seen:
            continue
        e["t"] = int(t)
        d = (e["from"], time.strftime("%Y%m%d", time.gmtime(e["t"])))
        if per_day.get(d, 0) >= MAX_PER_CALL_DAY:
            continue
        per_day[d] = per_day.get(d, 0) + 1
        seen.add(_key(e))
        files.setdefault(core.area_key(e["to"]), []).append(e)
        accepted.append(e)
    cutoff = now - core.REPLY_WINDOW_H * 3600
    for area in list(files):
        files[area] = sorted((e for e in files[area] if e.get("t", 0) >= cutoff),
                             key=lambda e: (e["to"], e["t"], e["id"], e["k"]))
        if not files[area]:
            del files[area]
    return files, accepted, newest


def archive_merge(old, new):
    """One archive file plus the new entries: nothing is ever pruned here."""
    have = {_key(e) for e in old}
    out = list(old) + [e for e in new if _key(e) not in have]
    return sorted(out, key=lambda e: (e["to"], e["t"], e["id"], e["k"]))


def build_index(files, since, now, archive_months):
    """The tiny file every client reads first: each live area file with its count and fingerprint,
    the inbox checkpoint, and which archive months exist."""
    return {"v": core.ENTRY_V, "updated": int(now), "since": int(since), "window_h": core.REPLY_WINDOW_H,
            "files": {a: {"n": len(es), "fp": fingerprint(es)} for a, es in sorted(files.items())},
            "archive": sorted(archive_months)}


# ---- I/O: only main() and these touch the network or the folder ----------------------------------

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def read_inbox(since):
    """Everything in the community inbox since `since` (unix seconds; 0 = all ntfy still holds)."""
    url = "%s/%s/json?poll=1&since=%s" % (core.NTFY_BASE, core.INBOX_TOPIC, int(since) if since else "all")
    body = _get(url) or b""
    out = []
    for line in body.decode("utf-8", "replace").splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("event") == "message" and m.get("message"):
            out.append((int(m.get("time") or 0), m["message"]))
    return out


def _read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _write(path, blob):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(blob)


def main(argv=None, messages=None, now=None):
    """One run over the ledger folder. `messages`/`now` stand in for the inbox and the clock in tests."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True, help="the ledger folder (a checkout of the ledger branch)")
    args = ap.parse_args(argv)
    now = int(now if now is not None else time.time())
    data = args.data
    os.makedirs(data, exist_ok=True)

    idx = json.loads((_read(os.path.join(data, core.LEDGER_INDEX)) or b"{}").decode("utf-8") or "{}")
    files = {a: unpack(_read(os.path.join(data, area_file(a)))) for a in idx.get("files", {})}
    before = set(files)
    since = int(idx.get("since") or 0)
    msgs = read_inbox(since) if messages is None else messages
    files, accepted, newest = merge(files, msgs, now)

    touched = {}
    for e in accepted:
        touched.setdefault((core.archive_key(e["utc"]), core.area_key(e["to"])), []).append(e)
    for (month, area), new in touched.items():
        path = os.path.join(data, archive_file(month, area))
        _write(path, pack(archive_merge(unpack(_read(path)), new)))

    for area, es in files.items():
        _write(os.path.join(data, area_file(area)), pack(es))
    for area in before - set(files):                  # emptied past the window
        try:
            os.remove(os.path.join(data, area_file(area)))
        except OSError:
            pass
    arch = os.path.join(data, ARCHIVE)
    months = set(os.listdir(arch)) if os.path.isdir(arch) else set()
    # THE CHECKPOINT never moves backwards, and a quiet hour keeps the old one.
    index = build_index(files, max(since, newest), now, months)
    with open(os.path.join(data, core.LEDGER_INDEX), "w", encoding="utf-8") as fh:
        json.dump(index, fh, separators=(",", ":"), sort_keys=True)
    print("inbox %d, accepted %d, areas %d, archive %s" % (
        len(msgs), len(accepted), len(files), ",".join("%s/%s" % k for k in sorted(touched)) or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
