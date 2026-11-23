"""Persistent position history and its human-readable log file."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
import sqlite3


def record_position(db, timestamp, mode, event, side, symbol, quantity, *, price=None,
                    pnl=None, source):
    line = f"{timestamp} {mode} POSITION_{event} {side} {symbol} quantity={quantity}"
    if price is not None:
        line += f" price={price}"
    if pnl is not None:
        line += f" realized_pnl={pnl:.2f}"
    db.execute("INSERT INTO position_log_entries(line) VALUES (?)", (f"{line} source={source}",))


class PositionLog:
    def __init__(self, db_path):
        self.db_path = db_path
        self.path = db_path.with_name("stocktrader-positions.log")
        self.write_error = ""
        with sqlite3.connect(db_path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS position_log_entries(id INTEGER PRIMARY KEY, line TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS position_log_cursor(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), entry_id INTEGER, file_size INTEGER);
                CREATE TABLE IF NOT EXISTS position_log_accounts(
                    account TEXT PRIMARY KEY, quantities TEXT NOT NULL, started_at TEXT NOT NULL,
                    snapshot_cutoffs TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS position_log_fills(
                    account TEXT, order_id TEXT, symbol TEXT, fill_id TEXT,
                    filled_at TEXT NOT NULL, delta TEXT NOT NULL, price TEXT, processed INTEGER DEFAULT 0,
                    PRIMARY KEY(account, order_id, symbol, fill_id));
                CREATE INDEX IF NOT EXISTS position_log_pending ON position_log_fills(account, filled_at)
                    WHERE processed=0;
            """)
        self.flush()

    def flush(self):
        try:
            self._append_committed()
            self.write_error = ""
        except (OSError, sqlite3.Error) as exc:
            self.write_error = f"Position log write failed: {exc}"
            logging.getLogger(__name__).warning("%s", self.write_error)

    def _append_committed(self):
        """Append committed events. Retry an interrupted append from its last committed byte."""
        with sqlite3.connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("SELECT entry_id, file_size FROM position_log_cursor").fetchone()
            with self.path.open("a+b") as stream:
                size = stream.tell()
                entry_id, offset = cursor if cursor else (0, size)
                if size < offset:  # File was removed or truncated. Restore the recorded history.
                    entry_id, offset = 0, 0
                if size != offset:
                    stream.truncate(offset)
                    stream.seek(0, os.SEEK_END)
                for entry_id, line in db.execute(
                    "SELECT id, line FROM position_log_entries WHERE id > ? ORDER BY id", (entry_id,)
                ):
                    stream.write((line + "\n").encode("utf-8"))
                if cursor != (entry_id, stream.tell()):
                    stream.flush()
                    os.fsync(stream.fileno())
                    db.execute("INSERT OR REPLACE INTO position_log_cursor VALUES(1,?,?)",
                               (entry_id, stream.tell()))

    def observe_broker(self, account, positions, orders, as_of):
        """Reconcile successful broker snapshots, including fills between two refreshes."""
        quantities = {}
        for position in positions:
            if position.get("instrument-type", "Equity") != "Equity":
                continue
            symbol = position.get("symbol") or position.get("underlying-symbol")
            quantity = Decimal(str(position["quantity"]))
            if position.get("quantity-direction", "Long").lower() == "short":
                quantity = -abs(quantity)
            quantities[symbol] = quantities.get(symbol, Decimal(0)) + quantity
        current = {s: q for s, q in quantities.items() if q}
        observed_at = as_of.astimezone(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""
                SELECT quantities, started_at, snapshot_cutoffs FROM position_log_accounts WHERE account=?
            """, (account,)).fetchone()
            started_at = previous[1] if previous else observed_at
            cutoffs = json.loads(previous[2]) if previous else {}
            if previous:
                self._save_fills(db, account, orders, started_at, cutoffs)
                fills = {}
                for symbol, row_id, timestamp, delta, price in db.execute("""
                    SELECT symbol, rowid, filled_at, delta, price FROM position_log_fills
                    WHERE account=? AND processed=0 ORDER BY filled_at, length(order_id), order_id, rowid
                """, (account,)):
                    fills.setdefault(symbol, []).append((row_id, timestamp, Decimal(delta), price))
                before = {s: Decimal(q) for s, q in json.loads(previous[0]).items()}
                for symbol in sorted(before.keys() | current.keys() | fills.keys()):
                    old, new = before.get(symbol, 0), current.get(symbol, 0)
                    changes = fills.get(symbol, [])
                    # Orders and holdings arrive in separate requests. Retain fills ahead
                    # of the holdings snapshot until a later refresh confirms them.
                    balance, matched = old, 0
                    for index, (_, _, delta, _) in enumerate(changes, 1):
                        balance += delta
                        if balance == new:
                            matched = index
                    if matched:
                        changes = changes[:matched]
                        for _, timestamp, delta, price in changes:
                            self._record_transition(db, symbol, old, old + delta, timestamp, price, "fill")
                            old += delta
                    elif old != new:
                        self._record_transition(db, symbol, old, new, observed_at, None, "broker_snapshot")
                        # Later reports may include the fills already reflected in this snapshot.
                        cutoffs[symbol] = observed_at
                    else:
                        continue
                    db.executemany("UPDATE position_log_fills SET processed=1 WHERE rowid=?",
                                   [(row_id,) for row_id, _, _, _ in changes])

            db.execute("INSERT OR REPLACE INTO position_log_accounts VALUES(?,?,?,?)",
                       (account, json.dumps({s: str(q) for s, q in current.items()}),
                        started_at, json.dumps(cutoffs)))
        self.flush()

    @staticmethod
    def _save_fills(db, account, orders, started_at, cutoffs):
        for item in orders:
            order = item.get("order", item)
            for leg in order.get("legs") or []:
                action = leg.get("action")
                if leg.get("instrument-type", "Equity") != "Equity" or action not in (
                    "Buy to Open", "Buy to Close", "Sell to Open", "Sell to Close"
                ):
                    continue
                for fill in leg.get("fills") or []:
                    stamp = fill.get("filled-at")
                    fill_id = fill.get("fill-id") or fill.get("ext-exec-id")
                    if not stamp or not fill_id or not fill.get("quantity"):
                        continue
                    timestamp = datetime.fromisoformat(stamp).astimezone(timezone.utc).isoformat()
                    if timestamp <= cutoffs.get(leg["symbol"], started_at):
                        continue
                    qty = Decimal(str(fill["quantity"]))
                    delta = qty if action.startswith("Buy") else -qty
                    db.execute("""
                        INSERT OR IGNORE INTO position_log_fills
                            (account, order_id, symbol, fill_id, filled_at, delta, price) VALUES(?,?,?,?,?,?,?)
                    """, (account, order["id"], leg["symbol"], fill_id, timestamp, str(delta),
                          fill.get("fill-price")))

    @staticmethod
    def _record_transition(db, symbol, old, new, timestamp, price, source):
        if old and (new == 0 or (old > 0) != (new > 0)):
            record_position(db, timestamp, "Production", "CLOSED", "LONG" if old > 0 else "SHORT",
                            symbol, abs(old), price=price, source=source)
        if new and (old == 0 or (old > 0) != (new > 0)):
            record_position(db, timestamp, "Production", "OPENED", "LONG" if new > 0 else "SHORT",
                            symbol, abs(new), price=price, source=source)
