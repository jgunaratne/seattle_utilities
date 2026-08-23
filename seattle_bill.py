#!/usr/bin/env python3
"""
Seattle MyUtilities (SPU) bill fetcher — pure HTTP, no browser.

Logs into https://myutilities.seattle.gov over the full SAML/Oracle-IDCS flow that the
SPA performs in a browser, obtains the eportal bearer token, and pulls account/bill data.
See PIPELINE_NOTES.md for the reverse-engineered flow. Designed to be memory-light on the
Pi: never loads big JS bundles; only small HTML auth pages.

Usage:
  python3 seattle_bill.py login        # run full login, save bearer token to state
  python3 seattle_bill.py discover     # with a valid token, probe candidate bill endpoints
  python3 seattle_bill.py get <PATH>   # GET an authed /rest path and print JSON
  python3 seattle_bill.py split        # fetch bill, write spu_bill_split_<date>.csv
  python3 seattle_bill.py sheet        # split + write sheet_upload_<date>.csv + push to Google Sheet
"""
import os, re, sys, json, html, time, urllib.parse

import requests

# All data files live next to this script, so the whole folder can be moved/renamed freely.
BASE = os.path.dirname(os.path.abspath(__file__))
# Credentials, per-account settings, and the Google service-account key live in config/.
CONFIG_DIR = os.path.join(BASE, "config")
CONF = os.path.join(CONFIG_DIR, "nextcentury_credentials.conf")
STATE = os.path.join(BASE, "seattle_state.json")
# Generated billing CSVs are written here (created on demand) to keep them out of the repo root.
OUTPUT_DIR = os.path.join(BASE, "output")


def sa_keyfile(conf=None):
    """Resolve the Google service-account key path. A relative GOOGLE_SA_KEYFILE is taken
    relative to config/; falls back to config/google_service_account.json."""
    conf = load_conf() if conf is None else conf
    kf = conf.get("GOOGLE_SA_KEYFILE", "") or "google_service_account.json"
    return kf if os.path.isabs(kf) else os.path.join(CONFIG_DIR, kf)

EPORTAL = "https://myutilities.seattle.gov"
LOGIN_HOST = "https://login.seattle.gov"
UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def load_conf():
    c = {}
    try:
        with open(CONF) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    c[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return c


def load_creds():
    c = load_conf()
    return c["SEATTLE_UTIL_USERNAME"], c["SEATTLE_UTIL_PASSWORD"]


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, json.dumps(d, indent=2).encode())
    os.close(fd)


def parse_form(htmltext):
    """Return (action, {name: value}) for the first <form>, unescaping HTML entities."""
    m = re.search(r'<form[^>]*action="([^"]+)"', htmltext, re.I)
    if not m:
        return None, {}
    action = html.unescape(m.group(1))
    inputs = re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', htmltext, re.I)
    return action, {n: html.unescape(v) for n, v in inputs}


def extract_setitem(htmltext, key):
    m = re.search(r'setItem\(\s*["\']%s["\']\s*,\s*["\']?(.*?)["\']?\s*\)' % re.escape(key), htmltext)
    return m.group(1) if m else None


