# TastyTraderBot v4 — yfinance history + tastytrade API

## What changed
- Downloads historical adjusted close prices for multiple tickers using `yfinance`.
- Runs moving-average crossover backtests and shows equity curve, drawdown, closed trades, and open positions.
- Connects to tastytrade using OAuth2 refresh-token credentials.
- Supports manual single-leg equity Day limit orders: dry-run preview first, then separate explicit confirmation to submit.
- Supports long entries/exits (Buy to Open, Sell to Close) and short entries/exits (Sell to Open, Buy to Close). The backtest opens long positions on bullish reversals and short positions on bearish reversals.
- Sandbox is the default. Production credentials are separate and production submission requires a dry-run, review checkbox, exact typed phrase, and an order notional cap.

## Important limitations
- Live order submission is manual only. This is not an autonomous live-trading engine; backtest signals are not automatically routed as orders.
- No automatic live quote feed, live stop-loss monitoring, or broker-side protective order is implemented. Yahoo Finance data is for historical analysis only and may be delayed/incomplete.
- Only single-leg equity limit orders are supported. No options, market orders, cancels, replacements, or automatic position management.
- Position-opening orders (Buy to Open and Sell to Open) are blocked outside weekdays 6:30–11:30 AM America/Los_Angeles. Closing orders are allowed outside that window.
- If order submission times out or returns a server error, refresh broker orders and search for the external identifier before trying again. Never blindly retry; the API does not deduplicate submissions.
- Use sandbox end-to-end first. Live trading can lose money. The notional cap is a safeguard, not a guarantee against losses.

## Install (Windows PowerShell)
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements_tastytraderbot.txt
python -m streamlit run tastytraderbot.py
```

## Install (macOS/Linux)
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements_tastytraderbot.txt
python -m streamlit run tastytraderbot.py
```

## Sandbox credentials
Create a sandbox OAuth app/grant in tastytrade. Set these in your terminal before starting Streamlit:

### Windows PowerShell
```powershell
$env:TASTYTRADE_CLIENT_SECRET="a7c8209f93b88dc56b8fb3b25929b5bc1fc5562a"
$env:TASTYTRADE_REFRESH_TOKEN="eyJhbGciOiJFZERTQSIsInR5cCI6InJ0K2p3dCIsImtpZCI6InJfQWVKWW0zMmJxeWJKdzBQT0xKZlg4V3U4b19lc09vZlJvWXhodC1COUkiLCJqa3UiOiJodHRwOi8vc2FuZGJveC1uZ2lueC5zZXJ2aWNlLmNvbnN1bDo4MDgwL29hdXRoL2p3a3MifQ.eyJpc3MiOiJodHRwczovL2FwaS5zYW5kYm94LnRhc3R5d29ya3MuY29tIiwic3ViIjoiVTdkZmM0OTM3LWU0MjAtNGE2MS1hYTM4LTFjMDk1OGVhY2QyOCIsImlhdCI6MTc5MTY4NTE0MywiYXVkIjoiMDRkYjkyY2MtYzc0ZS00NTA3LWI0NTUtMDEzZjk1YzUwOTViIiwiZ3JhbnRfaWQiOiJHMGRhOGM2MmMtMjNkNy00ZjQ0LTg3NDMtNjdjOGVmZTE5YThmIiwic2NvcGUiOiJyZWFkIHRyYWRlIG9wZW5pZCJ9.iOFx0EPWI_U73VYskTSUl0b6JG6twMEx3CJiGD5CKKFkkhIcYgpYDdWRGBvLaRVpt9dtin0OpdegpsI4lq5JDg"
$env:TASTYTRADE_CLIENT_ID="04db92cc-c74e-4507-b455-013f95c5095b"
python -m streamlit run tastytraderbot.py
```

### macOS/Linux
```bash
export TASTYTRADE_CLIENT_ID="YOUR_SANDBOX_CLIENT_ID"
export TASTYTRADE_CLIENT_SECRET="YOUR_SANDBOX_CLIENT_SECRET"
export TASTYTRADE_REFRESH_TOKEN="YOUR_SANDBOX_REFRESH_TOKEN"
python -m streamlit run tastytraderbot.py
```

## Production credentials
Production uses a separate OAuth application/grant. Set these environment variables only on your own computer:
- `TASTYTRADE_PROD_CLIENT_ID` (optional)
- `TASTYTRADE_PROD_CLIENT_SECRET`
- `TASTYTRADE_PROD_REFRESH_TOKEN`

Select **Production (real money)** in the sidebar and reconnect. Verify the account number carefully. The app requires the exact text `SUBMIT LIVE ORDER` and a review checkbox after the dry-run. Never share tokens in chat or commit them to source control. Ensure the grant has trading permission and account 2FA is enabled where required.

## Historical data
Choose symbols in the sidebar, then in **Historical data & backtest** select a period and interval and click **Download prices from Yahoo Finance**. Yahoo Finance limits intraday data availability; if the download fails, try a shorter period or larger interval. Daily-bar backtests do not test the intraday 6:30–11:30 AM entry window.

You can also upload a CSV with a `timestamp`, `date`, `datetime`, or `time` column and one column per ticker.

## Test
```bash
python -m unittest -v test_tastytraderbot.py
```

## Official references
- tastytrade equity order guide: https://developer.tastytrade.com/docs/guides/place-an-equity-order/
- tastytrade OAuth2: https://developer.tastytrade.com/oauth/
- tastytrade sandbox: https://developer.tastytrade.com/docs/sandbox/
- yfinance: https://github.com/ranaroussi/yfinance
