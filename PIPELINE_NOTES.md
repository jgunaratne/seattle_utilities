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
| `meter_pipeline.py` | Fetches NextCentury readings and checkpoints to `meter_state.json`. |
| `seattle_bill.py` | Logs into Seattle MyUtilities over HTTP, fetches the bill, and writes the split CSV. |
| `config/nextcentury_credentials.conf.example` | Public-safe template for local credentials and account IDs. |
| `config/hoa_adjustments.json.example` | Public-safe template for local per-cycle manual adjustments. |
| `AGENT_RUNBOOK.md` | Repeatable operating procedure for each billing cycle. |

Ignored local files include the real `config/` secrets (credentials and the Google
service-account key), state files, generated CSVs under `output/`, and per-cycle HOA
adjustments. Only the `config/*.example` templates are tracked.

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

Optional key:

```bash
GOOGLE_SHEET_ID="replace-me"
```

## Runtime State

`meter_pipeline.py` writes `meter_state.json`. `seattle_bill.py login` writes
`seattle_state.json`, including a short-lived bearer token. These files are private runtime
state and are ignored by git.

The `work/` directory is reserved for scratch traces, fetched HTML, cookie jars, and other
debug artifacts. It is ignored by git.

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
