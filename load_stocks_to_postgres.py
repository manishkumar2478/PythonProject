import argparse
import csv
import json
import os
from decimal import Decimal
from datetime import datetime

import psycopg2
from psycopg2.extras import execute_values
from psycopg2 import sql


def load_env_file(env_path=None):
    env_path = env_path or os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.isfile(env_path):
        return

    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)



def parse_args():
    parser = argparse.ArgumentParser(description="Load CSV or JSON data into PostgreSQL incrementally.")
    parser.add_argument(
        "--format",
        choices=["csv", "json"],
        default="csv",
        help="Input file format.",
    )
    parser.add_argument(
        "--input",
        "--csv",
        dest="input",
        default=None,
        help="Path to the input file. For CSV use Stocks.csv, for JSON use json/bulk.json.",
    )
    parser.add_argument(
        "--table",
        default=os.environ.get("STOCKS_TABLE", "stocks"),
        help="PostgreSQL target table name.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Number of rows to insert per batch.",
    )
    parser.add_argument(
        "--show-audit",
        action="store_true",
        help="Show recent audit entries and exit.",
    )
    parser.add_argument(
        "--audit-limit",
        type=int,
        default=10,
        help="Number of audit records to show when using --show-audit.",
    )
    return parser.parse_args()


def parse_json(json_path):
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        if "data" in data:
            data = data["data"]
        elif "records" in data:
            data = data["records"]
        else:
            raise ValueError("JSON file must contain a list of records or an object with a 'data' or 'records' key.")

    if not isinstance(data, list):
        raise ValueError("JSON data must be a list of records.")

    rows = []
    for raw_row in data:
        if not raw_row:
            continue
        if not isinstance(raw_row, dict):
            raise ValueError("Each JSON record must be an object.")

        trade_date = datetime.strptime(raw_row["Date"].strip(), "%d-%b-%Y").date()
        symbol = raw_row["Symbol"].strip()
        security_name = raw_row["Security Name"].strip()
        client_name = raw_row["Client Name"].strip()
        buy_sell = raw_row["Buy/Sell"].strip()
        quantity_traded = int(str(raw_row["Quantity Traded"]).replace(",", ""))
        trade_price = Decimal(str(raw_row["Trade Price / Wght. Avg. Price"]).replace(",", ""))
        remarks = raw_row.get("Remarks", "").strip() or None

        rows.append(
            (
                trade_date,
                symbol,
                security_name,
                client_name,
                buy_sell,
                quantity_traded,
                trade_price,
                remarks,
            )
        )
    return rows


def get_connection():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "postgres"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", ""),
    )


def parse_csv(csv_path):
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw_row in reader:
            if not raw_row.get("Date"):
                continue
            trade_date = datetime.strptime(raw_row["Date"].strip(), "%d-%b-%Y").date()
            symbol = raw_row["Symbol"].strip()
            security_name = raw_row["Security Name"].strip()
            client_name = raw_row["Client Name"].strip()
            buy_sell = raw_row["Buy/Sell"].strip()
            quantity_traded = int(raw_row["Quantity Traded"].replace(",", ""))
            trade_price = Decimal(raw_row["Trade Price / Wght. Avg. Price"].replace(",", ""))
            remarks = raw_row.get("Remarks", "").strip() or None
            rows.append(
                (
                    trade_date,
                    symbol,
                    security_name,
                    client_name,
                    buy_sell,
                    quantity_traded,
                    trade_price,
                    remarks,
                )
            )
    return rows


def ensure_table_exists(conn, table_name):
    # Create the table without a UNIQUE constraint so duplicate rows can be loaded.
    create_sql = sql.SQL(
        """
        CREATE TABLE IF NOT EXISTS {} (
            trade_date DATE NOT NULL,
            symbol TEXT NOT NULL,
            security_name TEXT NOT NULL,
            client_name TEXT NOT NULL,
            buy_sell TEXT NOT NULL,
            quantity_traded BIGINT NOT NULL,
            trade_price_wght_avg_price NUMERIC NOT NULL,
            remarks TEXT,
            loaddate TIMESTAMP NOT NULL DEFAULT now()
        )
        """
    ).format(sql.Identifier(table_name))
    with conn.cursor() as cur:
        cur.execute(create_sql)
        # ensure loaddate and fileid columns exist for existing tables
        alter_sql = sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS loaddate TIMESTAMP NOT NULL DEFAULT now();").format(sql.Identifier(table_name))
        cur.execute(alter_sql)
        alter_sql2 = sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS fileid INTEGER;").format(sql.Identifier(table_name))
        cur.execute(alter_sql2)
    conn.commit()


