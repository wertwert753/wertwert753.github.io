"""Independent moving-average crossover backtest for imported price history."""
from __future__ import annotations
import math
import pandas as pd
import numpy as np

def crossover_signals(candles: pd.DataFrame | pd.Series, amplitude: int = 2,
                      channel_deviation: float = 2.0) -> pd.DataFrame:
    """Calculate non-lookahead HalfTrend states and reversal signals.

    Accepts either a close-price Series or an OHLC DataFrame. OHLC column names
    are case-insensitive. For a close-only Series, high and low are approximated
    by close, so the result is necessarily less faithful than true OHLC data.
    """
    if not isinstance(amplitude, (int, np.integer)) or amplitude < 1:
        raise ValueError("amplitude must be an integer >= 1")
    if not np.isfinite(channel_deviation) or channel_deviation < 0:
        raise ValueError("channel_deviation must be finite and >= 0")

    if isinstance(candles, pd.Series):
        close = pd.to_numeric(candles, errors="coerce")
        df = pd.DataFrame({"High": close, "Low": close, "Close": close}, index=close.index)
    else:
        df = candles.copy()
        lookup = {str(c).strip().lower(): c for c in df.columns}
        required = ("high", "low", "close")
        missing = [c for c in required if c not in lookup]
        if missing:
            raise ValueError(f"OHLC data is missing required columns: {', '.join(missing)}")
        df = df.rename(columns={lookup[k]: k.title() for k in required})
        for col in ("High", "Low", "Close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if df.empty:
        for col in ("ht", "halftrend", "trend", "arrowUp", "arrowDown", "atr", "atr_high", "atr_low", "signal", "buy_signal", "sell_signal"):
            df[col] = pd.Series(index=df.index, dtype="object" if col == "signal" else "float64")
        return df

    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(100, min_periods=1).mean()
    atr2 = atr / 2.0
    deviation = channel_deviation * atr2

    high_price = high.rolling(amplitude, min_periods=amplitude).max()
    low_price = low.rolling(amplitude, min_periods=amplitude).min()
    high_ma = high.rolling(amplitude, min_periods=amplitude).mean()
    low_ma = low.rolling(amplitude, min_periods=amplitude).mean()

    n = len(df)
    trend_values = np.zeros(n, dtype=np.int8)  # 0 = uptrend, 1 = downtrend
    ht = np.full(n, np.nan)
    arrow_up = np.full(n, np.nan)
    arrow_down = np.full(n, np.nan)
    atr_high = np.full(n, np.nan)
    atr_low = np.full(n, np.nan)
    up_values = np.full(n, np.nan)
    down_values = np.full(n, np.nan)

    trend, next_trend = 0, 0
    first_low = low.iloc[0]
    first_high = high.iloc[0]
    max_low = float(first_low) if pd.notna(first_low) else 0.0
    min_high = float(first_high) if pd.notna(first_high) else 0.0
    up = down = np.nan

    for i in range(n):
        if i > 0:
            if next_trend == 1:
                if pd.notna(low_price.iloc[i]):
                    max_low = max(float(low_price.iloc[i]), max_low)
                if (pd.notna(high_ma.iloc[i]) and pd.notna(close.iloc[i]) and pd.notna(low.iloc[i-1])
                    and high_ma.iloc[i] < max_low and close.iloc[i] < low.iloc[i-1]):
                    trend, next_trend = 1, 0
                    if pd.notna(high_price.iloc[i]): min_high = float(high_price.iloc[i])
            else:
                if pd.notna(high_price.iloc[i]):
                    min_high = min(float(high_price.iloc[i]), min_high)
                if (pd.notna(low_ma.iloc[i]) and pd.notna(close.iloc[i]) and pd.notna(high.iloc[i-1])
                    and low_ma.iloc[i] > min_high and close.iloc[i] > high.iloc[i-1]):
                    trend, next_trend = 0, 1
                    if pd.notna(low_price.iloc[i]): max_low = float(low_price.iloc[i])

        previous_trend = int(trend_values[i-1]) if i > 0 else trend
        if trend == 0:
            if i > 0 and previous_trend != 0:
                up = down if pd.isna(down) else down
                arrow_up[i] = up
            else:
                previous_up = up_values[i-1] if i > 0 else np.nan
                up = max(max_low, previous_up) if pd.notna(previous_up) else max_low
            level = up
        else:
            if i > 0 and previous_trend != 1:
                down = up if pd.notna(up) else down
                arrow_down[i] = down
            else:
                previous_down = down_values[i-1] if i > 0 else np.nan
                down = min(min_high, previous_down) if pd.notna(previous_down) else min_high
            level = down

        trend_values[i] = trend
        ht[i] = level
        up_values[i] = up
        down_values[i] = down
        if pd.notna(level) and pd.notna(deviation.iloc[i]):
            atr_high[i] = level + deviation.iloc[i]
            atr_low[i] = level - deviation.iloc[i]

    df["ht"] = ht
    df["halftrend"] = trend_values
    df["trend"] = trend_values  # compatibility alias: 0 uptrend, 1 downtrend
    df["arrowUp"] = arrow_up
    df["arrowDown"] = arrow_down
    df["atr"] = atr.to_numpy()
    df["atr_high"] = atr_high
    df["atr_low"] = atr_low
    df["buy_signal"] = pd.notna(df["arrowUp"])
    df["sell_signal"] = pd.notna(df["arrowDown"])
    df["signal"] = np.select([df["buy_signal"], df["sell_signal"]], ["BUY", "SELL"], default="")
    # Lower-case close alias keeps the signal table compatible with OHLC input.
    df["close"] = close.to_numpy()
    return df

def run_csv_backtest(prices: pd.DataFrame, starting_cash: float, amplitude: int, channel_deviation: float,
                     stop_pct: float, max_allocation_pct: float = 10.0,
                     fee_per_order: float = 0.0, slippage_bps: float = 1.0,
                     daily_bars: bool = False):
    if starting_cash <= 0:
        raise ValueError("Starting capital must be positive.")
    if not 0 < stop_pct <= 25:
        raise ValueError("Stop loss must be between 0 and 25 percent.")
    if not 0 < max_allocation_pct <= 100:
        raise ValueError("Allocation must be between 0 and 100 percent.")
    if prices.empty or len(prices.columns) == 0:
        raise ValueError("No price series supplied.")
    prices = prices.copy().sort_index().apply(pd.to_numeric, errors="coerce").ffill().dropna(how="all")
    signals = {s: crossover_signals(prices[s], amplitude, channel_deviation) for s in prices.columns}
    cash = float(starting_cash)
    positions, trades, equity_rows = {}, [], []
    last_prices = {}

    for ts, row in prices.iterrows():
        # Mark each symbol at its current close.
        for symbol in prices.columns:
            if pd.notna(row[symbol]):
                last_prices[symbol] = float(row[symbol])

        # Stops and reversals close the current direction before considering entries.
        for symbol in list(positions):
            mark = last_prices.get(symbol)
            if mark is None:
                continue
            pos = positions[symbol]
            sig = signals[symbol].loc[ts]
            stop_hit = mark <= pos["stop"] if pos["qty"] > 0 else mark >= pos["stop"]
            reverse = bool(sig.sell_signal) if pos["qty"] > 0 else bool(sig.buy_signal)
            reason = "Stop-loss" if stop_hit else ("Halftrend Reversals Signal" if reverse else "")
            if reason:
                direction = 1 if pos["qty"] > 0 else -1
                fill = mark * (1 - direction * slippage_bps / 10000)
                cash += pos["qty"] * fill - fee_per_order
                trades.append({"Symbol": symbol, "Side": "Long" if direction > 0 else "Short", "Qty": abs(pos["qty"]), "Entry time": pos["entry_time"],
                               "Entry fill": round(pos["entry"], 4), "Exit time": str(ts),
                               "Exit fill": round(fill, 4),
                               "P&L": round(direction * (fill - pos["entry"]) * abs(pos["qty"]) - 2 * fee_per_order, 2),
                               "Reason": reason})
                del positions[symbol]

        # Only enter during regular weekday entry window (timestamp is expected to be Pacific-local
        # or timezone-aware; naive CSV timestamps are interpreted as Pacific local time).
        dt = pd.Timestamp(ts)
        if dt.tzinfo is None:
            dt = dt.tz_localize("America/Los_Angeles")
        else:
            dt = dt.tz_convert("America/Los_Angeles")
        allow_entry = bool(daily_bars) or (dt.weekday() < 5 and (dt.hour, dt.minute) >= (6, 30) and (dt.hour, dt.minute) < (11, 30))
        if allow_entry:
            for symbol in prices.columns:
                if symbol in positions or symbol not in last_prices:
                    continue
                sig = signals[symbol].loc[ts]
                if sig is None or not (bool(sig.buy_signal) or bool(sig.sell_signal)):
                    continue
                direction = 1 if bool(sig.buy_signal) else -1
                mark = last_prices[symbol]
                market_value = sum(p["qty"] * last_prices.get(s, p["entry"]) for s, p in positions.items())
                equity = cash + market_value
                allocation = min(cash, equity * max_allocation_pct / 100)
                fill = mark * (1 + direction * slippage_bps / 10000)
                qty = math.floor(max(0, allocation - fee_per_order) / fill)
                if qty < 1:
                    continue
                cash -= direction * qty * fill + fee_per_order
                positions[symbol] = {"qty": direction * qty, "entry": fill, "entry_time": str(ts),
                                     "stop": fill * (1 - direction * stop_pct / 100)}

        market_value = sum(p["qty"] * last_prices.get(s, p["entry"]) for s, p in positions.items())
        unrealized = sum((last_prices.get(s, p["entry"]) - p["entry"]) * p["qty"] for s, p in positions.items())
        equity_rows.append({"Time": str(ts), "Equity": cash + market_value, "Cash": cash,
                            "Market value": market_value, "Unrealized P&L": unrealized,
                            "Realized P&L": sum(t["P&L"] for t in trades)})

    eq = pd.DataFrame(equity_rows)
    trade_df = pd.DataFrame(trades, columns=["Symbol", "Side", "Qty", "Entry time", "Entry fill", "Exit time", "Exit fill", "P&L", "Reason"])
    open_df = pd.DataFrame([{"Symbol": s, "Side": "Long" if p["qty"] > 0 else "Short", "Qty": abs(p["qty"]), "Entry": p["entry"],
                             "Last": last_prices.get(s, p["entry"]), "Stop": p["stop"],
                             "Unrealized P&L": (last_prices.get(s, p["entry"]) - p["entry"]) * p["qty"]}
                            for s, p in positions.items()],
                           columns=["Symbol", "Side", "Qty", "Entry", "Last", "Stop", "Unrealized P&L"])
    return eq, trade_df, open_df
