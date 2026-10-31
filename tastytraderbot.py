"""TastyTraderBot v4: yfinance history + tastytrade API + guarded manual equity orders.

Simulation remains the default. Live production order submission is manual, limit-only,
requires dry-run and explicit confirmation, and is not an autonomous trading engine.
"""
from __future__ import annotations
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from tastytrade_client_orders import TastytradeClient, TastytradeAPIError, SANDBOX_URL, PRODUCTION_URL
from strategy import run_csv_backtest, crossover_signals
from market_data import fetch_yfinance_history, fetch_yfinance_ohlc

PT = ZoneInfo("America/Los_Angeles")


def entry_window_open(dt=None):
    dt = (dt or datetime.now(PT)).astimezone(PT)
    return dt.weekday() < 5 and time(6, 30) <= dt.time().replace(tzinfo=None) < time(11, 30)


def get_secret(name, default=""):
    try:
        value = st.secrets.get(name, default)
        if value:
            return str(value)
    except Exception:
        pass
    return os.getenv(name, default)


def extract_order_data(response):
    if not isinstance(response, dict):
        return {}
    data = response.get("data", response)
    if isinstance(data, dict):
        return data.get("order", data)
    return {}


def preflight_errors(response):
    if not isinstance(response, dict):
        return []
    data = response.get("data", response)
    if isinstance(data, dict):
        errors = data.get("errors", [])
        if isinstance(errors, list):
            return errors
        if errors:
            return [errors]
    return []


st.set_page_config(page_title="TastyTraderBot v4", page_icon="📈", layout="wide")
st.title("📈 TastyTraderBot v4")
st.caption("Historical data via Yahoo Finance • Moving-average crossover • tastytrade API")
st.warning("Simulation is the default. Real-money orders are manual, limit-only, and require a dry-run plus explicit confirmation. No automatic strategy-to-live-order execution is enabled.")

with st.sidebar:
    st.header("Broker connection")
    environment = st.selectbox("API environment", ["Sandbox (recommended)", "Production (real money)"], index=0)
    is_production_selected = environment.startswith("Production")
    base_url = PRODUCTION_URL if is_production_selected else SANDBOX_URL
    if is_production_selected:
        client_id = get_secret("TASTYTRADE_PROD_CLIENT_ID")
        client_secret = get_secret("TASTYTRADE_PROD_CLIENT_SECRET")
        refresh_token = get_secret("TASTYTRADE_PROD_REFRESH_TOKEN")
        st.warning("PRODUCTION MODE: orders can use real money. Production OAuth credentials are required.")
    else:
        client_id = get_secret("TASTYTRADE_CLIENT_ID")
        client_secret = get_secret("TASTYTRADE_CLIENT_SECRET")
        refresh_token = get_secret("TASTYTRADE_REFRESH_TOKEN")
        st.info("Sandbox credentials are separate from production credentials.")
    st.caption("Credentials are read from environment variables or Streamlit secrets; never paste them into chat.")
    connect = st.button("Connect / refresh account data", type="primary")
    st.divider()
    st.header("Strategy settings")
    symbols_text = st.text_input("Tickers (comma-separated)", "AAPL, MSFT, NVDA, AMD, TSLA")
    amp = st.number_input("Halftrend Amplitude (bars)", 2, 10, 2)
    chdev = st.number_input("Halftrend ATR channel deviation", 2.0, 10.0, 2.0)
    stop_pct = st.slider("Backtest stop loss (%)", 0.0001, .010, 0.0001, step=.0001, format="%0.5f")
    starting_cash = st.number_input("Simulation starting capital ($)", 100.0, 10_000_000.0, 500.0, 500.0)
    st.caption("New strategy entries are restricted to weekdays 6:30–11:30 AM Pacific for intraday history.")

