import unittest
import pandas as pd
import numpy as np
from strategy import crossover_signals, run_csv_backtest
from tastytrade_client_orders import TastytradeClient, SANDBOX_URL, PRODUCTION_URL


class StrategyTests(unittest.TestCase):
    def test_invalid_halftrend_parameters(self):
        with self.assertRaises(ValueError):
            crossover_signals(pd.Series([1, 2, 3]), 0, 2.0)

    def test_halftrend_ohlc_reversal_signals(self):
        close = np.r_[np.linspace(100, 120, 30), np.linspace(120, 90, 30), np.linspace(90, 125, 30)]
        candles = pd.DataFrame({"Open": close, "High": close + 0.4,
                                "Low": close - 0.4, "Close": close})
        result = crossover_signals(candles, amplitude=2, channel_deviation=2.0)
        self.assertIn("trend", result.columns)
        self.assertIn("halftrend", result.columns)
        self.assertTrue(result["buy_signal"].any())
        self.assertTrue(result["sell_signal"].any())
        self.assertTrue(set(result["signal"].unique()).issubset({"BUY", "SELL", "HOLD"}))

    def test_halftrend_requires_ohlc_fields(self):
        with self.assertRaises(ValueError):
            crossover_signals(pd.DataFrame({"Close": [1, 2, 3]}))

    def test_backtest_shape_and_no_negative_equity(self):
        idx = pd.date_range("2026-10-12 06:30", periods=80, freq="min", tz="America/Los_Angeles")
        series = np.r_[np.linspace(100, 120, 40), np.linspace(120, 95, 40)]
        prices = pd.DataFrame({"AAA": series, "BBB": series * 0.5}, index=idx)
        eq, trades, open_positions = run_csv_backtest(prices, 10000, 2, 2.0, 2, 20, 0, 0)
        self.assertEqual(len(eq), len(prices))
        self.assertIn("P&L", trades.columns)
        self.assertTrue((eq["Equity"] >= 0).all())

    def test_daily_bars_can_backtest(self):
        idx = pd.date_range("2026-01-01", periods=50, freq="D")
        prices = pd.DataFrame({"AAA": np.linspace(10, 20, 50)}, index=idx)
        eq, _, _ = run_csv_backtest(prices, 1000, 2, 2.0, 2, daily_bars=True)
        self.assertEqual(len(eq), len(prices))

    def test_bad_capital_rejected(self):
        with self.assertRaises(ValueError):
            run_csv_backtest(pd.DataFrame({"A": [10, 11]}), 0, 2, 2.0, 2)

    def test_client_only_accepts_known_hosts(self):
        with self.assertRaises(ValueError):
            TastytradeClient("secret", "refresh", base_url="https://not-tastytrade.example")

    def test_order_builder_limit_payload(self):
        order = TastytradeClient.build_equity_order("aapl", "Buy to Open", 2, 150.25)
        self.assertEqual(order["legs"][0]["symbol"], "AAPL")
        self.assertEqual(order["order-type"], "Limit")
        self.assertEqual(order["price-effect"], "Debit")
        self.assertTrue(order["external-identifier"])

    def test_submission_requires_explicit_confirmation(self):
        client = TastytradeClient("secret", "refresh", base_url=PRODUCTION_URL)
        order = TastytradeClient.build_equity_order("AAPL", "Buy to Open", 1, 100)
        with self.assertRaises(PermissionError):
            client.submit_order("ACCOUNT", order, confirm_live=False)

    def test_order_notional_cap_checked_before_network(self):
        client = TastytradeClient("secret", "refresh", base_url=SANDBOX_URL)
        order = TastytradeClient.build_equity_order("AAPL", "Buy to Open", 10, 200)
        with self.assertRaises(ValueError):
            client.submit_order("ACCOUNT", order, confirm_live=True, max_order_notional=1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
