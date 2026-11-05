# StockTrader

A local FastAPI application for Yahoo Finance HalfTrend candles, tastytrade account monitoring, and FinBERT-measured sentiment in financial articles.

## Run

Install dependencies and start StockTrader from the project directory:

```powershell
python -m pip install -r requirements.txt
python stocktrader.py
```

StockTrader listens on all network interfaces at port `8765` by default and does not require a username or password. The host computer has full controls at `http://127.0.0.1:8765`; other computers can view the dashboard but cannot change settings, run sentiment analysis, or control trading. Use the host computer's LAN address, such as `http://192.168.1.25:8765`, from another device. The host firewall must allow inbound connections on a trusted network. Do not expose the trading service to the public internet.

Open `http://127.0.0.1:8765` on the host computer. The app refreshes the cached dashboard on a 60-second background cycle; opening the page does not execute the full UI script again.

Set the local login and OAuth credentials in the environment. Sandbox is the default:

```powershell
$env:TASTYTRADE_CLIENT_SECRET = "your-sandbox-client-secret"
$env:TASTYTRADE_REFRESH_TOKEN = "your-sandbox-refresh-token"
```

For production, use separate credentials: `TASTYTRADE_PROD_CLIENT_SECRET` and `TASTYTRADE_PROD_REFRESH_TOKEN` (plus optional `TASTYTRADE_PROD_CLIENT_ID`). Create OAuth credentials through tastytrade's API settings. Never place secrets in source files.

After setting any brokerage credentials, start StockTrader:

```powershell
python stocktrader.py
```

Sandbox is a local virtual portfolio and does not connect to or submit orders to a tastytrade sandbox account. It uses Yahoo candles as simulated fill and mark references. Production mode connects to tastytrade for account data, live quotes, order preflight, and order submission.

## Operating modes

- The Sandbox simulator runs locally without a tastytrade account. It records virtual HalfTrend positions, daily and cumulative P&L, and a trade log using a default $500 virtual starting capital and configurable allocation. Virtual fills and marks use Yahoo reference prices and exclude commissions and slippage. Simulator positions remain open overnight. Its ledger persists in SQLite across restarts.
- Sandbox displays a virtual equity-over-time plot from the simulator's persisted equity snapshots. Production displays tastytrade account net liquidation value over time. Both charts can be filtered with the equity start-date control.
- Production starts in monitor-only mode. Click **Start automation** to allow real orders; Production also requires the confirmation checkbox and the exact phrase `ENABLE LIVE TRADING`. The local service runs the trading cycle every 60 seconds while it is open. It pauses new entries and begins closing open equity positions at 12:45 p.m. Pacific, requests cancellation of working equity orders before closing, and dry-runs each close order. Keep the service running through the closeout and verify broker fills; unsupported positions and rejected orders are reported for attention.
- The default strategy uses 1-minute Yahoo candles and the project's HalfTrend calculation (amplitude 2, channel deviation 2). Buy signals open longs when flat or cover shorts; sell signals open shorts when flat or close longs. A reversal closes an existing opposite position; it does not immediately flip into a new position. Sandbox simulator positions use fractional virtual quantities. Production short entries use whole shares; eligible Production long entries and exits use tastytrade Notional Market orders, with the calculated share quantity converted to a dollar amount at the current ask/bid. Fractional orders still pass tastytrade's order dry-run before submission. Production quantities are sized from account equity, available buying power, per-position allocation, exposure caps, and the per-order dollar cap. Short sale availability and margin requirements remain subject to tastytrade account permissions and broker preflight.
- Production orders use tastytrade bid/ask quotes and pass broker dry-run before submission. Existing positions/orders, market hours, and the daily loss limit are checked first.
- Production quote requests retry transient 429/5xx failures and fall back to Yahoo candle or daily-close references when tastytrade quotes are unavailable. Yahoo prices can be delayed and their age may be unknown; the app accepts them as reference prices for automated orders, as configured. Verify the reference source and broker order status.
- Account history is stored locally in `trading_dashboard.sqlite3`. The app-start P&L baseline is the first net-liquidation snapshot after connecting; it is not a tax or performance statement.

## Article sentiment page

The **Article Sentiment** tab analyzes recent Yahoo Finance search articles for the same Most Actives and held-stock list observed by the dashboard. It shows article-level tone, a -100 to +100 score, confidence, per-stock averages, and links to the articles. Results use the available headlines and summaries, not full article text. Successfully scored articles are saved to the local database as per-stock confidence-weighted sentiment snapshots.

Recent sentiment is combined with HalfTrend signals to prioritize order processing: positive scores favor BUY signals and negative scores favor SELL signals, with stronger scores and higher confidence ranked first. Sentiment never creates a trade or changes a HalfTrend direction, and snapshots older than 24 hours are ignored. Re-run analysis on the Article Sentiment tab to refresh the ranking data.

Article scoring uses the ProsusAI FinBERT financial sentiment classifier through Transformers and PyTorch. Install the project requirements; the model files are downloaded from Hugging Face the first time you run sentiment analysis and cached locally. Inference then runs on the StockTrader computer using its available CPU/GPU. The app scores article headlines and summaries and saves aggregated results locally for strategy ranking. FinBERT measures general financial tone and does not determine whether the tone is specifically about the selected stock.

While StockTrader is running, it automatically analyzes three recent articles per observed stock at 6:00 a.m. Pacific on U.S. market weekdays. It checks the top Yahoo Finance result for each stock hourly from 7:00 a.m. through noon Pacific, scoring only article IDs not already analyzed that day. A fresh daily article set is fetched the following market day. The schedule skips weekends and standard U.S. market holidays, and it catches up the daily batch if StockTrader starts later in the morning.

## Data and operational limits

Yahoo supplies the candles, Most Actives list, and Sandbox simulator price references. Production mode uses tastytrade for account data, live quotes, order preflight, and order submission. Sandbox mode does not read the tastytrade sandbox account. The downloader retries symbols omitted from a multi-symbol Yahoo response individually; Yahoo may still have no usable price for a symbol. Production quote GET requests retry transient 429/5xx failures with capped exponential backoff and jitter. The dashboard refreshes on a 60-second cadence and is not a low-latency execution system.

Production strategy entries use day limit orders unless a fractional order is needed, in which case the app uses a Notional Market order. The app does not add to an existing same-direction position, place stop orders, or automatically cancel/replace orders. Short selling can involve substantial losses and requires tastytrade account approval and buying power; the app caps notional exposure, but that cap does not limit losses if a shorted stock rises. Tastytrade credentials and market-data entitlements are account-specific.