now = datetime.now(PT)
a, b, c = st.columns(3)
a.metric("Pacific time", now.strftime("%a %b %d · %I:%M:%S %p"))
b.metric("New-entry window", "OPEN" if entry_window_open(now) else "CLOSED")
c.metric("API environment", "PRODUCTION" if is_production_selected else "SANDBOX")
account_tab, strategy_tab, orders_tab, setup_tab = st.tabs(["Broker account", "Historical data & backtest", "Order preview / submit", "Setup & safeguards"])

for key, default in {
    "tt_client": None, "tt_environment": None, "tt_accounts": None,
    "tt_selected_account": "", "tt_balances": None, "tt_positions": None,
    "tt_orders": None, "tt_error": "", "tt_prices": None,
    "tt_prices_source": "", "tt_backtest": None, "tt_pending_order": None, "tt_ohlc": {},
    "tt_pending_preview": None, "tt_pending_environment": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

if connect:
    if not (client_secret and refresh_token):
        prefix = "TASTYTRADE_PROD_" if is_production_selected else "TASTYTRADE_"
        st.session_state.tt_error = f"Missing credentials. Set {prefix}CLIENT_SECRET and {prefix}REFRESH_TOKEN" + (" (and optionally CLIENT_ID)." if is_production_selected else " (and optionally CLIENT_ID).")
        st.session_state.tt_client = None
    else:
        try:
            client = TastytradeClient(client_secret=client_secret, refresh_token=refresh_token,
                                     client_id=client_id, base_url=base_url)
            accounts = client.get_accounts()
            st.session_state.tt_client = client
            st.session_state.tt_environment = environment
            st.session_state.tt_accounts = accounts
            st.session_state.tt_error = ""
            st.session_state.tt_balances = st.session_state.tt_positions = st.session_state.tt_orders = None
            st.session_state.tt_pending_order = st.session_state.tt_pending_preview = None
            if accounts:
                first = accounts[0].get("account", accounts[0])
                st.session_state.tt_selected_account = str(first.get("account-number", first.get("account_number", "")))
        except Exception as exc:
            st.session_state.tt_error = str(exc)
            st.session_state.tt_client = None
if st.session_state.tt_error:
    st.error(st.session_state.tt_error)

with account_tab:
    st.subheader("Broker connection and portfolio")
    client = st.session_state.tt_client
    if client and st.session_state.tt_accounts is not None:
        rows = []
        for item in st.session_state.tt_accounts:
            account = item.get("account", item)
            rows.append({"Account": account.get("account-number", account.get("account_number", "")),
                         "Nickname": account.get("nickname", ""),
                         "Type": account.get("account-type-name", account.get("account-type", "")),
                         "Authority": item.get("authority-level", item.get("authority_level", "")),
                         "Status": account.get("closed-date", "Open") or "Open"})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        numbers = [r["Account"] for r in rows if r["Account"]]
        if numbers:
            selected = st.selectbox("Account to inspect / trade", numbers,
                                    index=numbers.index(st.session_state.tt_selected_account)
                                    if st.session_state.tt_selected_account in numbers else 0)
            st.session_state.tt_selected_account = selected
            if st.button("Refresh balances, positions & orders"):
                try:
                    st.session_state.tt_balances = client.get_balances(selected)
                    st.session_state.tt_positions = client.get_positions(selected)
                    st.session_state.tt_orders = client.get_orders(selected)
                except Exception as exc:
                    st.error(str(exc))
        if st.session_state.tt_balances is not None:
            st.markdown("**Balances**")
            st.json(st.session_state.tt_balances)
        if st.session_state.tt_positions is not None:
            st.markdown("**Open broker positions**")
            st.json(st.session_state.tt_positions)
        if st.session_state.tt_orders is not None:
            st.markdown("**Broker order history / status**")
            st.json(st.session_state.tt_orders)
    else:
        st.info("Choose sandbox, set the matching OAuth credentials, and click Connect. Production requires separate production credentials.")

with strategy_tab:
    st.subheader("Get historical prices automatically")
    st.write("Download adjusted historical closing prices from Yahoo Finance, or upload your own CSV. Yahoo Finance is a third-party data source; check data quality and licensing before relying on it.")
    col1, col2, col3 = st.columns(3)
    period = col1.selectbox("History period", ["7d", "30d", "60d", "1y", "2y", "5y"], index=2)
    interval = col2.selectbox("Bar interval", ["1m", "5m", "15m", "30m", "60m", "1d", "1wk"], index=1)
    col3.caption("Yahoo Finance limits intraday history. If no data is returned, choose a shorter period or a larger interval.")
    if st.button("Download prices from Yahoo Finance", type="primary"):
        try:
            symbols = [s.strip().upper() for s in symbols_text.split(",") if s.strip()]
            if interval == "1m" and period != "7d":
                raise ValueError("Yahoo Finance 1-minute history is limited to about 7 days; choose period 7d.")
            if interval in ("5m", "15m", "30m") and period not in ("7d", "30d", "60d"):
                raise ValueError("Yahoo Finance intraday history at this interval is limited to about 60 days; choose 7d, 30d, or 60d.")
            if interval == "60m" and period not in ("7d", "30d", "60d", "1y", "2y"):
                raise ValueError("Yahoo Finance hourly history is limited; choose a period up to 2y.")
            prices = fetch_yfinance_history(symbols, period=period, interval=interval)
            st.session_state.tt_prices = prices
            st.session_state.tt_ohlc = fetch_yfinance_ohlc(symbols, period=period, interval=interval)
            st.session_state.tt_prices_source = f"Yahoo Finance · {period} · {interval}"
            st.session_state.tt_backtest = None
            st.success(f"Downloaded {len(prices):,} rows for {len(prices.columns)} ticker(s).")
        except Exception as exc:
            st.error(f"Historical download failed: {exc}")
    uploaded = st.file_uploader("Or upload historical prices CSV", type=["csv"])
    if uploaded is not None:
        try:
            csv_data = pd.read_csv(uploaded)
            timestamp_col = next((c for c in csv_data.columns if c.lower() in ("timestamp", "datetime", "date", "time")), None)
            if not timestamp_col:
                st.error("CSV needs a timestamp/date/datetime column plus one column per ticker.")
            else:
                csv_data[timestamp_col] = pd.to_datetime(csv_data[timestamp_col], errors="coerce")
                csv_data = csv_data.dropna(subset=[timestamp_col]).set_index(timestamp_col)
                csv_data = csv_data.apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all")
                csv_data.columns = [str(c).upper() for c in csv_data.columns]
                st.session_state.tt_prices = csv_data.sort_index()
                st.session_state.tt_prices_source = "Uploaded CSV"
        except Exception as exc:
            st.error(f"Could not read CSV: {exc}")
    prices = st.session_state.tt_prices
    if prices is not None and not prices.empty:
        st.caption(f"Source: {st.session_state.tt_prices_source} · {len(prices):,} bars · {', '.join(prices.columns)}")
        st.dataframe(prices.tail(10), use_container_width=True)
        # Daily price histories are commonly timestamped at midnight. They can be backtested,
        # but do not simulate the intraday entry window.
        daily_bars = all(pd.Timestamp(ts).hour == 0 and pd.Timestamp(ts).minute == 0 for ts in prices.index)
        st.caption("Daily bars are useful for swing-strategy backtests but do not test the 6:30–11:30 AM intraday entry window." if daily_bars else "Intraday backtest entry signals are restricted to weekdays 6:30–11:30 AM Pacific.")
        if st.button("Run backtest using loaded prices", type="primary"):
            try:
                eq, trades, open_positions = run_csv_backtest(
                    prices, float(starting_cash), int(amp), int(chdev), float(stop_pct),
                    max_allocation_pct=100.0, daily_bars=daily_bars
                )
                st.session_state.tt_backtest = (eq, trades, open_positions)
            except Exception as exc:
                st.error(f"Backtest failed: {exc}")
    else:
        st.info("No prices loaded yet. Download from Yahoo Finance or upload a CSV to begin.")
    if st.session_state.tt_backtest:
        eq, trades, open_positions = st.session_state.tt_backtest
        ending = float(eq["Equity"].iloc[-1]) if len(eq) else float(starting_cash)
        pnl = ending - float(starting_cash)
        drawdown = (eq["Equity"] - eq["Equity"].cummax()) / eq["Equity"].cummax() * 100
        m1, m2, m3 = st.columns(3)
        m1.metric("Ending equity", f"${ending:,.2f}")
        m2.metric("Net P&L", f"${pnl:,.2f}", f"{pnl / starting_cash * 100:.2f}%")
        m3.metric("Closed trades", str(len(trades)))
        st.plotly_chart(px.line(eq, x="Time", y="Equity", title="Backtest equity curve"), use_container_width=True)

        ohlc_data = st.session_state.get("tt_ohlc", {})
        if ohlc_data:
            st.subheader("HalfTrend indicator — Yahoo Finance OHLC")
            ht_symbol = st.selectbox("Ticker for HalfTrend plot", list(ohlc_data), key="halftrend_plot_ticker")
            ht_amplitude = st.slider("HalfTrend amplitude", 1, 10, 2, key="halftrend_amplitude")
            ht_deviation = st.slider("ATR channel deviation", 0.5, 4.0, 2.0, 0.5, key="halftrend_deviation")
            ht_candles = ohlc_data[ht_symbol].copy()
            ht = crossover_signals(ht_candles, amplitude=ht_amplitude, channel_deviation=ht_deviation)
            fig_ht = go.Figure()
            fig_ht.add_trace(go.Candlestick(x=ht.index, open=ht_candles["Open"], high=ht_candles["High"],
                                            low=ht_candles["Low"], close=ht_candles["Close"], name=ht_symbol))
            fig_ht.add_trace(go.Scatter(x=ht.index, y=ht["halftrend"], mode="lines", name="HalfTrend", line=dict(width=2)))
            buys, sells = ht["buy_signal"], ht["sell_signal"]
            fig_ht.add_trace(go.Scatter(x=ht.index[buys], y=ht_candles.loc[buys, "Low"], mode="markers",
                                        name="BUY reversal", marker=dict(symbol="triangle-up", size=12, color="green")))
            fig_ht.add_trace(go.Scatter(x=ht.index[sells], y=ht_candles.loc[sells, "High"], mode="markers",
                                        name="SELL reversal", marker=dict(symbol="triangle-down", size=12, color="red")))
            fig_ht.add_trace(go.Scatter(x=ht.index, y=ht["atr_high"], mode="lines", name="ATR channel high",
                                        line=dict(width=1, dash="dot")))
            fig_ht.add_trace(go.Scatter(x=ht.index, y=ht["atr_low"], mode="lines", name="ATR channel low",
                                        line=dict(width=1, dash="dot"), fill="tonexty", fillcolor="rgba(100,100,100,0.08)"))
            fig_ht.update_layout(title=f"{ht_symbol} HalfTrend reversals · Yahoo Finance",
                                 xaxis_title="Time", yaxis_title="Adjusted price",
                                 xaxis_rangeslider_visible=False, height=650, legend_orientation="h")
            st.plotly_chart(fig_ht, use_container_width=True)
            st.caption("Signals are marked on the candle where the HalfTrend state changes. Use completed candles only; Yahoo Finance intraday history can be delayed or incomplete.")
            st.dataframe(ht[["close", "halftrend", "atr_high", "atr_low", "trend", "signal"]].tail(20),
                         use_container_width=True)
        else:
            st.info("Download Yahoo Finance data to view HalfTrend. Uploaded close-only CSVs do not contain true OHLC candles.")

        st.plotly_chart(px.area(pd.DataFrame({"Time": eq["Time"], "Drawdown %": drawdown}),
                                x="Time", y="Drawdown %", title="Peak-to-trough drawdown"), use_container_width=True)
        st.subheader("Closed trade history")
        st.dataframe(trades.iloc[::-1], use_container_width=True, hide_index=True)
        st.subheader("Open positions at end of test")
        st.dataframe(open_positions, use_container_width=True, hide_index=True)
        st.download_button("Download backtest trade history", trades.to_csv(index=False).encode(),
                           "tastytraderbot_trades.csv", "text/csv")

with orders_tab:
    st.subheader("Equity limit-order preview and submission")
    if is_production_selected:
        st.error("PRODUCTION: submitting an order sends it to tastytrade and may use real money.")
    else:
        st.info("Sandbox orders are simulated/fake-money orders. Use this environment to test the entire order workflow first.")
    st.markdown("Only single-leg equity **limit orders** are supported here. This panel submits orders manually; it does not automatically convert crossover signals into trades, and it does not place protective stop-loss orders.")
    client = st.session_state.tt_client
    connected_to_selected = bool(client and st.session_state.tt_environment == environment and client.base_url == base_url)
    if client and not connected_to_selected:
        st.warning("The selected API environment differs from your connected client. Click Connect / refresh account data in the sidebar before preparing orders.")
    if connected_to_selected and st.session_state.tt_selected_account:
        max_notional = st.number_input("Maximum order notional cap ($)", min_value=100.0, max_value=10000.0, value=1000.0, step=100.0)
        with st.form("prepare_order_form"):
            ticker = st.text_input("Ticker", "AAPL").strip().upper()
            side = st.selectbox("Action", ["Buy to Open", "Sell to Close", "Sell to Open", "Buy to Close"])
            quantity = st.number_input("Whole shares", min_value=1, max_value=100000, value=1, step=1)
            limit_price = st.number_input("Limit price per share ($)", min_value=0.01, max_value=100000.0, value=100.00, step=0.01)
            prepare = st.form_submit_button("Run tastytrade dry-run preview")
        if prepare:
            try:
                if side in ("Buy to Open", "Sell to Open") and not entry_window_open():
                    raise ValueError("New positions may be opened only on weekdays from 6:30 AM to 11:30 AM Pacific.")
                notional = int(quantity) * float(limit_price)
                if notional > max_notional:
                    raise ValueError(f"Order notional ${notional:,.2f} exceeds the configured cap ${max_notional:,.2f}.")
                order_payload = client.build_equity_order(ticker, side, int(quantity), float(limit_price))
                preview = client.dry_run_order(st.session_state.tt_selected_account, order_payload)
                st.session_state.tt_pending_order = order_payload
                st.session_state.tt_pending_preview = preview
                st.session_state.tt_pending_environment = environment
            except Exception as exc:
                st.error(f"Could not prepare order: {exc}")
        pending = st.session_state.tt_pending_order
        preview = st.session_state.tt_pending_preview
        if pending and preview and st.session_state.tt_pending_environment == environment:
            st.markdown("#### Dry-run result — no order has been submitted")
            st.json(preview)
            errs = preflight_errors(preview)
            if errs:
                st.error("The broker preview returned errors. This order cannot be submitted from this preview.")
            else:
                leg = pending["legs"][0]
                notional = int(leg["quantity"]) * float(pending["price"])
                st.write(f"**Proposed:** {leg['action']} {leg['quantity']} {leg['symbol']} at a ${float(pending['price']):,.2f} limit · maximum gross notional ${notional:,.2f} · Day order")
                st.warning("Review the broker's buying-power effect, fees, and every warning above. A dry-run does not guarantee a fill or protect against later price movement.")
                with st.form("confirm_order_form"):
                    reviewed = st.checkbox("I reviewed the dry-run result, order details, fees, and warnings.")
                    expected_phrase = "SUBMIT LIVE ORDER" if is_production_selected else "SUBMIT SANDBOX ORDER"
                    typed_phrase = st.text_input(f"Type exactly: {expected_phrase}")
                    submit = st.form_submit_button("Submit order to tastytrade")
                if submit:
                    try:
                        if not reviewed or typed_phrase != expected_phrase:
                            raise PermissionError("Confirmation checkbox and exact confirmation phrase are required.")
                        if st.session_state.tt_pending_environment != environment:
                            raise PermissionError("Environment changed since preview. Prepare the order again.")
                        if is_production_selected and not client.is_production:
                            raise PermissionError("Production submission blocked: connected client is not production.")
                        if not is_production_selected and client.is_production:
                            raise PermissionError("Environment mismatch: refusing sandbox-labelled submission through production API.")
                        if leg["action"] in ("Buy to Open", "Sell to Open") and not entry_window_open():
                            raise PermissionError("New positions may be opened only weekdays 6:30–11:30 AM Pacific.")
                        result = client.submit_order(st.session_state.tt_selected_account, pending,
                                                     confirm_live=True, max_order_notional=max_notional)
                        st.success("Order submission response received. Verify its status in the Broker account tab; submission does not guarantee a fill.")
                        st.json(result)
                        st.session_state.tt_pending_order = None
                        st.session_state.tt_pending_preview = None
                    except Exception as exc:
                        st.error(f"Order was not confirmed as submitted: {exc}")
                        st.warning("If the request timed out or returned a server error, refresh broker orders and search for the external identifier before attempting anything again. Do not blindly resubmit.")
    else:
        st.info("Connect to the currently selected environment and select an account first.")

with setup_tab:
    st.subheader("OAuth credentials")
    st.markdown("Create OAuth applications/grants in tastytrade under **Manage → My Profile → API → OAuth Applications**. Sandbox and production credentials are separate.")
    st.markdown("""
**Sandbox environment variables**
- `TASTYTRADE_CLIENT_ID` (optional)
- `TASTYTRADE_CLIENT_SECRET`
- `TASTYTRADE_REFRESH_TOKEN`

**Production environment variables**
- `TASTYTRADE_PROD_CLIENT_ID` (optional)
- `TASTYTRADE_PROD_CLIENT_SECRET`
- `TASTYTRADE_PROD_REFRESH_TOKEN`

Keep these private. Never paste them into chat or commit them to source control. The production grant must have trading permissions and tastytrade account 2FA enabled where required.
""")
    st.subheader("Safeguards and limitations")
    st.markdown("""
- Simulation remains separate from broker orders. No unattended/automatic live execution is enabled.
- Manual single-leg equity Day limit orders support Buy to Open / Sell to Close for long positions and Sell to Open / Buy to Close for short positions; no market orders, options, automated stops, cancels, or replacements.
- Production order submission requires a dry-run preview, review checkbox, exact typed confirmation, account selection, and a configurable notional cap (maximum setting $10,000).
- Orders that open a position (Buy to Open or Sell to Open) are blocked outside weekdays 6:30–11:30 AM Pacific. Closing orders are permitted outside that window for manual position management.
- The app checks existing order history for a matching external identifier and never automatically retries a submission. After a timeout/5xx, inspect broker order history before retrying.
- Yahoo Finance is not tastytrade market data, may be delayed/incomplete, and its intraday history is restricted. It is used for historical analysis only, not as an execution quote.
- The configured stop loss is backtest-only. This build does not automatically monitor live positions or place broker-side protective orders.
- Backtest results ignore some real-world effects (spread, taxes, liquidity, partial fills, fees unless configured) and do not predict future results.
""")
    st.markdown("Official docs: [tastytrade equity order guide](https://developer.tastytrade.com/docs/guides/place-an-equity-order/) · [OAuth2](https://developer.tastytrade.com/oauth/) · [Sandbox](https://developer.tastytrade.com/docs/sandbox/) · [yfinance project](https://github.com/ranaroussi/yfinance)")

st.divider()
st.caption("TastyTraderBot v4 • Historical simulation + manually confirmed tastytrade equity limit orders • Not investment advice")