def login():
    user, pw = load_creds()
    s = requests.Session()
    s.headers.update({"User-Agent": UA})

    # 1) Kick off SSO; follows SAMLRequest redirect to the IDCS login page.
    r = s.get(EPORTAL + "/rest/auth/ssologin", timeout=60)
    action, fields = parse_form(r.text)            # IDCS /sso/v1/user/login form
    assert action and "login.seattle.gov" in action, f"unexpected IDCS step: {action!r}"

    # 2) Auto-submit IDCS form to login.seattle.gov -> page injects signinAT/initialState.
    r = s.post(LOGIN_HOST + "/", data=fields, timeout=60,
               headers={"Content-Type": "application/x-www-form-urlencoded"})
    signin_at = extract_setitem(r.text, "signinAT")
    initial_state = extract_setitem(r.text, "initialState")
    base_uri = extract_setitem(r.text, "baseUri")
    assert signin_at and initial_state and base_uri, "missing IDCS auth context from login page"

    # 3) Submit credentials. initialState MUST be a parsed object (carries requestState).
    body = {"initialState": json.loads(initial_state), "signinAT": signin_at,
            "credentials": {"username": user, "password": pw}}
    r = s.post(LOGIN_HOST + "/authenticate", json=body, timeout=60,
               headers={"Accept": "application/json", "Origin": LOGIN_HOST,
                        "Referer": LOGIN_HOST + "/"})
    auth = r.json()
    if auth.get("status") != "success":
        raise SystemExit(f"authenticate failed: {auth.get('status')} {auth.get('cause')}")
    authn_token = auth["authnToken"]

    # 4) Complete SAML: submit authnToken to IDCS, then follow the auto-submit form chain
    #    until we hit the eportal redirect carrying the /ssohome/<GUID> auth code.
    r = s.post(base_uri + "/sso/v1/sdk/session", data={"authnToken": authn_token}, timeout=60)
    guid = None
    for _ in range(8):
        action, fields = parse_form(r.text)
        if not action:
            break
        # Don't auto-follow the final redirect; we need its Location (has the #fragment).
        r = s.post(action, data=fields, timeout=60, allow_redirects=False,
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        # chase any plain redirects, but stop to inspect a ssohome fragment
        while r.is_redirect:
            loc = r.headers.get("Location", "")
            m = re.search(r"/ssohome/([0-9a-fA-F-]+)", loc)
            if m:
                guid = m.group(1)
                break
            r = s.send(r.next, allow_redirects=False, timeout=60)
        if guid:
            break
    assert guid, "did not reach /ssohome/<GUID> auth code"

    # 5) Exchange the single-use GUID for the eportal bearer token.
    r = s.post(EPORTAL + "/rest/auth/token", timeout=60,
               headers={"Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json"},
               data={"grant_type": "authorization_code", "usertoken": guid, "logintype": "sso"})
    tok = r.json()
    access_token = tok["access_token"]

    st = load_state()
    st.update({"access_token": access_token,
               "token_obtained": int(time.time()),
               "expires_in": tok.get("expires_in"),
               "ssohome_guid": guid})
    save_state(st)
    print("login OK; bearer token saved to", STATE)
    print("  expires_in:", tok.get("expires_in"), "scope:", tok.get("scope"))
    return access_token


def authed_session():
    st = load_state()
    tok = st.get("access_token")
    if not tok:
        raise SystemExit("no token in state; run `login` first")
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Authorization": "Bearer " + tok,
                      "Accept": "application/json"})
    return s


def discover():
    s = authed_session()
    candidates = [
        ("GET", "/rest/account/list"), ("POST", "/rest/account/list"),
        ("GET", "/rest/account/accounts"), ("POST", "/rest/account/accounts"),
        ("GET", "/rest/user/getuserinfo"), ("POST", "/rest/user/getuserinfo"),
        ("POST", "/rest/auth/account"), ("GET", "/rest/auth/account"),
        ("GET", "/rest/account/summary"), ("POST", "/rest/account/summary"),
        ("GET", "/rest/billing/summary"), ("POST", "/rest/billing/summary"),
        ("GET", "/rest/bill/list"), ("POST", "/rest/bill/list"),
        ("GET", "/rest/account/billinghistory"), ("POST", "/rest/account/billinghistory"),
    ]
    for meth, ep in candidates:
        try:
            r = s.request(meth, EPORTAL + ep, json={} if meth == "POST" else None, timeout=25)
            snippet = r.text[:120].replace("\n", " ")
            print(f"{r.status_code} {meth:4} {ep}  {snippet}")
        except Exception as e:
            print(f"ERR  {meth:4} {ep}  {e}")


def get(path):
    s = authed_session()
    r = s.get(EPORTAL + path, timeout=30)
    print("status", r.status_code)
    try:
        print(json.dumps(r.json(), indent=2)[:2000])
    except Exception:
        print(r.text[:1000])


SPU_ACCOUNT = load_conf().get("SPU_ACCOUNT_NUMBER", "")
HOA_FILE = os.path.join(CONFIG_DIR, "hoa_adjustments.json")


def load_hoa(path=HOA_FILE):
    """Optional per-unit HOA line items (Landscape, Admin fee, Extra garbage, ...).
    Format: { "Landscape": {"UNIT_1": 20.0, "UNIT_2": 0.0, "UNIT_3": -20.0}, ... }
    These are manual, vary each cycle, and typically net ~$0 across units.
    Returns {} if the file is absent."""
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def fetch_bill():
    """Return the current SPU bill summary dict (uses saved bearer)."""
    if not SPU_ACCOUNT:
        raise SystemExit("missing SPU_ACCOUNT_NUMBER in config/nextcentury_credentials.conf")
    s = authed_session()
    st = load_state()
    tok = st["access_token"]
    p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
    cid = str(json.loads(__import__("base64").urlsafe_b64decode(p))["user"]["customerId"])
    body = {"customerId": cid, "accountContext": {"accountNumber": SPU_ACCOUNT,
            "personId": None, "companyCd": None, "serviceAddress": None}}
    r = s.post(EPORTAL + "/rest/account/summary", json=body, timeout=30)
    r.raise_for_status()
    return r.json()["accountSummaryType"]


