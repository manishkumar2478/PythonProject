# PythonProject

This repository contains a Python loader for importing `Stocks.csv` into a PostgreSQL table and recording run metadata.

## What this does

- Reads `Stocks.csv` from the project root (or a path via `--csv`)
- Ensures a PostgreSQL target table exists and adds a `loaddate` column to record when each row was loaded
- Inserts all rows (duplicates are allowed) and records when each row was loaded
- Creates and writes a run-level audit record into an audit table (defaults to `stocks_audit`)
- Loads database connection settings from a project `.env` file if present

## Setup

1. Install dependencies:

```bash
pip install psycopg2-binary
```

2. Create or update `.env` in the project root with your DB credentials:

```env
PGHOST=localhost
PGPORT=5432
PGDATABASE=postgres
PGUSER=postgres
PGPASSWORD=your_password_here
STOCKS_TABLE=stocks
# Optional: change audit table name
AUDIT_TABLE=stocks_audit
```

## Run the loader

Basic run for CSV data:

```bash
python load_stocks_to_postgres.py
```

Use the loader for CSV or JSON input with the new flags:

- `--format` : input file format (`csv` or `json`)
- `--input` : path to the input file
- `--table` : target table name (overrides `STOCKS_TABLE`)
- `--batch-size` : number of rows per insert batch
- `--show-audit` : print recent audit entries and exit
- `--audit-limit` : number of audit rows to show with `--show-audit` (default 10)

Example — load CSV and record audit:

```bash
python load_stocks_to_postgres.py --format csv --input Stocks.csv --table stocks --batch-size 500
```

Example — load JSON and record audit:

```bash
python load_stocks_to_postgres.py --format json --input json/Stocks_06282026.json --table stocks --batch-size 500
```

Example — show last 5 audit records:

```bash
python load_stocks_to_postgres.py --show-audit --audit-limit 5
```

## JSON loader

This repository also includes a JSON loader script that loads the same data format into PostgreSQL with audit tracking.

Basic run:

```bash
python load_json_to_postgres.py
```

Useful options:

- `--json` : custom JSON path (default `json/bulk.json`)
- `--table` : target table name (overrides `STOCKS_TABLE`)
- `--batch-size` : number of rows per insert batch
- `--show-audit` : print recent audit entries and exit
- `--audit-limit` : number of audit rows to show with `--show-audit` (default 10)

Example — load JSON and record audit:

```bash
python load_json_to_postgres.py --json json/bulk.json --table stocks --batch-size 500
```

## Audit table

By default the script creates and writes to `stocks_audit` (override with `AUDIT_TABLE`). The audit table contains:

- `id` (serial)
- `filename` (text)
- `filetype` (text)
- `total_records` (int)
- `inserted_records` (int)
- `skipped_records` (int)
- `file_size_bytes` (bigint)
- `loaddate` (timestamp)

You can also query the audit table directly with `psql`:

```bash
psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -c "SELECT * FROM stocks_audit ORDER BY loaddate DESC LIMIT 10;"
```

## Notes & next steps

- The loader now allows duplicates and records a `loaddate` per row. If you prefer to prevent duplicates, re-add a UNIQUE constraint or change the insert logic to use `ON CONFLICT` with an appropriate key.
- Consider adding a retention or cleanup job for `stocks_audit` if it grows over time.
- I can add a `--dry-run` flag, pretty audit output, or a dedupe/cleanup helper if you'd like — tell me which one to add next.
