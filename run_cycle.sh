#!/usr/bin/env bash
#
# run_cycle.sh — run one billing cycle end to end, no AI agent involved.
#
# The meter-reading window is NOT printed on the Seattle bill. By convention it is:
#     start = the PREVIOUS bill date   end = THIS (new) bill date
# So this script discovers the dates for you instead of making you guess:
#     end   <- the current bill date fetched from Seattle MyUtilities (currentBillDate)
#     start <- last cycle's end date saved in meter_state.json (table.end_date)
# That keeps the invariant that this period's start reads match last period's end reads.
#
# Usage:
#   ./run_cycle.sh                      # auto: derive both dates, ask to confirm
#   ./run_cycle.sh -y                   # auto, no confirmation prompt
#   ./run_cycle.sh <start> <end>        # manual override, YYYY-MM-DD (e.g. backfills)
#
# Steps: login (once) -> fetch bill date -> pull meter readings -> write split CSV.
#
# Safety: `login` runs exactly once. On failure it ABORTS rather than retrying,
# because Seattle MyUtilities may lock the account after repeated failed attempts.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# Prefer an explicit $PYTHON, then the project venv if present, then system python3.
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi
date_re='^[0-9]{4}-[0-9]{2}-[0-9]{2}$'

die()  { echo "ERROR: $*" >&2; exit 1; }
note() { echo "$*" >&2; }

# --- parse args -------------------------------------------------------------
ASSUME_YES=0
if [ "${1:-}" = "-y" ] || [ "${1:-}" = "--yes" ]; then ASSUME_YES=1; shift; fi

MANUAL=0
START=""; END=""
if [ $# -eq 2 ]; then
  MANUAL=1; START="$1"; END="$2"
elif [ $# -ne 0 ]; then
  die "usage: $0 [-y] [<start YYYY-MM-DD> <end YYYY-MM-DD>]"
fi

# --- preflight --------------------------------------------------------------
[ -f config/nextcentury_credentials.conf ] || die \
  "missing config/nextcentury_credentials.conf — copy config/nextcentury_credentials.conf.example and fill it in"
command -v "$PY" >/dev/null 2>&1 || die "$PY not found on PATH"
"$PY" -c 'import requests' 2>/dev/null || die \
  "the 'requests' package is required: pip3 install requests"

# --- Seattle login (ONCE, no retry) -----------------------------------------
note "==> Logging in to Seattle MyUtilities (single attempt)"
"$PY" seattle_bill.py login || die \
  "Seattle login failed. NOT retrying (account-lock risk). Check credentials and try later."

# --- derive the billing window ----------------------------------------------
if [ "$MANUAL" -eq 0 ]; then
  note "==> Fetching current bill date from Seattle MyUtilities"
  END="$("$PY" - <<'PY'
import seattle_bill as sb
b = sb.fetch_bill()
mm, dd, yyyy = b["currentBillDate"].split("/")
print(f"{yyyy}-{mm}-{dd}")
PY
)" || die "could not read current bill date from the Seattle bill"

  START="$("$PY" - <<'PY'
import json, sys
try:
    print(json.load(open("meter_state.json"))["table"]["end_date"])
except Exception:
    sys.exit(1)
PY
)" || die "no previous end date in meter_state.json — pass dates manually: $0 <start> <end>"
fi

# --- validate ---------------------------------------------------------------
[[ "$START" =~ $date_re ]] || die "start date '$START' must be YYYY-MM-DD"
[[ "$END"   =~ $date_re ]] || die "end date '$END' must be YYYY-MM-DD"
if [[ "$START" > "$END" ]] || [[ "$START" == "$END" ]]; then
  die "window is empty: start ($START) is not before end ($END). " \
      "If the current bill was already processed, there is nothing new to run."
fi

# --- confirm ----------------------------------------------------------------
echo
echo "Billing window : $START  ->  $END"
[ "$MANUAL" -eq 0 ] && echo "   start from last cycle (meter_state.json), end from current bill"
if [ -f config/hoa_adjustments.json ]; then
  echo "HOA file       : present — verify its values are correct for THIS cycle"
else
  echo "HOA file       : none (no manual HOA adjustments will be applied)"
fi
echo
if [ "$ASSUME_YES" -eq 0 ]; then
  [ -t 0 ] || die "not a terminal; re-run with -y to skip the prompt"
  read -r -p "Proceed with this window? [y/N] " ans
  case "$ans" in y|Y|yes|YES) ;; *) die "aborted by user" ;; esac
fi

# --- pull meter readings ----------------------------------------------------
echo
echo "==> Pulling meter readings  $START .. $END"
"$PY" meter_pipeline.py readings --start "$START" --end "$END"

# --- fetch bill + write split CSV (+ push to Google Sheet if configured) ----
echo
SHEET_READY="$("$PY" - <<'PY'
import os, seattle_bill as sb
c = sb.load_conf()
sid = c.get("GOOGLE_SHEET_ID", "")
kf = sb.sa_keyfile(c)
print("yes" if sid and sid != "replace-me" and os.path.exists(kf) else "no")
PY
)"
if [ "$SHEET_READY" = yes ]; then
  echo "==> Computing split, writing CSV, and pushing to Google Sheet"
  "$PY" seattle_bill.py sheet
else
  echo "==> Computing split and writing CSV (Google Sheet not configured — skipping push)"
  "$PY" seattle_bill.py split
fi

# --- report -----------------------------------------------------------------
echo
NEWEST_CSV="$(ls -t output/spu_bill_split_*.csv 2>/dev/null | head -1 || true)"
if [ -n "$NEWEST_CSV" ]; then
  echo "Done. Split written to: $NEWEST_CSV"
  echo
  echo "Sanity checks before sending:"
  echo "  - new period's start reads match last period's end reads"
  echo "  - water + sewer + garbage equals the bill total"
  echo "  - usage percentages sum to 100.00"
  echo "  - per-unit SPU subtotals sum to the bill total"
else
  echo "Done, but no output/spu_bill_split_*.csv was found — check the split output above."
fi