def _money(s):
    return float(str(s).replace("$", "").replace(",", "").strip())


def _alloc(amount, weights):
    """Allocate `amount` dollars across keys by `weights` (dict), cent-accurate.
    Largest-remainder rounding so the parts sum to `amount` exactly."""
    tw = sum(weights.values())
    raw = {k: amount * w / tw for k, w in weights.items()}
    cents = {k: int(raw[k] * 100) for k in weights}            # floor
    diff = int(round(amount * 100)) - sum(cents.values())
    order = sorted(weights, key=lambda k: (raw[k] * 100) % 1, reverse=True)
    for i in range(abs(diff)):
        cents[order[i % len(order)]] += 1 if diff > 0 else -1
    return {k: cents[k] / 100 for k in weights}


def bill_components(bill):
    """Pull the Water / Sewer / Solid-Waste dollar amounts from the bill's services.
    Each `serviceType` string embeds its amount, e.g. '... Bimonthly, $372.32, ...'."""
    comp = {"water": 0.0, "sewer": 0.0, "garbage": 0.0}
    for s in bill.get("services") or []:
        st = s.get("serviceType", "")
        m = re.search(r"\$([\d,]+\.\d{2})", st)
        if not m:
            continue
        amt = _money(m.group(1))
        low = st.lower()
        if "water" in low:
            comp["water"] += amt
        elif "sewer" in low:
            comp["sewer"] += amt
        elif "solid waste" in low or "garbage" in low or "refuse" in low:
            comp["garbage"] += amt
    return comp


def split(out_csv=None):
    """Split the current SPU bill across units the way the tracking sheet does:
       Water -> by meter usage %, Sewer & Garbage -> equal thirds."""
    import csv
    bill = fetch_bill()
    total = _money(bill["totalAmountDue"])
    comp = bill_components(bill)
    comp_sum = round(sum(comp.values()), 2)
    if abs(comp_sum - total) > 0.01:
        print(f"  WARNING: component sum ${comp_sum} != bill total ${total} "
              f"(water {comp['water']}, sewer {comp['sewer']}, garbage {comp['garbage']})")

    meter = json.load(open(os.path.join(BASE, "meter_state.json")))
    tbl = meter["table"]
    usage = tbl["usage"]                       # {unit: gallons}
    units = sorted(usage)
    tot_usage = tbl["total_usage"]

    water_share = _alloc(comp["water"], usage)                       # by usage
    equal = {u: 1 for u in units}
    sewer_share = _alloc(comp["sewer"], equal)                       # equal thirds
    garbage_share = _alloc(comp["garbage"], equal)                   # equal thirds

    # Optional manual HOA line items (Landscape, Admin fee, Extra garbage, ...).
    hoa = load_hoa()
    hoa_items = list(hoa.keys())
    if hoa:
        print(f"  [HOA] using {len(hoa_items)} line item(s) from {HOA_FILE}: {hoa_items}")
        print(f"        ^ verify these are correct for THIS billing cycle before sending.")

    rows = []
    for u in units:
        spu_tot = round(water_share[u] + sewer_share[u] + garbage_share[u], 2)
        row = {"unit": u, "usage_gal": usage[u],
               "usage_pct": round(100 * usage[u] / tot_usage, 2),
               "water": f"{water_share[u]:.2f}", "sewer": f"{sewer_share[u]:.2f}",
               "garbage": f"{garbage_share[u]:.2f}", "spu_total": f"{spu_tot:.2f}"}
        hoa_tot = 0.0
        for item in hoa_items:
            amt = float(hoa[item].get(u, 0) or 0)
            row[item] = f"{amt:.2f}"
            hoa_tot += amt
        if hoa:
            row["hoa_total"] = f"{hoa_tot:.2f}"
            row["current_bill"] = f"{round(spu_tot + hoa_tot, 2):.2f}"
        rows.append(row)

    out = {"spu_account": SPU_ACCOUNT, "service_address": bill["serviceAddress"],
           "bill_date": bill["currentBillDate"], "due_date": bill["paymentDueDate"],
           "bill_total": f"{total:.2f}",
           "components": {k: f"{v:.2f}" for k, v in comp.items()},
           "usage_period": f"{tbl['start_date']} to {tbl['end_date']}",
           # Meter reads are carried through so the sheet grid can show the two
           # cumulative-reading rows the tracking sheet has always had.
           "start_date": tbl["start_date"], "end_date": tbl["end_date"],
           "start_reads": tbl["start_reads"], "end_reads": tbl["end_reads"],
           "rows": rows}
    print(json.dumps(out, indent=2))

    # Default filename is date-stamped with the bill date (MM/DD/YYYY -> YYYY-MM-DD) so it's
    # clear which billing cycle each CSV is for, e.g. spu_bill_split_2026-06-02.csv.
    if out_csv is None:
        mm, dd, yyyy = bill["currentBillDate"].split("/")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        out_csv = os.path.join(OUTPUT_DIR, f"spu_bill_split_{yyyy}-{mm}-{dd}.csv")

    if out_csv:
        with open(out_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["# SPU account", SPU_ACCOUNT, out["service_address"]])
            w.writerow(["# bill date", out["bill_date"], "due", out["due_date"],
                        "total", f"${total:.2f}", "period", out["usage_period"]])
            w.writerow(["# components", f"water ${comp['water']:.2f} (by usage)",
                        f"sewer ${comp['sewer']:.2f} (equal)",
                        f"garbage ${comp['garbage']:.2f} (equal)"])
            header = ["unit", "usage_gal", "usage_pct", "water", "sewer", "garbage", "spu_total"]
            if hoa:
                header += hoa_items + ["hoa_total", "current_bill"]
            w.writerow(header)
            for r in rows:
                w.writerow([r.get(c, "") for c in header])
            # TOTAL row
            tot_row = ["TOTAL", tot_usage, "100.00", f"{comp['water']:.2f}",
                       f"{comp['sewer']:.2f}", f"{comp['garbage']:.2f}", f"{total:.2f}"]
            if hoa:
                for item in hoa_items:
                    tot_row.append(f"{sum(float(hoa[item].get(u, 0) or 0) for u in units):.2f}")
                hoa_grand = sum(float(hoa[item].get(u, 0) or 0) for item in hoa_items for u in units)
                tot_row += [f"{hoa_grand:.2f}", f"{round(total + hoa_grand, 2):.2f}"]
            w.writerow(tot_row)
        print("\nwrote", out_csv)
    return out


