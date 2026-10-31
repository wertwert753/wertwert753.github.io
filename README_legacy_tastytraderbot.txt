# TastyTraderBot v3 — tastytrade Open API

## Safety model
- Default to tastytrade certification sandbox.
- Reads accounts, balances, positions, and orders.
- Allows an equity order **dry-run only**.
- This code intentionally contains no production order-submission method.
- Strategy backtesting requires a CSV of timestamped historical close prices.
- The sandbox does not provide quote/market-data endpoints; this app does not pretend otherwise.

## Install
Python 3.10+ recommended.

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
pip install -r requirements_tastytraderbot.txt
streamlit run tastytraderbot.py
```

## OAuth credentials
Create a separate tastytrade sandbox OAuth client and grant. Do not reuse production credentials in sandbox.

### Windows PowerShell
```powershell
$env:TASTYTRADE_CLIENT_SECRET="a7c8209f93b88dc56b8fb3b25929b5bc1fc5562a"
$env:TASTYTRADE_REFRESH_TOKEN="eyJhbGciOiJFZERTQSIsInR5cCI6InJ0K2p3dCIsImtpZCI6InJfQWVKWW0zMmJxeWJKdzBQT0xKZlg4V3U4b19lc09vZlJvWXhodC1COUkiLCJqa3UiOiJodHRwOi8vc2FuZGJveC1uZ2lueC5zZXJ2aWNlLmNvbnN1bDo4MDgwL29hdXRoL2p3a3MifQ.eyJpc3MiOiJodHRwczovL2FwaS5zYW5kYm94LnRhc3R5d29ya3MuY29tIiwic3ViIjoiVTdkZmM0OTM3LWU0MjAtNGE2MS1hYTM4LTFjMDk1OGVhY2QyOCIsImlhdCI6MTc5MTY4NTE0MywiYXVkIjoiMDRkYjkyY2MtYzc0ZS00NTA3LWI0NTUtMDEzZjk1YzUwOTViIiwiZ3JhbnRfaWQiOiJHMGRhOGM2MmMtMjNkNy00ZjQ0LTg3NDMtNjdjOGVmZTE5YThmIiwic2NvcGUiOiJyZWFkIHRyYWRlIG9wZW5pZCJ9.iOFx0EPWI_U73VYskTSUl0b6JG6twMEx3CJiGD5CKKFkkhIcYgpYDdWRGBvLaRVpt9dtin0OpdegpsI4lq5JDg"
$env:TASTYTRADE_CLIENT_ID="04db92cc-c74e-4507-b455-013f95c5095b"
streamlit run tastytraderbot.py
```

### macOS/Linux
```bash
export TASTYTRADE_CLIENT_SECRET="YOUR_SANDBOX_CLIENT_SECRET"
export TASTYTRADE_REFRESH_TOKEN="YOUR_SANDBOX_REFRESH_TOKEN"
export TASTYTRADE_CLIENT_ID="YOUR_SANDBOX_CLIENT_ID"
streamlit run tastytraderbot.py
```

Or use `.streamlit/secrets.toml` (keep it out of source control):
```toml
TASTYTRADE_CLIENT_SECRET = "..."
TASTYTRADE_REFRESH_TOKEN = "..."
TASTYTRADE_CLIENT_ID = "..."
```

Create credentials from tastytrade: Manage → My Profile → API → OAuth Applications. Use a `read` scope for account data; the dry-run endpoint may require the appropriate trading scope depending on API authorization. For sensitive scopes, tastytrade requires 2FA.

## Strategy CSV format
CSV example:
```csv
timestamp,AAPL,MSFT
2026-10-12 06:30:00,227.10,510.20
2026-10-12 06:31:00,227.25,510.00
```
Timestamps without a timezone are treated as Pacific local time. Use trusted, properly licensed historical data. This backtest is not a guarantee of future performance.

## Test
```bash
python -m unittest -v test_tastytraderbot.py
```

## Before any future live trading
A separate reviewed implementation must add: market-data provider integration, broker-side protective orders, order/fill reconciliation, duplicate-order checks after uncertain submissions, persistent audit logs, daily loss/exposure caps, stale-data kill switch, session recovery, and explicit live confirmation. Test the complete workflow in the certification sandbox first.
