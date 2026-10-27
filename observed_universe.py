"""Persistence helpers for sharing the dashboard's observed symbols with pages."""
from __future__ import annotations

import sqlite3


def save_observed_symbols(db_path, symbols):
    cleaned = list(dict.fromkeys(
        str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()
    ))
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS observed_symbols (
            symbol TEXT PRIMARY KEY, position INTEGER NOT NULL)""")
        db.execute("DELETE FROM observed_symbols")
        db.executemany("INSERT INTO observed_symbols(symbol,position) VALUES(?,?)",
                       [(symbol, index) for index, symbol in enumerate(cleaned)])


def load_observed_symbols(db_path):
    with sqlite3.connect(db_path) as db:
        try:
            rows = db.execute("SELECT symbol FROM observed_symbols ORDER BY position").fetchall()
        except sqlite3.OperationalError:
            return []
    return [row[0] for row in rows]
