# Agent Runbook

Use this procedure each billing cycle to refresh meter readings, fetch the Seattle utility
bill, compute the split, and prepare CSV output.

For the common case you can run the whole cycle with one command (it auto-derives the
billing window and uses `.venv/` if present):

```bash
./run_cycle.sh          # add -y to skip the confirmation prompt
```

The steps below are the manual equivalent, for backfills or debugging. They show `python3`;
if you installed the dependencies in `.venv`, use `.venv/bin/python` instead (the sheet push
needs `gspread`, which lives in the venv).

## Setup (once)

Install dependencies into a virtualenv (auto-detected by `run_cycle.sh`):

```bash
python3 -m venv .venv
.venv/bin/pip install requests gspread google-auth   # gspread/google-auth only needed for the sheet push
```

Create the local config:

```bash
cp config/nextcentury_credentials.conf.example config/nextcentury_credentials.conf
chmod 600 config/nextcentury_credentials.conf
```

Fill in the local-only values in `config/nextcentury_credentials.conf`:

- `NC_EMAIL`
- `NC_PASSWORD`
- `NC_PROPERTY_ID`
- `NC_UNIT_NAMES`
- `SEATTLE_UTIL_USERNAME`
- `SEATTLE_UTIL_PASSWORD`
- `SPU_ACCOUNT_NUMBER`
- `GOOGLE_SHEET_ID` and `GOOGLE_SA_KEYFILE` if you use the sheet workflow (see Step 4)

Do not commit the real files under `config/` (`nextcentury_credentials.conf`,
`hoa_adjustments.json`, the Google service-account key), `*_state.json`, or generated CSVs
under `output/`. Only the `config/*.example` templates are tracked.

## Constraints

- Use pure HTTP only. Do not launch Chrome, Chromium, or a headless browser on a small Pi.
- Seattle MyUtilities may lock an account after failed attempts. Use `seattle_bill.py login`
  and do not retry credential posts in a loop.
- `seattle_state.json` contains a short-lived bearer token and is ignored by git.

## Step 1: Pull Meter Readings

Use the previous bill date and current bill date as the billing window:

```bash
python3 meter_pipeline.py readings --start <YYYY-MM-DD> --end <YYYY-MM-DD>
```

This updates `meter_state.json` with unit IDs, readings, usage, totals, and percentages.

Sanity check: the new period's start reads should match the previous period's end reads.

## Step 2: Set HOA Adjustments

If this cycle has manual HOA adjustments, copy the example file and edit local values:

```bash
cp config/hoa_adjustments.json.example config/hoa_adjustments.json
```

`config/hoa_adjustments.json` is ignored by git. Lines whose keys start with `_` are ignored by the
script. Verify these values against the actual HOA charges before sending a bill.

## Step 3: Fetch Bill and Compute Split

```bash
python3 seattle_bill.py login
python3 seattle_bill.py split
```

`split` fetches the current bill, reads `meter_state.json`, applies optional HOA items, and
writes `output/spu_bill_split_<bill-date>.csv`.

Sanity checks:

- Water + sewer + garbage equals the bill total.
- Usage percentages sum to 100.00.
- Per-unit SPU subtotals sum to the bill total.
- HOA line items are correct for this cycle.

## Step 4: Push to the Google Sheet (optional)

```bash
python3 seattle_bill.py sheet
```

`sheet` runs the same split, also writes `output/sheet_upload_<bill-date>.csv`, and pushes
the result into a tab named for the bill date in the configured Google Sheet (creating the
tab if needed, clearing and reusing it on a re-run). `run_cycle.sh` does this automatically
when the sheet is configured; otherwise it falls back to `split` and you import the CSV
manually.

One-time setup for the push:

1. Create a Google Cloud service account and download its JSON key into `config/`.
2. Set `GOOGLE_SA_KEYFILE` (bare filename resolves relative to `config/`) and
   `GOOGLE_SHEET_ID` in `config/nextcentury_credentials.conf`.
3. Enable the **Google Sheets API** for the key's Cloud project.
4. Share the sheet with the service account's email (`…@….iam.gserviceaccount.com`) as an
   **Editor**.

Writes need an authenticated identity, so this uses a service-account key, not an API key.

## Re-run Cheat Sheet

One-shot (auto-derives the window, pushes to the sheet if configured):

```bash
./run_cycle.sh -y
```

Manual:

```bash
python3 meter_pipeline.py readings --start <prev-bill-date> --end <this-bill-date>
# edit config/hoa_adjustments.json if needed
python3 seattle_bill.py login
python3 seattle_bill.py split        # or: seattle_bill.py sheet  (also pushes to the sheet)
```