def remove_unique_constraints(conn, table_name):
    # Drop any unique constraints on the table to allow duplicate inserts.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = %s::regclass
              AND contype = 'u'
            """,
            (table_name,)
        )
        constraints = [r[0] for r in cur.fetchall()]
        if not constraints:
            return

        # Build a table identifier (schema.table) if needed
        if "." in table_name:
            schema, tbl = table_name.split(".", 1)
            table_ident = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(tbl))
        else:
            table_ident = sql.Identifier(table_name)

        for con in constraints:
            cur.execute(sql.SQL("ALTER TABLE {} DROP CONSTRAINT {};").format(table_ident, sql.Identifier(con)))
    conn.commit()


def ensure_audit_table_exists(conn, audit_table_name):
    create_sql = sql.SQL(
        """
        CREATE TABLE IF NOT EXISTS {} (
            id SERIAL PRIMARY KEY,
            filename TEXT NOT NULL,
            filetype TEXT NOT NULL,
            total_records INTEGER NOT NULL,
            inserted_records INTEGER NOT NULL,
            skipped_records INTEGER NOT NULL,
            file_size_bytes BIGINT,
            loaddate TIMESTAMP NOT NULL DEFAULT now()
        )
        """
    ).format(sql.Identifier(audit_table_name))
    with conn.cursor() as cur:
        cur.execute(create_sql)
    conn.commit()


def ensure_stocks_fileid_fk(conn, table_name, audit_table_name):
    # ensure fileid column exists and add a FK constraint to audit table if missing
    fk_name = f"{table_name}_fileid_fkey"
    with conn.cursor() as cur:
        cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS fileid INTEGER;").format(sql.Identifier(table_name)))
        # check if constraint exists
        cur.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = %s",
            (fk_name,)
        )
        if cur.fetchone():
            return
        # add foreign key constraint
        cur.execute(sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY (fileid) REFERENCES {} (id);").format(
            sql.Identifier(table_name), sql.Identifier(fk_name), sql.Identifier(audit_table_name)
        ))
    conn.commit()


def insert_audit_record(conn, audit_table_name, filename, filetype, total, inserted, skipped, file_size):
    insert_sql = sql.SQL(
        "INSERT INTO {} (filename, filetype, total_records, inserted_records, skipped_records, file_size_bytes, loaddate) VALUES (%s, %s, %s, %s, %s, %s, now()) RETURNING id"
    ).format(sql.Identifier(audit_table_name))
    with conn.cursor() as cur:
        cur.execute(insert_sql, (filename, filetype, total, inserted, skipped, file_size))
        audit_id = cur.fetchone()[0]
    conn.commit()
    return audit_id


def update_audit_record(conn, audit_table_name, audit_id, inserted, skipped):
    update_sql = sql.SQL(
        "UPDATE {} SET inserted_records = %s, skipped_records = %s WHERE id = %s"
    ).format(sql.Identifier(audit_table_name))
    with conn.cursor() as cur:
        cur.execute(update_sql, (inserted, skipped, audit_id))
    conn.commit()


def show_audit_entries(conn, audit_table_name, limit=10):
    # Fetch recent audit rows and print them
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT id, filename, filetype, total_records, inserted_records, skipped_records, file_size_bytes, loaddate FROM {} ORDER BY loaddate DESC LIMIT %s;").format(sql.Identifier(audit_table_name)),
            (limit,),
        )
        rows = cur.fetchall()
    if not rows:
        print("No audit records found.")
        return

    # Print header
    print("id | filename | filetype | total | inserted | skipped | size_bytes | loaddate")
    for r in rows:
        print(" | ".join([str(x) if x is not None else "" for x in r]))


def insert_rows(conn, table_name, rows, batch_size=1000, fileid=None):
    # Insert including loaddate so every row records when it was loaded.
    insert_sql = sql.SQL(
        """
        INSERT INTO {} (
            trade_date,
            symbol,
            security_name,
            client_name,
            buy_sell,
            quantity_traded,
            trade_price_wght_avg_price,
            remarks,
            loaddate,
            fileid
        ) VALUES %s
        """
    ).format(sql.Identifier(table_name))
    inserted = 0
    load_ts = datetime.now()
    with conn.cursor() as cur:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            # append load timestamp to each row tuple
            if fileid is None:
                batch_with_load = [r + (load_ts, None) for r in batch]
            else:
                batch_with_load = [r + (load_ts, fileid) for r in batch]
            execute_values(cur, insert_sql.as_string(conn), batch_with_load)
            inserted += cur.rowcount
    conn.commit()
    return inserted


def main():
    load_env_file()
    args = parse_args()
    audit_table = os.environ.get("AUDIT_TABLE", "stocks_audit")

    if args.show_audit:
        with get_connection() as conn:
            ensure_audit_table_exists(conn, audit_table)
            show_audit_entries(conn, audit_table, args.audit_limit)
        return

    if args.input is None:
        args.input = os.path.join(os.path.dirname(__file__), "Stocks.csv") if args.format == "csv" else os.path.join(os.path.dirname(__file__), "json", "bulk.json")

    if not os.path.isfile(args.input):
        raise FileNotFoundError(f"Input file not found: {args.input}")

    if args.format == "csv":
        rows = parse_csv(args.input)
        if not rows:
            print("No rows found in the CSV file.")
            return
    else:
        rows = parse_json(args.input)
        if not rows:
            print("No rows found in the JSON file.")
            return

    with get_connection() as conn:
        # ensure audit table exists first, then create an audit record to obtain file id
        ensure_audit_table_exists(conn, audit_table)
        # compute file metadata
        filename = os.path.basename(args.input)
        filetype = os.path.splitext(filename)[1].lstrip(".") or "unknown"
        try:
            file_size = os.path.getsize(args.input)
        except OSError:
            file_size = None
        # insert audit row with zero counts for now and get audit id
        audit_id = insert_audit_record(conn, audit_table, filename, filetype, len(rows), 0, 0, file_size)

        ensure_table_exists(conn, args.table)
        # ensure stocks has fileid column and FK to audit
        ensure_stocks_fileid_fk(conn, args.table, audit_table)
        # remove any unique constraints so duplicates can be loaded
        remove_unique_constraints(conn, args.table)
        # insert rows with fileid set to audit_id
        inserted = insert_rows(conn, args.table, rows, args.batch_size, fileid=audit_id)
        skipped = len(rows) - inserted
        # update the audit record with actual counts
        update_audit_record(conn, audit_table, audit_id, inserted, skipped)

    skipped = len(rows) - inserted
    print(f"Loaded {len(rows)} rows from {args.csv}.")
    print(f"Inserted {inserted} new rows into {args.table}.")
    print(f"Skipped {skipped} duplicate rows.")
    print(f"fileid={audit_id}")


if __name__ == "__main__":
    main()
