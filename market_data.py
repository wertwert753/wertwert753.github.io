"""Historical market data retrieval using yfinance. Lazy import keeps core tests lightweight."""
from __future__ import annotations
import pandas as pd


def fetch_most_active_symbols(count=100) -> list[str]:
    """Return the Yahoo Finance Most Actives universe used by the dashboard."""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed. Install the project requirements.") from exc

    result = yf.screen("most_actives", count=count)
    quotes = result.get("quotes", []) if isinstance(result, dict) else []
    symbols = []
    for quote in quotes:
        symbol = str(quote.get("symbol", "")).strip().upper()
        if symbol and symbol.replace("-", "").replace(".", "").isalnum() and symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) == count:
            break
    if len(symbols) < count:
        #raise RuntimeError(f"Yahoo returned only {len(symbols)} usable Most Actives symbols.")
        print(f"Only {len(symbols)} symbols retrieved")
    return symbols


def fetch_yfinance_history(symbols, period="60d", interval="5m") -> pd.DataFrame:
    """Download adjusted historical Close prices for multiple ticker symbols.

    Returns a DataFrame indexed by timestamp with one uppercase column per ticker.
    yfinance intraday history is limited by Yahoo's availability rules; use supported
    period/interval combinations and verify data before relying on it.
    """
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed. Run: python -m pip install yfinance") from exc

    cleaned = []
    for raw in symbols:
        symbol = str(raw).strip().upper()
        if symbol and symbol not in cleaned:
            if not symbol.replace("-", "").replace(".", "").isalnum():
                raise ValueError(f"Invalid ticker symbol: {symbol}")
            cleaned.append(symbol)
    if not cleaned:
        raise ValueError("Enter at least one ticker symbol.")

    data = yf.download(
        tickers=cleaned,
        period=period,
        interval=interval,
        auto_adjust=True,
        progress=False,
        group_by="column",
        threads=True,
    )
    if data is None or data.empty:
        raise RuntimeError("Yahoo Finance returned no data. Try a shorter period, a supported interval, or different symbols.")

    if isinstance(data.columns, pd.MultiIndex):
        # yfinance versions commonly return (Price, Ticker), but tolerate reverse order.
        close = None
        if "Close" in data.columns.get_level_values(0):
            close = data.xs("Close", axis=1, level=0, drop_level=True)
        elif "Close" in data.columns.get_level_values(-1):
            close = data.xs("Close", axis=1, level=-1, drop_level=True)
        if close is None:
            raise RuntimeError("Yahoo Finance response did not include Close prices.")
        prices = close.copy()
    else:
        if "Close" not in data.columns:
            raise RuntimeError("Yahoo Finance response did not include a Close column.")
        prices = data[["Close"]].rename(columns={"Close": cleaned[0]})

    if isinstance(prices, pd.Series):
        prices = prices.to_frame(name=cleaned[0])
    prices = prices.apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all")
    prices.columns = [str(c).upper() for c in prices.columns]
    #prices = prices.apply(pd.to_numeric, errors="coerce").dropna(how="all")
    prices = prices.loc[:, ~prices.columns.duplicated()]
    if prices.empty:
        raise RuntimeError("Downloaded data contained no usable close prices.")
    # yfinance intraday timestamps are generally timezone-aware. Keep them aware so the
    # strategy can convert correctly to America/Los_Angeles.
    return prices.sort_index()


def fetch_yfinance_ohlc(symbols, period="60d", interval="5m", auto_adjust=True) -> dict[str, pd.DataFrame]:
    """Download real OHLC candles per ticker for HalfTrend visualization."""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed. Install it with pip.") from exc
    cleaned = list(dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()))
    if not cleaned:
        raise ValueError("Enter at least one ticker symbol.")
    raw = yf.download(tickers=cleaned, period=period, interval=interval,
                      auto_adjust=auto_adjust, progress=False, group_by="column", threads=True)
    if raw is None or raw.empty:
        raise RuntimeError("Yahoo Finance returned no OHLC candles. Try a shorter period or another interval.")
    def extract(frame, symbol):
        if frame is None or frame.empty:
            return None
        fields = {}
        for field in ("Open", "High", "Low", "Close"):
            if isinstance(frame.columns, pd.MultiIndex):
                if field in frame.columns.get_level_values(0):
                    block = frame.xs(field, axis=1, level=0, drop_level=True)
                elif field in frame.columns.get_level_values(-1):
                    block = frame.xs(field, axis=1, level=-1, drop_level=True)
                else:
                    return None
                if isinstance(block, pd.Series):
                    fields[field] = block
                elif symbol in block.columns:
                    fields[field] = block[symbol]
                elif block.shape[1] == 1:
                    fields[field] = block.iloc[:, 0]
                else:
                    return None
            elif field in frame.columns:
                fields[field] = frame[field]
            else:
                return None
        result = pd.DataFrame(fields, index=frame.index).apply(pd.to_numeric, errors="coerce")
        result = result.dropna(subset=["Open", "High", "Low", "Close"]).sort_index()
        return result if not result.empty else None

    output = {}
    for symbol in cleaned:
        parsed = extract(raw, symbol)
        if parsed is None:
            # Multi-symbol Yahoo responses can omit individual tickers. Retry only
            # those names alone so one missing symbol does not hide other candles.
            try:
                single = yf.download(tickers=symbol, period=period, interval=interval,
                                     auto_adjust=auto_adjust, progress=False,
                                     group_by="column", threads=False)
                parsed = extract(single, symbol)
            except Exception:
                parsed = None
        if parsed is not None:
            output[symbol] = parsed
    if not output:
        raise RuntimeError("Yahoo Finance returned no usable OHLC candles.")
    return output


def fetch_yfinance_delayed_closes(symbols, period="1mo") -> dict[str, tuple[float, pd.Timestamp | None]]:
    """Fetch Yahoo daily closes, then fast-info prices for delayed Sandbox fallback."""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed. Install it with pip.") from exc
    output = {}
    for raw_symbol in dict.fromkeys(str(s).strip().upper() for s in symbols if str(s).strip()):
        try:
            frame = yf.Ticker(raw_symbol).history(period=period, interval="1d", auto_adjust=False)
            if frame is not None and not frame.empty and "Close" in frame:
                close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
                if not close.empty:
                    output[raw_symbol] = (float(close.iloc[-1]), pd.Timestamp(close.index[-1]))
                    continue
        except Exception:
            pass
        try:
            info = yf.Ticker(raw_symbol).fast_info
            price = info.get("last_price") or info.get("previous_close")
            if price is not None and float(price) > 0:
                stamp = info.get("last_price_time")
                output[raw_symbol] = (float(price), pd.Timestamp(stamp) if stamp else None)
        except Exception:
            pass
    return output
