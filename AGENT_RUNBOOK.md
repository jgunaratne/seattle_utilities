# Agent Runbook

Use this procedure each billing cycle to refresh meter readings, fetch the Seattle utility
bill, compute the split, and prepare CSV output.

Before running anything, create the local config:

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
- `GOOGLE_SHEET_ID` if you use a sheet workflow

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

## Step 4: Sheet Update

The pipeline writes durable CSV output. Import or paste it into your tracking sheet using
your normal sheet workflow.

If you automate the sheet update, store OAuth credentials outside this repository and keep
sheet IDs in the ignored local config file.

## Re-run Cheat Sheet

```bash
python3 meter_pipeline.py readings --start <prev-bill-date> --end <this-bill-date>
# edit config/hoa_adjustments.json if needed
python3 seattle_bill.py login
python3 seattle_bill.py split
```
