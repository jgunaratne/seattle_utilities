# Utility Billing Pipeline Notes

This directory contains a small billing pipeline:

1. Fetch cumulative water meter readings from NextCentury.
2. Fetch the current Seattle MyUtilities bill through the web portal's HTTP flow.
3. Split water by usage percentage.
4. Split sewer and solid waste equally across configured units.
5. Optionally add manual HOA line items from a local adjustment file.
6. Write CSV output for review or sheet import.

All account-specific values live in `config/nextcentury_credentials.conf`, which is ignored
by git. Start from `config/nextcentury_credentials.conf.example`.

## Files

| File | Purpose |
|---|---|
| `run_cycle.sh` | One-command orchestrator: login, derive the billing window, pull readings, write the split CSV, and push to the sheet if configured. |
| `meter_pipeline.py` | Fetches NextCentury readings and checkpoints to `meter_state.json`. |
| `seattle_bill.py` | Logs into Seattle MyUtilities over HTTP, fetches the bill, writes the split CSV to `output/`, and (via `sheet`) pushes to a Google Sheet. |
| `config/nextcentury_credentials.conf.example` | Public-safe template for local credentials and account IDs. |
| `config/hoa_adjustments.json.example` | Public-safe template for local per-cycle manual adjustments. |
| `AGENT_RUNBOOK.md` | Repeatable operating procedure for each billing cycle. |

Generated split CSVs are written to `output/` (created on demand). Ignored local files
include the real `config/` secrets (credentials and the Google service-account key JSON),
state files, the `output/` CSVs, per-cycle HOA adjustments, and the `.venv/`. Only the
`config/*.example` templates are tracked.

## Dependencies

`requests` is required for everything. The Google Sheet push additionally needs `gspread`
and `google-auth`. The recommended setup is a virtualenv at `.venv/` (auto-detected by
`run_cycle.sh`):

```bash
python3 -m venv .venv
.venv/bin/pip install requests gspread google-auth
```

## Local Config

Copy the example config and fill in local values:

```bash
cp config/nextcentury_credentials.conf.example config/nextcentury_credentials.conf
chmod 600 config/nextcentury_credentials.conf
```

Required keys:

```bash
NC_EMAIL="you@example.com"
NC_PASSWORD="replace-me"
NC_PROPERTY_ID="p_REPLACE_ME"
NC_UNIT_NAMES="UNIT_1,UNIT_2,UNIT_3"

SEATTLE_UTIL_USERNAME="replace-me"
SEATTLE_UTIL_PASSWORD="replace-me"
SPU_ACCOUNT_NUMBER="replace-me"
```

Optional keys (Google Sheet push, used by `seattle_bill.py sheet`):

```bash
GOOGLE_SHEET_ID="replace-me"
GOOGLE_SA_KEYFILE="google_service_account.json"
```

`GOOGLE_SA_KEYFILE` is the path to a Google service-account JSON key; a bare filename is
resolved relative to `config/` (or use an absolute path). See "Google Sheet Push" below for
the one-time setup.

## Runtime State

`meter_pipeline.py` writes `meter_state.json`. `seattle_bill.py login` writes
`seattle_state.json`, including a short-lived bearer token. These files are private runtime
state and are ignored by git.

## Google Sheet Push

`seattle_bill.py sheet` (and `run_cycle.sh`, when configured) writes the split into a tab of
a Google Sheet named by the bill date. It authenticates with a service account, so the
one-time setup is:

1. Create a service account in Google Cloud and download its JSON key into `config/`.
2. Point `GOOGLE_SA_KEYFILE` at that file and set `GOOGLE_SHEET_ID` (the long id in the
   sheet URL: `.../spreadsheets/d/<THIS>/edit`).
3. Enable the **Google Sheets API** for the key's Cloud project.
4. Share the sheet with the service account's email
   (`…@….iam.gserviceaccount.com`) as an **Editor** — it needs write access.

A plain API key cannot write to a private sheet; writes require an authenticated identity,
which is why this pipeline uses a service-account key rather than `GOOGLE_CLOUD_API_KEY`.

## API Notes

NextCentury:

- Base URL: `https://api.nextcenturymeters.com`
- Login: `POST /login` with `{email, password, deviceId}` and header `version: 2`
- Authenticated requests send the returned token in the `authorization` header.
- Property ID and unit names come from `config/nextcentury_credentials.conf`.
- Daily reads endpoint: `/api/Units/{unitId}/DailyReads?from={ISO}&to={ISO}`
- The representative reading for a day is the last check-in for that day.

Seattle MyUtilities:

- Portal: `https://myutilities.seattle.gov`
- Login is handled by `seattle_bill.py login`; avoid repeated failed login attempts.
- The script stores only the portal bearer token in ignored local state.
- `SPU_ACCOUNT_NUMBER` comes from `config/nextcentury_credentials.conf`.

## Split Rules

The Seattle bill has separate components:

- Water: split by meter usage percentage.
- Sewer: split equally across the configured units.
- Garbage or solid waste: split equally across the configured units.

The split uses largest-remainder rounding so the per-unit cents sum exactly to each bill
component.