# --------------------------------------------------------------------------- google sheet
def _usd(v):
    """Format a 2-decimal numeric string as the sheet shows it: $75.10, -$20.00.
    The sign goes outside the '$' so Sheets parses it as a negative currency value."""
    s = str(v)
    return f"-${s[1:]}" if s.startswith("-") else f"${s}"


def _short_date(iso):
    """'2026-05-26' -> '5/26/26', the form the tracking sheet's reading rows use."""
    yyyy, mm, dd = iso.split("-")
    return f"{int(mm)}/{int(dd)}/{yyyy[2:]}"


def tab_title(bill_date):
    """'08/03/2026' -> '8/3/2026', matching the existing tab names in the sheet."""
    mm, dd, yyyy = bill_date.split("/")
    return f"{int(mm)}/{int(dd)}/{yyyy}"


def build_sheet_grid(out):
    """Turn the split() result into the tracking sheet's own layout: units run across as
    COLUMNS (2505, 2507, 6761, All) with line items down the side, matching every existing
    tab. Money is written as '$x.xx' and percents as 'x.xx%'; push_to_sheet sends these with
    USER_ENTERED so Sheets stores real numbers with currency/percent formatting."""
    comp = out["components"]
    rows = out["rows"]
    base = {"unit", "usage_gal", "usage_pct", "water", "sewer", "garbage", "spu_total"}
    # HOA item rows (Landscape, Extra garbage, ...) appear only when present, in order.
    hoa_items = [k for k in rows[0] if k not in base and k not in ("hoa_total", "current_bill")] if rows else []
    has_hoa = bool(hoa_items) and "current_bill" in (rows[0] if rows else {})
    units = [r["unit"] for r in rows]
    by_unit = {r["unit"]: r for r in rows}

    def _all_col(value, note):
        """A row holding one value in the 'All' column (past the label + unit columns)."""
        return [""] * (len(units) + 1) + [value, note]

    grid = []
    grid.append([""] + units + ["All"])
    grid.append(["Water", _usd(comp["water"])])
    # Two cumulative-reading rows; the start row must match last cycle's end row.
    grid.append([_short_date(out["start_date"])] + [out["start_reads"][u] for u in units])
    grid.append([_short_date(out["end_date"])] + [out["end_reads"][u] for u in units])

    total_usage = sum(int(by_unit[u]["usage_gal"]) for u in units)
    grid.append(["Water usage"] + [by_unit[u]["usage_gal"] for u in units] + [total_usage])
    grid.append(["Water percent"] + [f"{by_unit[u]['usage_pct']:.2f}%" for u in units] + ["100.00%"])
    grid.append(["Water cost"] + [_usd(by_unit[u]["water"]) for u in units] + [_usd(comp["water"])])
    grid.append(["Sewer"] + [_usd(by_unit[u]["sewer"]) for u in units] + [_usd(comp["sewer"])])
    grid.append(["Garbage"] + [_usd(by_unit[u]["garbage"]) for u in units] + [_usd(comp["garbage"])])
    # 'Combined' is the figure straight off the bill; 'Check' is the sum of the rounded
    # per-unit cells beside it. They sit in the 'All' column so a rounding drift is visible.
    spu_sum = sum(float(by_unit[u]["spu_total"]) for u in units)
    grid.append(_all_col(_usd(out["bill_total"]), "Combined"))
    grid.append(["Total"] + [_usd(by_unit[u]["spu_total"]) for u in units]
                + [_usd(f"{spu_sum:.2f}"), "Check"])

    if has_hoa:
        for item in hoa_items:
            grid.append([item] + [_usd(by_unit[u][item]) for u in units])
        grid.append(["HOA total"] + [_usd(by_unit[u]["hoa_total"]) for u in units])
        # Same Combined/Check pair as above: the bill plus the HOA adjustments, against
        # the sum of the rounded per-unit 'current bill' cells.
        combined = float(out["bill_total"]) + sum(float(by_unit[u]["hoa_total"]) for u in units)
        grand = sum(float(by_unit[u]["current_bill"]) for u in units)
        grid.append(_all_col(_usd(f"{combined:.2f}"), "Combined"))
        grid.append(["Current bill"] + [_usd(by_unit[u]["current_bill"]) for u in units]
                    + [_usd(f"{grand:.2f}"), "Check"])
    return grid


