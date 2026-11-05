
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone

DB_PATH = Path("trading_dashboard.sqlite3")


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def get_table_names():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            ORDER BY name
        """).fetchall()

    return [row["name"] for row in rows]

def get_table_columns(table_name):
    with get_connection() as conn:
        # Verify table exists
        tables = get_table_names()

        if table_name not in tables:
            raise ValueError(f"Table not found: {table_name}")

        rows = conn.execute(
            f'PRAGMA table_info("{table_name}")'
        ).fetchall()

    return [dict(row) for row in rows]

def get_table_data(table_name):
    with get_connection() as conn:
        tables = get_table_names()

        if table_name not in tables:
            raise ValueError(f"Table not found: {table_name}")

        rows = conn.execute(
            f'SELECT * FROM "{table_name}"'
        ).fetchall()

    return [dict(row) for row in rows]

def get_database():
    database = {}

    for table in get_table_names():
        database[table] = {
            "columns": get_table_columns(table),
            "records": get_table_data(table)
        }

    return database

if __name__ == "__main__":

    database = get_database()

    for table_name, contents in database.items():

        print(f"\n{'=' * 50}")
        print(f"TABLE: {table_name}")
        print(f"{'=' * 50}")

        print("\nCOLUMNS:")

        for column in contents["columns"]:
            print(
                column["name"],
                column["type"],
                "PRIMARY KEY:", column["pk"]
            )

        print("\nRECORDS:")

        for record in contents["records"]:
            print(record)
