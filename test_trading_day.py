import gc
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

from trading_service import ET, TradingService


class TradingDayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        # Release SQLite connection cycles before Windows removes the test database.
        self.addCleanup(gc.collect)
        self.service = TradingService(Path(directory.name) / "test.sqlite3")
        self.now = datetime(2026, 11, 23, 9, 35, tzinfo=ET)  # Monday
        clock = self.enterContext(patch("trading_service.datetime", wraps=datetime))
        clock.now.side_effect = lambda tz: self.now.astimezone(tz)

    def refresh(self, index, prices):
        bars = pd.DataFrame({key: prices for key in ("Open", "High", "Low", "Close")}, index=index)
        with (
            patch("trading_service.fetch_most_active_symbols", return_value=["TEST"]),
            patch("trading_service.fetch_yfinance_ohlc", return_value={"TEST": bars}),
            patch("trading_service.fetch_yfinance_delayed_closes", return_value={}),
        ):
            return self.service.refresh()

    def test_friday_reversal_is_ignored_until_monday_reversal_completes(self):
        friday = pd.date_range("2026-11-20 15:50", periods=10, freq="min", tz=ET)
        monday = pd.date_range("2026-11-23 09:30", periods=3, freq="min", tz=ET)
        index = friday.append(monday).tz_convert("UTC")
        prices = [10, 11, 12, 13, 14, 13, 12, 11, 10, 9, 9, 9, 10]
        self.now = self.now.replace(minute=32, second=30)
        before = self.refresh(index, prices)
        self.assertEqual(before["signals"][0]["latest_signal"], "SELL")
        self.assertEqual(before["signals"][0]["signal"], "HOLD")
        self.assertEqual(before["positions"], [])
        self.assertEqual(before["simulator"]["trades"], [])

        self.now = self.now.replace(minute=33)
        after = self.refresh(index, prices)
        self.assertEqual(after["signals"][0]["signal_time"], str(index[-1]))
        self.assertEqual(after["simulator"]["trades"][0]["action"], "SIM_OPEN_LONG")

    def test_first_scan_each_day_uses_the_actual_same_day_reversal(self):
        friday = pd.date_range("2026-11-20 15:50", periods=10, freq="min")
        monday = pd.date_range("2026-11-23 09:30", periods=4, freq="min")
        index = friday.append(monday)
        prices = [10, 11, 12, 13, 14, 13, 12, 11, 10, 9, 9, 9, 10, 11]
        first = self.refresh(index, prices)["signals"][0]
        self.assertEqual((first["signal"], first["signal_time"]), ("BUY", str(monday[2])))
        self.assertEqual(self.refresh(index, prices)["signals"][0]["signal"], "HOLD")

        self.now = self.now.replace(day=24)
        tuesday = pd.date_range("2026-11-24 09:30", periods=4, freq="min")
        next_day = self.refresh(index.append(tuesday), prices + [11, 11, 10, 9])
        signal = next_day["signals"][0]
        self.assertEqual((signal["signal"], signal["signal_time"]), ("SELL", str(tuesday[2])))
        self.assertEqual(next_day["positions"], [])
        self.assertEqual(next_day["simulator"]["trades"][0]["action"], "SIM_CLOSE_LONG")

    def test_execution_checks_signal_dates_before_long_and_short_entries(self):
        broker = Mock()
        broker.get_equity.return_value = {}
        broker.build_equity_order.return_value = {}
        broker.dry_run_order.return_value = {}
        self.service.auto_enabled = True
        balance = {"cash-balance": 1000, "net-liquidating-value": 1000}
        prices = {"TEST": 10}
        for side in ("BUY", "SELL"):
            fresh = "2026-11-23 09:31:00"
            frame = pd.DataFrame({"buy_signal": [side == "BUY"], "sell_signal": [side == "SELL"]},
                                 index=pd.DatetimeIndex([fresh]))
            for stamp in ("2026-11-20 15:59:00", "2026-11-23T01:00:00+00:00", "", "invalid", fresh):
                with self.subTest(side=side, stamp=stamp):
                    row = {"symbol": "TEST", "signal": side, "signal_time": stamp,
                           "priority": 0, "sentiment": None}
                    signals = {"TEST": row}
                    self.service._simulator(self.service.settings, signals, prices)
                    self.service._execute_orders(self.service.settings, broker, "TEST", balance, [], [],
                                                 signals, {}, prices, [], {"TEST": frame})
                    if stamp == fresh:
                        position, = self.service._sim_positions(self.service.settings)
                        self.assertEqual(position["quantity"] > 0, side == "BUY")
                        broker.submit_order.assert_called_once()
                        self.assertEqual(broker.build_equity_order.call_args.args[1],
                                         "Buy to Open" if side == "BUY" else "Sell to Open")
                    else:
                        self.assertEqual(self.service._sim_positions(self.service.settings), [])
                        broker.dry_run_order.assert_not_called()
                        broker.submit_order.assert_not_called()
            self.service.close_all_positions()
            broker.reset_mock()
