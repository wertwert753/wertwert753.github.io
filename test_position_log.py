import gc
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from trading_service import ET, TradingService


class PositionLogTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.addCleanup(gc.collect)
        self.service = TradingService(Path(directory) / "test.sqlite3")
        self.log = self.service.position_log.path
        self.now = datetime(2026, 11, 23, 10, 0, tzinfo=ET)
        clock = self.enterContext(patch("trading_service.datetime", wraps=datetime))
        clock.now.side_effect = lambda tz: self.now.astimezone(tz)

    def simulate(self, **sides):
        signals = {symbol: {"signal": side, "signal_time": self.now.isoformat()}
                   for symbol, side in sides.items()}
        self.service._simulator(self.service.settings, signals, {s: 10 for s in signals})

    def test_signal_manual_and_end_of_day_closes(self):
        self.simulate(AAA="BUY", BBB="SELL")
        self.simulate(AAA="BUY", BBB="SELL")
        self.now = self.now.replace(minute=1)
        self.simulate(AAA="SELL")
        self.service.yahoo_prices = {"BBB": 9}
        self.service.close_all_positions()
        self.service.close_all_positions()
        self.now = self.now.replace(minute=2)
        self.simulate(CCC="SELL")
        self.now = self.now.replace(hour=15, minute=46)
        self.simulate()
        self.simulate()
        lines = self.log.read_text().splitlines()
        self.assertEqual([line.split()[2:5] for line in lines], [
            ["POSITION_OPENED", "LONG", "AAA"], ["POSITION_OPENED", "SHORT", "BBB"],
            ["POSITION_CLOSED", "LONG", "AAA"], ["POSITION_CLOSED", "SHORT", "BBB"],
            ["POSITION_OPENED", "SHORT", "CCC"], ["POSITION_CLOSED", "SHORT", "CCC"],
        ])
        self.assertIn("realized_pnl=10.00 source=manual", lines[3])
        self.assertIn("source=end_of_day", lines[-1])

    def test_log_recovery_and_rolled_back_trade(self):
        with sqlite3.connect(self.service.db_path) as db:
            db.execute("""CREATE TRIGGER fail_trade BEFORE INSERT ON sandbox_sim_equity
                          BEGIN SELECT RAISE(ABORT, 'test rollback'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.simulate(AAA="BUY")
        self.service.position_log.flush()
        self.assertEqual(self.log.read_bytes(), b"")
        with sqlite3.connect(self.service.db_path) as db:
            db.execute("DROP TRIGGER fail_trade")
        with patch.object(Path, "open", side_effect=PermissionError("locked")), self.assertLogs("position_log"):
            self.simulate(AAA="BUY")
        self.assertEqual(self.log.read_bytes(), b"")
        self.service = TradingService(self.service.db_path)
        expected = self.log.read_bytes()
        self.assertEqual(expected.count(b"POSITION_OPENED"), 1)
        with self.log.open("ab") as stream:
            stream.write(b"partial append")
        self.service.position_log.flush()
        self.assertEqual(self.log.read_bytes(), expected)

    def test_broker_snapshots_survive_failed_refresh_and_restart(self):
        broker = Mock()
        broker.get_balances.return_value = {}
        broker.get_orders.return_value = []
        def refresh():
            self.service.settings["environment"] = self.service.environment = "Production"
            self.service.client, self.service.account_number = broker, "test-account"
            with (patch("trading_service.fetch_most_active_symbols", return_value=[]),
                  patch("trading_service.fetch_yfinance_ohlc", return_value={}),
                  patch("trading_service.fetch_yfinance_delayed_closes", return_value={}),
                  patch.object(self.service, "_broker_quotes", return_value={})):
                self.service.refresh()
        broker.get_positions.return_value = []
        refresh()
        broker.get_orders.return_value = [{"status": "Live", "legs": [{"symbol": "AAA"}]}]
        refresh()
        self.assertEqual(self.log.read_bytes(), b"")
        broker.get_positions.return_value = [{"symbol": "AAA", "quantity": "3", "quantity-direction": "Short"}]
        refresh()
        broker.get_positions.side_effect = RuntimeError("offline")
        refresh()
        self.service = TradingService(self.service.db_path)
        broker.get_positions.side_effect = None
        broker.get_positions.return_value[0]["quantity"] = "1"
        refresh()
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        broker.get_positions.return_value = []
        refresh()
        refresh()
        lines = self.log.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("Production POSITION_OPENED SHORT AAA quantity=3", lines[0])
        self.assertIn("Production POSITION_CLOSED SHORT AAA quantity=1 source=broker_snapshot", lines[1])

    def test_broker_round_trip_and_partial_fills_between_polls(self):
        log = self.service.position_log
        log.observe_broker("test", [], [], self.now)
        def order(order_id, action, executions):
            fills = [{"fill-id": f"{order_id}-{minute}", "quantity": qty, "fill-price": price,
                      "filled-at": self.now.replace(minute=minute).astimezone(timezone.utc).isoformat()}
                     for minute, qty, price in executions]
            return {"id": order_id, "legs": [{"symbol": "AAA", "action": action, "fills": fills}]}
        opening = order("1", "Buy to Open", [(1, "0.3", "12.50"), (2, "0.2", "12.51")])
        closing = order("2", "Sell to Close", [(3, "0.4", "13.00"), (4, "0.1", "13.01")])
        self.now = self.now.replace(minute=2)
        log.observe_broker("test", [], [opening], self.now)
        self.assertEqual(self.log.read_bytes(), b"")
        self.now = self.now.replace(minute=5)
        log = TradingService(self.service.db_path).position_log
        log.observe_broker("test", [], [closing], self.now)
        log.observe_broker("test", [], [closing, opening], self.now)
        lines = self.log.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("POSITION_OPENED LONG AAA quantity=0.3 price=12.50 source=fill", lines[0])
        self.assertIn("POSITION_CLOSED LONG AAA quantity=0.1 price=13.01 source=fill", lines[1])