def push_to_sheet(grid, tab_title):
    """Write `grid` to a worksheet named `tab_title` in GOOGLE_SHEET_ID using a service
    account. Creates the tab if missing; clears and reuses it on a re-run (idempotent)."""
    conf = load_conf()
    sheet_id = conf.get("GOOGLE_SHEET_ID", "")
    keyfile = sa_keyfile(conf)
    if not sheet_id or sheet_id == "replace-me":
        raise SystemExit("set GOOGLE_SHEET_ID in config/nextcentury_credentials.conf")
    if not os.path.exists(keyfile):
        raise SystemExit(f"service-account key not found: {keyfile}\n"
                         "  create one in Google Cloud, share the sheet with its email (Editor),\n"
                         "  and set GOOGLE_SA_KEYFILE in config/nextcentury_credentials.conf")
    try:
        import gspread
    except ImportError:
        raise SystemExit("gspread not installed: pip3 install gspread google-auth")

    gc = gspread.service_account(filename=keyfile)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(tab_title)
        ws.clear()
    except gspread.WorksheetNotFound:
        # index=0 so the newest cycle sits first, the way the sheet is already ordered.
        ws = sh.add_worksheet(title=tab_title, rows=max(40, len(grid) + 5), cols=20, index=0)

    width = max((len(r) for r in grid), default=1)
    body = [r + [""] * (width - len(r)) for r in grid]   # pad ragged rows to a rectangle
    # USER_ENTERED so '$50.48' / '36.12%' land as real numbers with currency and percent
    # formatting, the way the hand-maintained tabs store them, rather than as text.
    ws.update(range_name="A1", values=body, value_input_option="USER_ENTERED")
    print(f"pushed to sheet tab '{tab_title}'")
    return sh.url


def sheet():
    """Full sheet workflow: compute the split, write the pretty CSV, push to Google."""
    import csv
    out = split()                                   # also writes the machine spu_bill_split CSV
    grid = build_sheet_grid(out)

    mm, dd, yyyy = out["bill_date"].split("/")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    pretty_csv = os.path.join(OUTPUT_DIR, f"sheet_upload_{yyyy}-{mm}-{dd}.csv")
    with open(pretty_csv, "w", newline="") as f:
        csv.writer(f).writerows(grid)
    print("wrote", pretty_csv)

    url = push_to_sheet(grid, tab_title(out["bill_date"]))
    print("sheet:", url)
    return out


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "login":
        login()
    elif cmd == "discover":
        discover()
    elif cmd == "get":
        get(sys.argv[2])
    elif cmd == "split":
        # No path given -> split() auto-names it spu_bill_split_<bill-date>.csv.
        out = sys.argv[2] if len(sys.argv) > 2 else None
        split(out_csv=out)
    elif cmd == "sheet":
        sheet()
    else:
        print(__doc__)
