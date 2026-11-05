# tools/propfeed/convert.py
# Purpose: [TOOLS] The propagation map's SKY FEED: KC2G's real-time ionosphere map, cut down to one small file NB can read.
# APIs: build, crop, encode, horizon_s, main, plan
# Uses: None
"""The propagation map's SKY FEED: KC2G's real-time ionosphere map, cut down to one small file NB can read.

THE OPERATOR'S DESIGN (2026-11-05). prop.kc2g.com blends the few live ionosondes (GIRO) with the
IRI model into a 1-degree world map of foF2, MUF(3000) and hmF2 every 15 minutes, with forecasts
ahead -- far better than "the nearest live station", which for most of the US is Idaho, 1,200 mi
away. But the map is HDF5 compressed with SZ (filter 32017): reading it needs h5py + hdf5plugin +
numpy, native code that would turn every NB patch into a full installer. So THIS script, not the
app, reads it: a scheduled GitHub job on the public releases repo runs it and replaces ONE release
asset (propfeed.json.gz). Every NB reads that file -- never KC2G's server -- with the stdlib only
(engine/prop_feed.py). "The job touching his server is way better than every NB client touching it."

SCHEDULE (the workflow, propfeed.yml; PEAK_UTC_HOURS here must agree -- tests/test_prop_feed.py):
  peak, 15Z-06Z (9 AM-midnight Central, from NB's own telemetry of who is on a net): every 30 min,
  at :13 and :43 -- off the hour (GitHub delays the top of the hour) and just after KC2G's :10 and
  :40 runs. Off-peak: every 2 hours.
STEPS: 30 minutes apart, from the newest map at or before now out to HORIZON (60 min peak, 150 min
off-peak), so a file is never stale before the next run lands; NB interpolates between steps.
REGION: North America and the Pacific out to Hawaii (lat 0..75, lon -180..-40).

ENCODING (version 1; engine/prop_feed.decode is the reader, and the test round-trips the two):
each field is one byte per grid point, row-major from (lat0, lon0), base64:
  fof2 = MHz x 10 (0.1 MHz, to 25.5)   mufd = MHz x 4 (0.25 MHz, to 63.75 -- past 6m's 50)
  hmf2 = km / 4 (to 1020)
  0 means "no value".

Run locally:  python tools/propfeed/convert.py --out propfeed.json.gz   (needs h5py hdf5plugin numpy)
"""
import argparse
import base64
import gzip
import io
import json
import time
import urllib.request

API = "https://prop.kc2g.com/api"
UA = "NetBuddy-propfeed (github.com/NmX69/nl-net-buddy-releases)"
VERSION = 1
LAT0, LAT1, LON0, LON1 = 0, 75, -180, -40
STEP_S = 30 * 60
PEAK_UTC_HOURS = frozenset(list(range(15, 24)) + list(range(0, 6)))
HORIZON_PEAK_S, HORIZON_OFFPEAK_S = 60 * 60, 150 * 60
SCALE = {"fof2": 10.0, "mufd": 4.0, "hmf2": 0.25}


def horizon_s(now):
    return HORIZON_PEAK_S if time.gmtime(now).tm_hour in PEAK_UTC_HOURS else HORIZON_OFFPEAK_S


def plan(available, now, horizon):
    """[(run_id, ts)]: the newest map at or before `now`, then the map nearest every STEP_S after it
    up to `horizon` -- each time from the newest run that produced it. `available` is KC2G's
    available_maps.json (a list of {run_id, ts})."""
    best = {}
    for m in available:
        ts, rid = int(m["ts"]), int(m["run_id"])
        if rid > best.get(ts, -1):
            best[ts] = rid
    times = sorted(best)
    past = [t for t in times if t <= now]
    if not past:
        return []
    base, out = past[-1], []
    out.append((best[base], base))
    want = base + STEP_S
    while want <= now + horizon:
        near = min(times, key=lambda t: abs(t - want))
        if abs(near - want) <= 8 * 60 and near > out[-1][1]:
            out.append((best[near], near))
        want += STEP_S
    return out


def encode(grid, name):
    """One field, already cropped to the region as rows of floats (NaN = none), to base64 bytes."""
    k = SCALE[name]
    raw = bytearray()
    for row in grid:
        for v in row:
            raw.append(0 if v != v or v <= 0 else max(1, min(255, int(round(v * k)))))
    return base64.b64encode(bytes(raw)).decode("ascii")


def crop(field):
    """The region out of a (181, 361) world grid indexed [lat + 90][lon + 180]."""
    return [[float(field[lat + 90][lon + 180]) for lon in range(LON0, LON1 + 1)] for lat in range(LAT0, LAT1 + 1)]


def build(steps, now):
    """The feed document. `steps` is [(run_id, ts, {fof2, mufd, hmf2: world grid})]."""
    return {"v": VERSION, "source": "prop.kc2g.com (KC2G): GIRO ionosondes + IRI, assimilated",
            "made": int(now), "lat0": LAT0, "lat1": LAT1, "lon0": LON0, "lon1": LON1,
            "steps": [dict({"run_id": rid, "ts": ts}, **{n: encode(crop(f[n]), n) for n in SCALE})
                      for rid, ts, f in steps]}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    import h5py                                          # noqa: F401 -- CI only, see the docstring
    import hdf5plugin                                    # noqa: F401 -- registers the SZ filter
    now = int(time.time())
    available = json.loads(_get(API + "/available_maps.json?past_hours=2&future_hours=4"))
    steps = []
    for rid, ts in plan(available, now, horizon_s(now)):
        f = h5py.File(io.BytesIO(_get("%s/assimilated.h5?run_id=%d&ts=%d" % (API, rid, ts))), "r")
        steps.append((rid, ts, {n: f["maps/" + n][()] for n in SCALE}))
    if not steps:
        raise SystemExit("KC2G listed no map at or before now")
    body = gzip.compress(json.dumps(build(steps, now), separators=(",", ":")).encode("utf-8"))
    with open(a.out, "wb") as fh:
        fh.write(body)
    print("wrote %s: %d steps (%s), %d bytes" % (a.out, len(steps), ", ".join(
        time.strftime("%H:%MZ", time.gmtime(ts)) for _, ts, _ in steps), len(body)))


if __name__ == "__main__":
    main()
