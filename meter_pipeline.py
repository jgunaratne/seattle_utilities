#!/usr/bin/env python3
"""
NextCentury water-meter -> Seattle utility bill -> Google Sheet pipeline.

This script is CHECKPOINTED: every step writes its result to STATE_FILE
(meter_state.json) so that if the machine crashes mid-run, re-running the
script resumes from the last completed step instead of starting over.

Discovered facts (so they are never lost again):
  * Base URL:        https://api.nextcenturymeters.com
  * Auth:            POST /login  {email,password,deviceId}  header 'version: 2'
                     -> JSON {success, user{...}, token}   (token is a JWT)
                     All authed calls send header  authorization: <token>
  * Property:        configured via NC_PROPERTY_ID in the conf file (looks like p_XXXXX)
  * Units endpoint:  GET /api/Properties/{pid}/Units  -> [{_id,name,meters[...]}]
  * Reads endpoint:  GET /api/Units/{unitId}/DailyReads?from={ISO}&to={ISO}
                     -> [{date:"YYYYMMDD", data:[{pulseCount,multiplier,...}, ...]}]
                     (the /api/Devices/{id}/reads route is 403/Forbidden for this
                      property-manager account; DailyReads is what the web app uses)
  * Meter reading  = pulseCount * multiplier   (cumulative; resetCount is just a
                     power-cycle counter and does NOT need to be added in).
                     Representative reading for a day = LAST check-in of that day.

  * Units of interest: configured via NC_UNIT_NAMES in the conf file.

Usage:
  python3 meter_pipeline.py readings --start 2026-03-26 --end 2026-05-26
  python3 meter_pipeline.py show          # print current saved state
"""

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
import urllib.error

BASE = "https://api.nextcenturymeters.com"
CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "nextcentury_credentials.conf")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meter_state.json")
def _read_conf():
    """Parse the KEY="value" lines from the conf file. Tolerates a missing file
    so the module still imports on a fresh checkout (before you create the conf)."""
    c = {}
    try:
        with open(CONF) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    c[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return c


_CONF = _read_conf()
PROPERTY_ID = _CONF.get("NC_PROPERTY_ID", "")
UNIT_NAMES = [u.strip() for u in _CONF.get("NC_UNIT_NAMES", "").split(",") if u.strip()]


# ----------------------------------------------------------------------------- state
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"updated": None, "units": {}, "readings": {}, "bill": {}, "table": {}}


def save_state(state):
    state["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)  # atomic; survives a crash mid-write
    print(f"  [checkpoint] saved -> {STATE_FILE}")


# ----------------------------------------------------------------------------- http
def _req(path, token=None, method="GET", body=None):
    url = BASE + path
    data = None
    headers = {"version": "2"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["authorization"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def login():
    creds = {}
    with open(CONF) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip().strip('"').strip("'")
    import uuid
    payload = {
        "email": creds["NC_EMAIL"],
        "password": creds["NC_PASSWORD"],
        "deviceId": str(uuid.uuid4()),
    }
    resp = _req("/login", method="POST", body=payload)
    token = resp.get("token") or (resp.get("user") or {}).get("token")
    if not token:
        raise RuntimeError(f"login failed: {json.dumps(resp)[:300]}")
    print(f"  login OK as {creds['NC_EMAIL']}")
    return token


def get_units(token):
    units = _req(f"/api/Properties/{PROPERTY_ID}/Units", token=token)
    name_to_id = {u["name"]: u["_id"] for u in units}
    return name_to_id


def get_daily_reads(token, unit_id, start, end):
    """start/end are date objects. Returns the raw DailyReads list."""
    frm = dt.datetime(start.year, start.month, start.day).strftime("%Y-%m-%dT00:00:00.000Z")
    to = dt.datetime(end.year, end.month, end.day).strftime("%Y-%m-%dT23:59:59.000Z")
    path = f"/api/Units/{unit_id}/DailyReads?from={frm}&to={to}"
    return _req(path, token=token)


def reading_on(daily_reads, target):
    """Cumulative meter reading on `target` date (date obj).
    Picks the DailyReads doc for that day and uses its LAST check-in.
    If the exact day is missing, falls back to the closest earlier day."""
    key = target.strftime("%Y%m%d")
    by_date = {d["date"]: d for d in daily_reads if d.get("data")}
    doc = by_date.get(key)
    if doc is None:
        earlier = sorted(k for k in by_date if k <= key)
        if not earlier:
            return None, None
        doc = by_date[earlier[-1]]
    last = doc["data"][-1]
    reading = last["pulseCount"] * last["multiplier"]
    return reading, doc["date"]


# ----------------------------------------------------------------------------- commands
def cmd_readings(args):
    if not PROPERTY_ID:
        raise SystemExit("missing NC_PROPERTY_ID in config/nextcentury_credentials.conf")
    if not UNIT_NAMES:
        raise SystemExit("missing NC_UNIT_NAMES in config/nextcentury_credentials.conf")

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    state = load_state()

    token = login()
    state["units"] = get_units(token)
    save_state(state)

    for name in UNIT_NAMES:
        uid = state["units"].get(name)
        if not uid:
            print(f"  !! unit {name} not found")
            continue
        # widen window a few days so the exact billing dates are covered
        reads = get_daily_reads(token, uid, start - dt.timedelta(days=5), end + dt.timedelta(days=2))
        sr, sd = reading_on(reads, start)
        er, ed = reading_on(reads, end)
        state["readings"].setdefault(args.start, {})[name] = sr
        state["readings"].setdefault(args.end, {})[name] = er
        usage = (er - sr) if (sr is not None and er is not None) else None
        print(f"  {name}: start({args.start}->{sd})={sr}  end({args.end}->{ed})={er}  usage={usage}")
        save_state(state)  # checkpoint after every unit

    # summary table
    s, e = state["readings"].get(args.start, {}), state["readings"].get(args.end, {})
    usage = {n: (e.get(n) - s.get(n)) for n in UNIT_NAMES if s.get(n) is not None and e.get(n) is not None}
    total = sum(usage.values()) if usage else None
    state["table"] = {
        "start_date": args.start, "end_date": args.end,
        "start_reads": s, "end_reads": e,
        "usage": usage, "total_usage": total,
        "percent": {n: round(100 * usage[n] / total, 2) for n in usage} if total else {},
    }
    save_state(state)
    print("\n=== USAGE SUMMARY ===")
    print(json.dumps(state["table"], indent=2))


def cmd_show(args):
    print(json.dumps(load_state(), indent=2))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("readings", help="fetch meter readings for a billing period")
    r.add_argument("--start", required=True, help="billing period start YYYY-MM-DD")
    r.add_argument("--end", required=True, help="billing period end YYYY-MM-DD")
    r.set_defaults(func=cmd_readings)
    s = sub.add_parser("show", help="print saved state")
    s.set_defaults(func=cmd_show)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
