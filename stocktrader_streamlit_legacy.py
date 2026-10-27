from __future__ import annotations

import math
import os
import random
import re
import sqlite3
import time
import uuid
from datetime import datetime, time as wall_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from market_data import fetch_most_active_symbols, fetch_yfinance_delayed_closes, fetch_yfinance_ohlc
from observed_universe import save_observed_symbols
from sentiment_store import load_sentiment_snapshot, sentiment_priority
from strategy import crossover_signals
from tastytrade_client_v4 import (
    PRODUCTION_URL,
    SANDBOX_URL,
    TastytradeAPIError,
    TastytradeClient,
)

APP_VERSION = "1.0.0"
PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "trading_dashboard.sqlite3"
TERMINAL_ORDER_STATES = {"filled", "cancelled", "canceled", "rejected", "expired"}
INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}

st.set_page_config(page_title="StockTrader", page_icon="📈", layout="wide")
st.title("StockTrader")
st.caption("Yahoo Finance candles · HalfTrend signals · portfolio dashboard")


def secret(name: str) -> str:
    try:
        value = st.secrets.get(name, "")
        if value:
            return str(value)
    except Exception:
        pass
    return os.getenv(name, "")


def payload_data(response):
    return response.get("data", response) if isinstance(response, dict) else response


def items(response, keys=()):
    data = payload_data(response)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
        if isinstance(data.get("items"), list):
            return data["items"]
    return []


def account_record(item):
    return item.get("account", item) if isinstance(item, dict) else {}


def numeric(record, *keys, default=0.0):
    for key in keys:
        value = record.get(key)
        try:
            if value not in (None, ""):
                return float(value)
        except (TypeError, ValueError):
            continue
    return default


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS baseline (
            account_key TEXT PRIMARY KEY, nlv REAL NOT NULL, started_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS equity (
            id INTEGER PRIMARY KEY, account_key TEXT NOT NULL, timestamp TEXT NOT NULL,
            net_liquidating_value REAL NOT NULL, cash REAL NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS signal_events (
            account_key TEXT NOT NULL, symbol TEXT NOT NULL, signal_time TEXT NOT NULL,
            side TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL,
            external_id TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(account_key, symbol, signal_time, side))""")
        db.execute("""CREATE TABLE IF NOT EXISTS sandbox_sim_positions (
            account_key TEXT NOT NULL, symbol TEXT NOT NULL, quantity REAL NOT NULL,
            average_price REAL NOT NULL, last_price REAL NOT NULL, opened_at TEXT NOT NULL,
            PRIMARY KEY(account_key, symbol))""")
        db.execute("""CREATE TABLE IF NOT EXISTS sandbox_sim_trades (
            id INTEGER PRIMARY KEY, account_key TEXT NOT NULL, trading_day TEXT NOT NULL,
            symbol TEXT NOT NULL, signal_time TEXT NOT NULL, signal TEXT NOT NULL,
            action TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL,
            realized_pnl REAL NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(account_key, symbol, signal_time, signal))""")
        db.execute("""CREATE TABLE IF NOT EXISTS sandbox_sim_equity (
            id INTEGER PRIMARY KEY, account_key TEXT NOT NULL, timestamp TEXT NOT NULL,
            equity REAL NOT NULL)""")


def persist_equity(account_key, nlv, cash):
    now = datetime.now(PT).isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as db:
        db.execute("INSERT INTO equity(account_key,timestamp,net_liquidating_value,cash) VALUES(?,?,?,?)",
                   (account_key, now, float(nlv), float(cash)))
        row = db.execute("SELECT nlv FROM baseline WHERE account_key=?", (account_key,)).fetchone()
        if row is None:
            db.execute("INSERT INTO baseline(account_key,nlv,started_at) VALUES(?,?,?)",
                       (account_key, float(nlv), now))
            return float(nlv)
        return float(row[0])


def update_event(account_key, symbol, signal_time, side, status, details, external_id):
    now = datetime.now(PT).isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""INSERT INTO signal_events
            (account_key,symbol,signal_time,side,status,details,external_id,created_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(account_key,symbol,signal_time,side) DO UPDATE SET
            status=excluded.status, details=excluded.details, external_id=excluded.external_id""",
                   (account_key, symbol, signal_time, side, status, str(details), external_id, now))


def has_event(account_key, symbol, signal_time, side):
    with sqlite3.connect(DB_PATH) as db:
        return db.execute("SELECT 1 FROM signal_events WHERE account_key=? AND symbol=? AND signal_time=? AND side=?",
                          (account_key, symbol, signal_time, side)).fetchone() is not None


def get_event(account_key, symbol, signal_time, side):
    with sqlite3.connect(DB_PATH) as db:
        return db.execute("SELECT status, external_id FROM signal_events WHERE account_key=? AND symbol=? AND signal_time=? AND side=?",
                          (account_key, symbol, signal_time, side)).fetchone()


def sandbox_sim_snapshot(account_key, initial_capital):
    """Return persistent virtual positions and realized P&L; independent of broker equity."""
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute("SELECT symbol,quantity,average_price,last_price,opened_at FROM sandbox_sim_positions WHERE account_key=?",
                          (account_key,)).fetchall()
        realized = db.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM sandbox_sim_trades WHERE account_key=?",
                              (account_key,)).fetchone()[0]
    positions = {symbol: {"quantity": float(qty), "average_price": float(avg),
                          "last_price": float(last), "opened_at": opened_at}
                 for symbol, qty, avg, last, opened_at in rows}
    unrealized = sum(p["quantity"] * (p["last_price"] - p["average_price"]) for p in positions.values())
    return positions, float(realized), float(unrealized), float(initial_capital) + float(realized) + unrealized


def persist_sandbox_sim_equity(account_key, equity):
    timestamp = datetime.now(PT).isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as db:
        db.execute("INSERT INTO sandbox_sim_equity(account_key,timestamp,equity) VALUES(?,?,?)",
                   (account_key, timestamp, float(equity)))


def sandbox_sim_equity_history(account_key):
    with sqlite3.connect(DB_PATH) as db:
        return pd.read_sql_query(
            "SELECT timestamp,equity FROM sandbox_sim_equity WHERE account_key=? ORDER BY id",
            db, params=(account_key,))


def sandbox_simulator_key():
    """Reuse the existing sandbox simulator ledger, then keep it account-independent."""
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            "SELECT account_key FROM sandbox_sim_trades WHERE account_key LIKE 'Sandbox:%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return row[0]
        row = db.execute(
            "SELECT account_key FROM sandbox_sim_positions WHERE account_key LIKE 'Sandbox:%' LIMIT 1"
        ).fetchone()
    return row[0] if row else "Sandbox:simulator"


def mark_sandbox_sim_positions(account_key, price_map):
    with sqlite3.connect(DB_PATH) as db:
        for symbol, price in price_map.items():
            if price is None or not math.isfinite(float(price)) or float(price) <= 0:
                continue
            db.execute("UPDATE sandbox_sim_positions SET last_price=? WHERE account_key=? AND symbol=?",
                       (float(price), account_key, symbol))


def apply_sandbox_sim_signal(account_key, symbol, signal_time, signal, price,
                             initial_capital, allocation_pct, max_exposure_pct=100.0):
    """Book an independent virtual signal fill at the selected reference price."""
    if not price or not math.isfinite(float(price)) or float(price) <= 0:
        return False
    now = datetime.now(PT).isoformat(timespec="seconds")
    trading_day = datetime.now(PT).date().isoformat()
    with sqlite3.connect(DB_PATH) as db:
        if db.execute("SELECT 1 FROM sandbox_sim_trades WHERE account_key=? AND symbol=? AND signal_time=? AND signal=?",
                      (account_key, symbol, signal_time, signal)).fetchone():
            return False
        row = db.execute("SELECT quantity,average_price,last_price FROM sandbox_sim_positions WHERE account_key=? AND symbol=?",
                         (account_key, symbol)).fetchone()
        qty, avg, last = (float(row[0]), float(row[1]), float(row[2])) if row else (0.0, 0.0, 0.0)
        action, trade_qty, realized = "NO_TRADE", 0.0, 0.0
        if qty == 0:
            realized_total = db.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM sandbox_sim_trades WHERE account_key=?",
                                        (account_key,)).fetchone()[0]
            positions = db.execute("SELECT quantity,last_price FROM sandbox_sim_positions WHERE account_key=?",
                                   (account_key,)).fetchall()
            exposure = sum(abs(float(q)) * float(mark or price) for q, mark in positions)
            unrealized = sum(float(q) * (float(mark or price) - float(avg))
                             for q, avg, mark in db.execute(
                                 "SELECT quantity,average_price,last_price FROM sandbox_sim_positions WHERE account_key=?",
                                 (account_key,),
                             ).fetchall())
            equity = max(0.0, float(initial_capital) + float(realized_total) + unrealized)
            budget = min(equity * float(allocation_pct) / 100,
                         max(0.0, equity * float(max_exposure_pct) / 100 - exposure))
            trade_qty = round(budget / float(price), 6) if budget > 0 else 0.0
            if trade_qty > 0:
                qty = trade_qty if signal == "BUY" else -trade_qty
                avg = float(price)
                last = float(price)
                action = "SIM_OPEN_LONG" if qty > 0 else "SIM_OPEN_SHORT"
                db.execute("INSERT INTO sandbox_sim_positions(account_key,symbol,quantity,average_price,last_price,opened_at) VALUES(?,?,?,?,?,?)",
                           (account_key, symbol, qty, avg, last, now))
        elif (qty > 0 and signal == "SELL") or (qty < 0 and signal == "BUY"):
            trade_qty = abs(qty)
            realized = qty * (float(price) - avg)
            action = "SIM_CLOSE_LONG" if qty > 0 else "SIM_CLOSE_SHORT"
            db.execute("DELETE FROM sandbox_sim_positions WHERE account_key=? AND symbol=?", (account_key, symbol))
        else:
            trade_qty = abs(qty)
        db.execute("INSERT OR IGNORE INTO sandbox_sim_trades(account_key,trading_day,symbol,signal_time,signal,action,quantity,price,realized_pnl,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (account_key, trading_day, symbol, signal_time, signal, action, trade_qty,
                    float(price), float(realized), now))
    return action != "NO_TRADE"


def close_sandbox_sim_position(account_key, symbol, price, now_et):
    """Realize a virtual position at the pre-close reference price."""
    if not price or not math.isfinite(float(price)) or float(price) <= 0:
        return False
    signal = "EOD_EXIT"
    signal_time = now_et.isoformat()
    trading_day = now_et.date().isoformat()
    now = datetime.now(PT).isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            "SELECT quantity,average_price FROM sandbox_sim_positions WHERE account_key=? AND symbol=?",
            (account_key, symbol),
        ).fetchone()
        if not row:
            return False
        qty, average = float(row[0]), float(row[1])
        realized = qty * (float(price) - average)
        action = "SIM_EOD_CLOSE_LONG" if qty > 0 else "SIM_EOD_CLOSE_SHORT"
        db.execute("DELETE FROM sandbox_sim_positions WHERE account_key=? AND symbol=?",
                   (account_key, symbol))
        db.execute("INSERT OR IGNORE INTO sandbox_sim_trades(account_key,trading_day,symbol,signal_time,signal,action,quantity,price,realized_pnl,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (account_key, trading_day, symbol, signal_time, signal, action,
                    abs(qty), float(price), float(realized), now))
    return True


def sandbox_sim_daily_history(account_key):
    with sqlite3.connect(DB_PATH) as db:
        return pd.read_sql_query(
            "SELECT trading_day AS Day, ROUND(SUM(realized_pnl),2) AS Realized_PnL FROM sandbox_sim_trades WHERE account_key=? GROUP BY trading_day ORDER BY trading_day DESC LIMIT 60",
            db, params=(account_key,))


def sandbox_sim_trade_history(account_key):
    with sqlite3.connect(DB_PATH) as db:
        return pd.read_sql_query(
            "SELECT created_at AS Time, trading_day AS Day, symbol AS Symbol, action AS Action, quantity AS Quantity, price AS Price, realized_pnl AS Realized_PnL FROM sandbox_sim_trades WHERE account_key=? ORDER BY id DESC LIMIT 100",
            db, params=(account_key,))


def history_tables(account_key):
    with sqlite3.connect(DB_PATH) as db:
        equity = pd.read_sql_query(
            "SELECT timestamp,net_liquidating_value,cash FROM equity WHERE account_key=? ORDER BY id",
            db, params=(account_key,))
        events = pd.read_sql_query(
            "SELECT created_at,symbol,side,status,details FROM signal_events WHERE account_key=? ORDER BY created_at DESC LIMIT 100",
            db, params=(account_key,))
    return equity, events


def broker_quotes(client, symbols):
    if not symbols:
        return {}

    def status_code(error):
        match = re.search(r"\((\d{3})\)", str(error))
        return int(match.group(1)) if match else None

    def fetch_batch(batch):
        last_error = None
        for attempt in range(3):
            try:
                response = client._request(
                    "GET", "/market-data/by-type", params={"equity": ",".join(batch)}
                )
                return items(response, ("items",))
            except Exception as exc:
                last_error = exc
                code = status_code(exc)
                retryable = code is None or code == 429 or code >= 500
                if not retryable or attempt == 2:
                    break
                time.sleep((0.5 * (2 ** attempt)) + random.uniform(0, 0.25))
        raise last_error

    try:
        quotes = fetch_batch(symbols)
        return {str(q.get("symbol", "")).upper(): q for q in quotes if isinstance(q, dict)}
    except Exception as exc:
        code = status_code(exc)
        # Do not multiply requests after rate limiting or a client/authentication error.
        if code == 429 or (code is not None and code < 500):
            raise

    # A transient gateway failure can affect a large batch. Retry smaller GET batches;
    # missing symbols remain quote-less and therefore cannot trigger an order.
    recovered = {}
    last_error = None
    for start in range(0, len(symbols), 5):
        try:
            batch_quotes = fetch_batch(symbols[start:start + 5])
            recovered.update({str(q.get("symbol", "")).upper(): q
                              for q in batch_quotes if isinstance(q, dict)})
        except Exception as exc:
            last_error = exc
            if status_code(exc) == 429:
                break
    if recovered:
        return recovered
    raise TastytradeAPIError(f"Quote batch and smaller-batch retries failed: {last_error}")


def signed_quantity(position):
    quantity = numeric(position, "quantity")
    direction = str(position.get("quantity-direction", "Long")).lower()
    return -abs(quantity) if direction == "short" else abs(quantity)


def active_orders_by_symbol(orders):
    result = set()
    for item in orders:
        order = item.get("order", item) if isinstance(item, dict) else {}
        if str(order.get("status", "")).lower() in TERMINAL_ORDER_STATES:
            continue
        for leg in order.get("legs", []) or []:
            if leg.get("instrument-type", "Equity").lower() == "equity":
                result.add(str(leg.get("symbol", "")).upper())
    return result


def in_regular_session():
    now = datetime.now(ET)
    return now.weekday() < 5 and wall_time(9, 30) <= now.time().replace(tzinfo=None) < wall_time(16, 0)


def in_preclose_window(now_et=None):
    """The closeout window starts 15 minutes before the 1 p.m. Pacific close."""
    now = now_et or datetime.now(ET)
    local_time = now.time().replace(tzinfo=None)
    return now.weekday() < 5 and wall_time(15, 45) <= local_time < wall_time(16, 0)


def bar_freshness(frame, interval):
    if frame.empty:
        return None, float("inf")
    timestamp = pd.Timestamp(frame.index[-1])
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(ET)
    end = timestamp + pd.Timedelta(minutes=INTERVAL_MINUTES[interval])
    age = (pd.Timestamp.now(tz="UTC") - end.tz_convert("UTC")).total_seconds()
    return end, age


def fresh_quote(quote, max_age=90):
    if not quote:
        return False
    try:
        updated = pd.Timestamp(quote.get("updated-at"))
        if updated.tzinfo is None:
            updated = updated.tz_localize("UTC")
        age = (pd.Timestamp.now(tz="UTC") - updated.tz_convert("UTC")).total_seconds()
        return -5 <= age <= max_age
    except Exception:
        return False


def make_chart(frame, indicator, symbol, interval):
    plot_frame = frame.tail(300)
    plot_indicator = indicator.loc[plot_frame.index]
    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=plot_frame.index, open=plot_frame.Open, high=plot_frame.High,
                                 low=plot_frame.Low, close=plot_frame.Close, name=symbol))
    fig.add_trace(go.Scatter(x=plot_frame.index, y=plot_indicator.ht, name="HalfTrend",
                             line={"color": "royalblue", "width": 1.5}))
    buys = plot_indicator[plot_indicator.buy_signal]
    sells = plot_indicator[plot_indicator.sell_signal]
    fig.add_trace(go.Scatter(x=buys.index, y=plot_frame.loc[buys.index, "Low"], mode="markers",
                             name="Buy reversal", marker={"symbol": "triangle-up", "size": 11, "color": "green"}))
    fig.add_trace(go.Scatter(x=sells.index, y=plot_frame.loc[sells.index, "High"], mode="markers",
                             name="Sell reversal", marker={"symbol": "triangle-down", "size": 11, "color": "red"}))
    fig.update_layout(title=f"{symbol} · {interval} · last 300 candles", height=520,
                      xaxis_rangeslider_visible=False, margin={"l": 10, "r": 10, "t": 55, "b": 10})
    return fig


def render_equity_history(history, value_column, title, widget_key):
    """Render an equity curve with a user-selected first date."""
    history = history.copy()
    history["timestamp"] = pd.to_datetime(history.timestamp, utc=True).dt.tz_convert(PT)
    first_date = history.timestamp.min().date()
    last_date = history.timestamp.max().date()
    start_date = st.date_input(
        "Start date", value=first_date, min_value=first_date, max_value=last_date,
        key=widget_key,
    )
    visible = history[history.timestamp.dt.date >= start_date]
    fig = go.Figure(go.Scatter(
        x=visible.timestamp, y=visible[value_column], mode="lines",
        name="Equity"))
    fig.update_layout(title=title, height=300, yaxis_title="USD",
                      margin={"l": 10, "r": 10, "t": 45, "b": 10})
    st.plotly_chart(fig, use_container_width=True)


init_db()

with st.sidebar:
    st.header("Broker connection")
    environment = st.selectbox("Environment", ["Sandbox", "Production"], index=0)
    production = environment == "Production"
    sandbox_mode = not production
    delayed_sandbox_quotes = sandbox_mode
    sandbox_sim_starting_capital = 500.0
    sandbox_sim_allocation_pct = 20
    if not production:
        st.info("Sandbox mode runs a local virtual portfolio using Yahoo prices. Positions close at 12:45 p.m. Pacific each trading day. No tastytrade sandbox orders or account values are used.")
        st.subheader("Sandbox P&L simulation")
        sandbox_sim_starting_capital = st.number_input(
            "Virtual starting capital ($)", min_value=100.0, max_value=1000000.0,
            value=500.0, step=100.0,
        )
        sandbox_sim_allocation_pct = st.slider(
            "Virtual equity per simulated position (%)", min_value=1, max_value=100, value=100,
        )
    connect_clicked = False
    client_id = client_secret = refresh_token = ""
    if production:
        prefix = "TASTYTRADE_PROD_"
        client_id = secret(prefix + "CLIENT_ID")
        client_secret = secret(prefix + "CLIENT_SECRET")
        refresh_token = secret(prefix + "REFRESH_TOKEN")
        connect_clicked = st.button("Connect / refresh account", type="primary")
        st.caption("Credentials are read from environment variables or Streamlit secrets. Do not paste them into the app.")
    st.divider()
    st.header("Strategy")
    interval = st.selectbox("Candlestick interval", list(INTERVAL_MINUTES), index=0)
    period_map = {"1m": ["7d"], "5m": ["5d", "30d", "60d"],
                  "15m": ["5d", "30d", "60d"], "30m": ["5d", "30d", "60d"],
                  "60m": ["5d", "30d", "60d"]}
    period = st.selectbox("Yahoo history for chart / indicator warmup", period_map[interval], index=len(period_map[interval]) - 1)
    amplitude = st.number_input("HalfTrend amplitude", min_value=1, max_value=10, value=2)
    channel_deviation = st.number_input("ATR channel deviation", min_value=0.0, max_value=10.0, value=2.0, step=0.5)
    st.caption("Signals and Sandbox simulator prices use Yahoo Finance candles. Production orders use tastytrade quotes.")
    st.divider()
    arm_requested = False
    production_ack = False
    live_phrase = ""
    start_clicked = stop_clicked = False
    if production:
        st.header("Order limits")
        allocation_pct = st.slider("Maximum account equity per new position (%)", 1, 25, 5)
        max_portfolio_pct = st.slider("Maximum total long exposure (%)", 10, 100, 50)
        max_order_notional = st.number_input("Maximum notional per order ($)", min_value=25.0, max_value=10000.0, value=100.0, step=25.0)
        max_gap_pct = st.number_input("Maximum quote / signal close gap (%)", min_value=0.1, max_value=5.0, value=0.5, step=0.1)
        daily_loss_pct = st.number_input("Pause new entries after daily loss (%)", min_value=0.5, max_value=10.0, value=2.0, step=0.5)
        st.divider()
        st.header("Automation")
        st.caption("With automation enabled, open equity positions are scheduled to close at 12:45 p.m. Pacific each trading day.")
        arm_requested = st.checkbox("Allow automatic orders after Start", value=False)
        production_ack = st.checkbox("I understand this sends real orders to my production account.", value=False)
        live_phrase = st.text_input("Type ENABLE LIVE TRADING", type="password")
        start_clicked = st.button("Start automation")
        stop_clicked = st.button("Stop automation")
    else:
        allocation_pct = 5
        max_portfolio_pct = 50
        max_order_notional = 100.0
        max_gap_pct = 0.5
        daily_loss_pct = 2.0

if "tt_client" not in st.session_state:
    st.session_state.tt_client = None
    st.session_state.tt_quote_client = None
    st.session_state.tt_accounts = []
    st.session_state.tt_environment = None
    st.session_state.tt_account = ""
    st.session_state.auto_enabled = False
    st.session_state.connection_error = ""

if connect_clicked:
    st.session_state.auto_enabled = False
    if not (client_secret and refresh_token):
        st.session_state.connection_error = f"Set {prefix}CLIENT_SECRET and {prefix}REFRESH_TOKEN before connecting."
    else:
        try:
            client = TastytradeClient(client_secret=client_secret, refresh_token=refresh_token,
                                      client_id=client_id, base_url=PRODUCTION_URL if production else SANDBOX_URL,
                                      user_agent=f"StockTrader/{APP_VERSION}")
            accounts = client.get_accounts()
            st.session_state.tt_client = client
            st.session_state.tt_quote_client = client if production else None
            st.session_state.tt_accounts = accounts
            st.session_state.tt_environment = environment
            st.session_state.tt_account = str(account_record(accounts[0]).get("account-number", "")) if accounts else ""
            st.session_state.connection_error = ""
        except Exception as exc:
            st.session_state.tt_client = None
            st.session_state.connection_error = str(exc)

if stop_clicked:
    st.session_state.auto_enabled = False
if start_clicked:
    allowed = arm_requested and (not production or (production_ack and live_phrase == "ENABLE LIVE TRADING"))
    if not allowed:
        st.session_state.auto_enabled = False
        st.error("Automation was not started. Enable automatic orders; Production also requires the acknowledgement and exact phrase.")
    elif not st.session_state.tt_client or st.session_state.tt_environment != environment:
        st.session_state.auto_enabled = False
        st.error("Connect to an account in the selected environment before starting automation.")
    else:
        st.session_state.auto_enabled = True

if st.session_state.connection_error:
    st.error(st.session_state.connection_error)
if st.session_state.tt_environment and st.session_state.tt_environment != environment:
    st.session_state.auto_enabled = False
    if production:
        st.warning("Environment changed. Reconnect before viewing account data or enabling orders.")
if not production:
    st.session_state.auto_enabled = False

client = st.session_state.tt_client
accounts = st.session_state.tt_accounts or []
account_rows = []
for item in accounts:
    account = account_record(item)
    number = str(account.get("account-number", account.get("account_number", "")))
    if number:
        account_rows.append((number, account.get("nickname", "")))
if production and client and account_rows:
    labels = [f"{number} · {nickname}" if nickname else number for number, nickname in account_rows]
    selected_label = st.sidebar.selectbox("Account", labels,
        index=next((i for i, row in enumerate(account_rows) if row[0] == st.session_state.tt_account), 0))
    st.session_state.tt_account = account_rows[labels.index(selected_label)][0]

@st.fragment(run_every="60s")
def render_dashboard():
    active_client = st.session_state.tt_client
    account_number = st.session_state.tt_account
    sandbox_mode = environment == "Sandbox"
    if not sandbox_mode and active_client and account_number and st.session_state.tt_environment == environment:
        try:
            balances_response = active_client.get_balances(account_number)
            balance = payload_data(balances_response)
            if isinstance(balance, dict) and isinstance(balance.get("balances"), dict):
                balance = balance["balances"]
            positions = active_client.get_positions(account_number)
            orders = active_client.get_orders(account_number)
            position_items = [p for p in positions if isinstance(p, dict)]
        except Exception as exc:
            st.error(f"Broker refresh failed: {exc}")
            return
    else:
        balance, position_items, orders = {}, [], []

    try:
        symbols = fetch_most_active_symbols(50)
        universe_ok = True
    except Exception as exc:
        st.error(f"Could not refresh Yahoo Most Actives: {exc}")
        manual = st.text_input("Symbols to monitor when the screener is unavailable", "NVDA, AAPL, MSFT")
        symbols = list(dict.fromkeys(x.strip().upper() for x in manual.split(",") if x.strip()))
        universe_ok = False

    held = [str(p.get("underlying-symbol", p.get("symbol", ""))).upper() for p in position_items]
    symbols = list(dict.fromkeys(symbols + [s for s in held if s]))
    save_observed_symbols(DB_PATH, symbols)
    st.caption(f"Yahoo Most Actives · {len(symbols)} monitored symbols · {interval} bars · {period} history · refreshed {datetime.now(PT).strftime('%I:%M:%S %p PT')}")

    try:
        ohlc = fetch_yfinance_ohlc(symbols, period=period, interval=interval, auto_adjust=False)
    except Exception as exc:
        st.error(f"Yahoo OHLC download failed; signal processing is skipped this refresh: {exc}")
        # Keep the closeout path alive; Production can use broker position marks,
        # and Sandbox can use each virtual position's last recorded mark.
        ohlc = {}

    signal_rows = []
    indicators = {}
    usable_frames = {}
    signal_by_symbol = {}
    sentiment_by_symbol = load_sentiment_snapshot(DB_PATH, max_age_hours=24)
    now_utc = pd.Timestamp.now(tz="UTC")
    interval_mins = INTERVAL_MINUTES[interval]
    for symbol, frame in ohlc.items():
        if len(frame) < max(10, amplitude + 1):
            continue
        indicator = crossover_signals(frame, amplitude=int(amplitude), channel_deviation=float(channel_deviation))
        end_of_bar, age = bar_freshness(frame, interval)
        # A candle is usable only after its interval has elapsed.
        if end_of_bar is None or end_of_bar.tz_convert("UTC") > now_utc:
            frame = frame.iloc[:-1]
            indicator = indicator.iloc[:-1]
            if frame.empty:
                continue
            end_of_bar, age = bar_freshness(frame, interval)
        indicators[symbol] = indicator
        usable_frames[symbol] = frame
        row = indicator.iloc[-1]
        bar_time = str(indicator.index[-1])
        signal = "BUY" if bool(row.buy_signal) else ("SELL" if bool(row.sell_signal) else "HOLD")
        signal_by_symbol[symbol] = (signal, bar_time, float(frame.iloc[-1].Close), age)
        measured = sentiment_by_symbol.get(symbol, {})
        priority = sentiment_priority(signal, measured)
        priority_label = ("No signal" if signal == "HOLD" else
                          "Aligned" if priority[0] else
                          "Not aligned" if measured else "No recent score")
        signal_rows.append({"Symbol": symbol, "Signal": signal, "Bar time": bar_time,
                            "Close": float(frame.iloc[-1].Close), "Bar age (sec)": round(age, 0),
                            "Article sentiment": round(measured["score"], 1) if measured else None,
                            "Sentiment confidence (%)": round(measured["confidence"], 0) if measured else None,
                            "Sentiment alignment": priority_label,
                            "Sentiment priority": round(priority[1], 0)})

    quote_map = {}
    quote_client = active_client if not sandbox_mode else None
    if quote_client and account_number:
        try:
            quote_map = broker_quotes(quote_client, list(dict.fromkeys(list(usable_frames) + held)))
        except Exception as exc:
            if sandbox_mode and delayed_sandbox_quotes:
                st.warning(f"Using Yahoo prices for the virtual portfolio; broker quotes were unavailable: {exc}")
            else:
                st.warning(f"Tastytrade quotes unavailable; automatic order submission is paused: {exc}")
    if sandbox_mode and delayed_sandbox_quotes:
        # Accept Yahoo's latest completed candle in Sandbox regardless of its age.
        for symbol, frame in ohlc.items():
            if symbol in quote_map or frame.empty:
                continue
            last_bar = frame.iloc[-1]
            bar_time = frame.index[-1]
            close = numeric(last_bar.get("Close"))
            if close is None or not math.isfinite(close) or close <= 0:
                continue
            bar_utc = pd.Timestamp(bar_time).tz_localize("UTC") if pd.Timestamp(bar_time).tzinfo is None else pd.Timestamp(bar_time).tz_convert("UTC")
            bar_close_utc = bar_utc + pd.Timedelta(minutes=interval_mins)
            age = (pd.Timestamp.now(tz="UTC") - bar_close_utc).total_seconds()
            if age < 0:  # Never use a still-forming candle as a quote.
                continue
            stamp = bar_close_utc.isoformat()
            quote_map[symbol] = {"symbol": symbol, "bid": close, "ask": close, "mid": close,
                                 "mark": close, "last": close, "updated-at": stamp,
                                 "quote-source": "Yahoo delayed candle close"}
        daily_candidates = sorted((set(usable_frames) | set(held)) - set(quote_map))
        try:
            delayed_daily = fetch_yfinance_delayed_closes(daily_candidates, period="5d") if daily_candidates else {}
        except Exception as exc:
            delayed_daily = {}
            st.warning(f"Yahoo daily-close fallback could not load: {exc}")
        for symbol, (close, raw_stamp) in delayed_daily.items():
            if not math.isfinite(close) or close <= 0:
                continue
            if raw_stamp is None:
                stamp = pd.Timestamp.now(tz="UTC").isoformat()
                source = "Yahoo fast-info (timestamp unavailable)"
            else:
                session_date = pd.Timestamp(raw_stamp)
                if session_date.tzinfo is not None:
                    session_date = session_date.tz_convert(ET)
                session_close = session_date.date().isoformat() + " 16:00:00"
                stamp = pd.Timestamp(session_close, tz=ET).tz_convert("UTC").isoformat()
                source = "Yahoo delayed daily close"
            quote_map[symbol] = {"symbol": symbol, "bid": close, "ask": close, "mid": close,
                                 "mark": close, "last": close, "updated-at": stamp,
                                 "quote-source": source}
        if any(q.get("quote-source") == "Yahoo delayed candle close" for q in quote_map.values()):
            st.caption("Virtual fills use delayed Yahoo candle closes as reference prices; no broker orders are submitted.")
        if any(str(q.get("quote-source", "")).startswith("Yahoo delayed daily close") for q in quote_map.values()):
            st.warning("Some Sandbox prices use Yahoo daily closes, regardless of age. They are delayed reference prices, not live bid/ask quotes.")
        if any(str(q.get("quote-source", "")).startswith("Yahoo fast-info") for q in quote_map.values()):
            st.warning("Some Sandbox prices use Yahoo fast-info without a reported timestamp. Verify the source price before relying on these simulated orders.")
    missing_quotes = sorted(set(usable_frames).union(held) - set(quote_map)) if sandbox_mode or (active_client and account_number) else []
    for row in signal_rows:
        q = quote_map.get(row["Symbol"], {})
        row["Quote source"] = q.get("quote-source", "tastytrade" if q else "Unavailable")
    if missing_quotes:
        delayed_status = "Yahoo returned no usable reference price for" if sandbox_mode else "No fresh tastytrade quote for"
        st.warning(f"{delayed_status} {', '.join(missing_quotes[:10])}"
                   + (" …" if len(missing_quotes) > 10 else "")
                   + ("; the simulator will use the latest Yahoo candle close when available." if sandbox_mode
                      else "; automatic orders for those symbols are blocked."))

    if sandbox_mode or (active_client and account_number):
        nlv = numeric(balance, "net-liquidating-value", "net_liquidating_value")
        cash = numeric(balance, "cash-balance", "cash_balance")
        buying_power = numeric(balance, "equity-buying-power", "equity_buying_power", default=cash)
        baseline = persist_equity(f"{environment}:{account_number}", nlv, cash) if nlv else 0.0
        day_loss_block = False
        equity_history, recent_events = history_tables(f"{environment}:{account_number}")
        if nlv and not equity_history.empty:
            today = datetime.now(PT).date().isoformat()
            same_day = equity_history[equity_history.timestamp.str.startswith(today)]
            if not same_day.empty:
                day_open_nlv = float(same_day.iloc[0].net_liquidating_value)
                day_loss_block = nlv <= day_open_nlv * (1 - daily_loss_pct / 100)

        positions_by_symbol = {}
        portfolio_rows = []
        for position in position_items:
            symbol = str(position.get("underlying-symbol", position.get("symbol", ""))).upper()
            qty = signed_quantity(position)
            quote = quote_map.get(symbol, {})
            mark = numeric(quote, "mark", "mid", "last", default=numeric(position, "mark", "mark-price", "close-price"))
            average = numeric(position, "average-open-price")
            unrealized = (mark - average) * qty if mark and average else 0.0
            positions_by_symbol[symbol] = {"qty": qty, "position": position}
            portfolio_rows.append({"Symbol": symbol, "Side": "Long" if qty >= 0 else "Short",
                                   "Shares": abs(qty), "Avg cost": average, "Mark": mark,
                                   "Market value": abs(qty) * mark, "Unrealized P&L": unrealized})

        event_account_key = sandbox_simulator_key() if sandbox_mode else f"{environment}:{account_number}"
        now_et = datetime.now(ET)
        eod_closeout_window = in_preclose_window(now_et)
        sim_price_map = {}
        if sandbox_mode:
            for symbol in set(quote_map) | set(usable_frames):
                quote = quote_map.get(symbol, {})
                price = numeric(quote, "mark", "mid", "last", "bid", "ask")
                if price <= 0 and symbol in usable_frames and not usable_frames[symbol].empty:
                    price = numeric(usable_frames[symbol].iloc[-1], "Close")
                if price > 0:
                    sim_price_map[symbol] = price
            mark_sandbox_sim_positions(event_account_key, sim_price_map)
            sim_positions, _, _, _ = sandbox_sim_snapshot(event_account_key, sandbox_sim_starting_capital)
            if eod_closeout_window:
                for symbol, sim_position in sim_positions.items():
                    close_price = sim_price_map.get(symbol, sim_position["last_price"])
                    close_sandbox_sim_position(event_account_key, symbol, close_price, now_et)
            elif in_regular_session():
                ordered_signals = sorted(
                    signal_by_symbol.items(),
                    key=lambda item: sentiment_priority(item[1][0], sentiment_by_symbol.get(item[0])),
                    reverse=True,
                )
                for symbol, (signal, signal_time, signal_close, _) in ordered_signals:
                    if signal not in ("BUY", "SELL"):
                        continue
                    quote = quote_map.get(symbol, {})
                    side_key = "ask" if signal == "BUY" else "bid"
                    fill_price = numeric(quote, side_key)
                    if fill_price <= 0:
                        fill_price = numeric(quote, "mid", "last", default=signal_close)
                    if fill_price <= 0:
                        fill_price = signal_close
                    apply_sandbox_sim_signal(
                        event_account_key, symbol, signal_time, signal, fill_price,
                        sandbox_sim_starting_capital, sandbox_sim_allocation_pct,
                    )
        if sandbox_mode:
            sim_positions, sim_realized, sim_unrealized, sim_equity = sandbox_sim_snapshot(
                event_account_key, sandbox_sim_starting_capital)
            persist_sandbox_sim_equity(event_account_key, sim_equity)
            sim_equity_history = sandbox_sim_equity_history(event_account_key)
            daily_history = sandbox_sim_daily_history(event_account_key)
            today = datetime.now(PT).date().isoformat()
            daily_realized_row = daily_history[daily_history.Day == today]
            daily_realized = float(daily_realized_row.iloc[0].Realized_PnL) if not daily_realized_row.empty else 0.0
            daily_profit = daily_realized + sim_unrealized
            total_profit = sim_realized + sim_unrealized
            st.subheader("Sandbox simulated strategy P&L")
            st.caption("Virtual signal fills use Yahoo candle prices. The simulator is separate from tastytrade accounts and persists its P&L across app and sandbox resets.")
            sim_cols = st.columns(4)
            sim_cols[0].metric("Today’s simulated profit", f"${daily_profit:+,.2f}",
                               f"{daily_profit / sandbox_sim_starting_capital * 100:+.2f}% of virtual starting capital")
            sim_cols[1].metric("Total simulated profit", f"${total_profit:+,.2f}",
                               f"{total_profit / sandbox_sim_starting_capital * 100:+.2f}% across all days")
            sim_cols[2].metric("Virtual account value", f"${sim_equity:,.2f}")
            sim_cols[3].metric("Open simulated positions", str(len(sim_positions)))
            if not sim_equity_history.empty:
                render_equity_history(
                    sim_equity_history, "equity", "Sandbox simulator equity over time",
                    "sandbox_equity_start_date")
            if sim_positions:
                st.markdown("**Positions currently held by the simulator**")
                sim_position_rows = []
                for symbol, position in sorted(sim_positions.items()):
                    qty = position["quantity"]
                    sim_position_rows.append({
                        "Symbol": symbol,
                        "Side": "Long" if qty > 0 else "Short",
                        "Shares": abs(qty),
                        "Average entry": position["average_price"],
                        "Latest mark": position["last_price"],
                        "Unrealized P&L": qty * (position["last_price"] - position["average_price"]),
                        "Opened": position["opened_at"],
                    })
                st.dataframe(pd.DataFrame(sim_position_rows), use_container_width=True, hide_index=True)
            else:
                st.caption("The simulator currently holds no positions.")
            if not daily_history.empty:
                st.markdown("**Daily simulated realized profit**")
                st.dataframe(daily_history, use_container_width=True, hide_index=True)
            with st.expander("Recent simulated trade log"):
                sim_trade_history = sandbox_sim_trade_history(event_account_key)
                if not sim_trade_history.empty:
                    st.dataframe(sim_trade_history, use_container_width=True, hide_index=True)
                else:
                    st.caption("No simulated signal trades have been recorded yet.")
        else:
            metric_cols = st.columns(4)
            metric_cols[0].metric("Net liquidation value", f"${nlv:,.2f}" if nlv else "—")
            metric_cols[1].metric("Profit since app start", f"${nlv - baseline:+,.2f}" if nlv and baseline else "—")
            metric_cols[2].metric("Cash balance", f"${cash:,.2f}")
            metric_cols[3].metric("Automation", "RUNNING" if st.session_state.auto_enabled else "OFF")
        if day_loss_block:
            st.error(f"Daily loss limit ({daily_loss_pct:.1f}%) reached. New entries are paused; HalfTrend exits remain enabled.")

        # Record every signal for audit. Submit only after explicit Start and all safety checks pass.
        active_symbols = active_orders_by_symbol(orders)
        regular_session = in_regular_session()
        ordered_signals = sorted(
            signal_by_symbol.items(),
            key=lambda item: sentiment_priority(item[1][0], sentiment_by_symbol.get(item[0])),
            reverse=True,
        )
        for symbol, (signal, signal_time, signal_close, age) in ordered_signals:
            if sandbox_mode:
                continue
            if signal not in ("BUY", "SELL"):
                continue
            if has_event(event_account_key, symbol, signal_time, signal):
                continue
            action, quantity, limit_price = None, 0, 0.0
            order = None
            external_id = f"ht-{uuid.uuid4().hex[:20]}"
            status = "Observed · monitor only"
            details = "No order submitted."
            update_event(event_account_key, symbol, signal_time, signal, status, details, external_id)
            if not st.session_state.auto_enabled:
                continue
            if eod_closeout_window:
                status, details = "Skipped", "New entries are paused during the 12:45 p.m. Pacific closeout window."
            elif not universe_ok and symbol not in held:
                status, details = "Blocked", "Most Actives list did not refresh; automatic entries disabled."
            elif not regular_session:
                status, details = "Blocked", "Outside the regular US equity session."
            elif age > max(180, interval_mins * 60 * 2) and not str(quote_map.get(symbol, {}).get("quote-source", "")).startswith("Yahoo"):
                status, details = "Blocked", f"Yahoo candle is stale ({age:.0f}s old)."
            elif symbol not in quote_map or (not str(quote_map.get(symbol, {}).get("quote-source", "")).startswith("Yahoo") and not fresh_quote(quote_map.get(symbol))):
                status, details = "Blocked", "No sufficiently fresh quote or delayed candle close."
            elif symbol in active_symbols:
                status, details = "Blocked", "An open tastytrade order already exists for this symbol."
            elif signal in ("BUY", "SELL") and day_loss_block and (not positions_by_symbol.get(symbol) or positions_by_symbol[symbol]["qty"] == 0):
                status, details = "Blocked", "Daily loss limit pauses new long and short entries."
            else:
                quote = quote_map[symbol]
                bid, ask = numeric(quote, "bid"), numeric(quote, "ask")
                quote_source = quote.get("quote-source", "tastytrade")
                position_state = positions_by_symbol.get(symbol)
                current_qty = position_state["qty"] if position_state else 0
                fractional_order = False
                if active_client.is_production:
                    try:
                        equity_definition = payload_data(active_client.get_equity(symbol))
                        fractional_order = bool(equity_definition.get("is-fractional-quantity-eligible"))
                    except Exception:
                        # A failed eligibility lookup must never be treated as approval.
                        fractional_order = False
                if signal == "BUY":
                    if not bid or not ask or ask < bid:
                        status, details = "Blocked", "Quote prices are unavailable or invalid."
                    elif current_qty > 0:
                        status, details = "Skipped", "Long position already exists; no additional shares will be added."
                    elif not str(quote.get("quote-source", "")).startswith("Yahoo") and abs(ask - signal_close) / signal_close * 100 > max_gap_pct:
                        status, details = "Blocked", "Live ask is too far from the Yahoo signal close."
                    elif current_qty < 0:
                        action, quantity, limit_price = "Buy to Close", math.floor(abs(current_qty)), round(ask, 2)
                        if quantity < 1:
                            status, details = "Skipped", "Short position has less than one whole share."
                    else:
                        current_exposure = max(
                            sum(row["Market value"] for row in portfolio_rows if row["Side"] == "Long"),
                            numeric(balance, "long-equity-value", "long_equity_value"),
                        )
                        remaining_exposure = max(0.0, nlv * max_portfolio_pct / 100 - current_exposure)
                        # Use settled cash as a hard guard so account margin is not intentionally used.
                        budget = min(max(0.0, nlv * allocation_pct / 100), max(0.0, buying_power),
                                     max(0.0, cash), max_order_notional, remaining_exposure)
                        quantity = (math.floor(budget / ask) if not fractional_order
                                    else math.floor((budget / ask) * 10000) / 10000)
                        if quantity <= 0 or (quantity < 1 and not fractional_order):
                            status, details = "Skipped", "Risk budget is too small for one whole share or the symbol is not fractional-eligible."
                        else:
                            action, limit_price = "Buy to Open", round(ask, 2)
                else:
                    if not bid or not ask or ask < bid:
                        status, details = "Blocked", "Quote prices are unavailable or invalid."
                    elif not str(quote.get("quote-source", "")).startswith("Yahoo") and abs(bid - signal_close) / signal_close * 100 > max_gap_pct:
                        status, details = "Blocked", "Live bid is too far from the Yahoo signal close."
                    elif current_qty < 0:
                        status, details = "Skipped", "Short position already exists; no additional shares will be sold short."
                    elif current_qty > 0:
                        quantity = (round(abs(current_qty), 4) if fractional_order
                                    else math.floor(current_qty))
                        action, limit_price = "Sell to Close", round(bid, 2)
                        if quantity <= 0:
                            status, details = "Skipped", "Long position has less than one whole share and is not fractional-eligible."
                    else:
                        short_exposure = sum(row["Market value"] for row in portfolio_rows if row["Side"] == "Short")
                        short_capacity = max(0.0, nlv * max_portfolio_pct / 100 - short_exposure)
                        budget = min(max(0.0, nlv * allocation_pct / 100), max(0.0, buying_power),
                                     max_order_notional, short_capacity)
                        quantity = math.floor(budget / bid) if bid > 0 else 0
                        if quantity < 1:
                            status, details = "Skipped", "Short risk budget is too small for one whole share."
                        else:
                            action, limit_price = "Sell to Open", round(bid, 2)
                if status == "Observed · monitor only" and action is not None and quantity > 0:
                    try:
                        if quote_source.startswith("Yahoo delayed"):
                            details = f"Sandbox order priced from {quote_source}."
                        order = TastytradeClient.build_equity_order(symbol, action, quantity, limit_price,
                                                                    source="StockTrader/1.0.0",
                                                                    allow_fractional=(
                                                                        fractional_order
                                                                        and action in ("Buy to Open", "Sell to Close")
                                                                    ))
                        update_event(event_account_key, symbol, signal_time, signal, "Preflight", str(order), order["external-identifier"])
                        preview = active_client.dry_run_order(account_number, order)
                        preview_data = payload_data(preview)
                        errors = preview_data.get("errors", []) if isinstance(preview_data, dict) else []
                        if errors:
                            raise TastytradeAPIError(f"Broker dry-run returned errors: {errors}")
                        warnings = preview_data.get("warnings", []) if isinstance(preview_data, dict) else []
                        if warnings:
                            raise TastytradeAPIError(f"Broker dry-run returned warnings; automatic submission paused: {warnings}")
                        result = active_client.submit_order(account_number, order, confirm_live=True,
                            max_order_notional=(max_order_notional if action in ("Buy to Open", "Sell to Open")
                                                else quantity * limit_price))
                        status, details = "Submitted", str(payload_data(result))[:500]
                        active_symbols.add(symbol)
                    except Exception as exc:
                        status, details = "Order error / check broker", str(exc)[:500]
                    finally:
                        if order and order.get("external-identifier"):
                            external_id = order["external-identifier"]
            update_event(event_account_key, symbol, signal_time, signal, status, details, external_id)

        if not sandbox_mode and eod_closeout_window:
            if not st.session_state.auto_enabled:
                open_equities = [symbol for symbol, state in positions_by_symbol.items()
                                 if state["qty"] != 0
                                 and str(state["position"].get("instrument-type", "Equity")).lower() in ("equity", "stock")
                                 and str(state["position"].get("symbol", symbol)).upper() == symbol]
                if open_equities or active_symbols:
                    st.error("Production positions or working orders remain open, but automation is OFF. Enable authorized automation before the 12:45 p.m. Pacific closeout window.")
                else:
                    st.info("No open Production equity positions need closing right now.")
            else:
                close_failures = []
                close_pending = []
                close_submitted = 0
                for item in orders:
                    live_order = item.get("order", item) if isinstance(item, dict) else {}
                    if str(live_order.get("status", "")).lower() in TERMINAL_ORDER_STATES:
                        continue
                    symbols_in_order = {
                        str(leg.get("symbol", "")).upper()
                        for leg in (live_order.get("legs") or [])
                        if str(leg.get("instrument-type", "Equity")).lower() in ("equity", "stock")
                    }
                    if not symbols_in_order:
                        continue
                    close_pending.extend(symbols_in_order)
                    if not live_order.get("cancellable", True):
                        continue
                    order_id = live_order.get("id")
                    if not order_id:
                        close_failures.append(f"{', '.join(sorted(symbols_in_order))}: working order has no broker id to cancel")
                        continue
                    try:
                        active_client.cancel_order(account_number, order_id)
                    except Exception as exc:
                        close_failures.append(f"{', '.join(sorted(symbols_in_order))}: order cancellation failed ({str(exc)[:180]})")
                for symbol, state in positions_by_symbol.items():
                    qty = state["qty"]
                    if qty == 0:
                        continue
                    position = state["position"]
                    instrument_symbol = str(position.get("symbol", symbol)).upper()
                    instrument_type = str(position.get("instrument-type", "Equity")).lower()
                    if instrument_type not in ("equity", "stock") or instrument_symbol != symbol:
                        close_failures.append(f"{symbol}: unsupported non-equity position")
                        continue
                    if symbol in active_symbols:
                        close_pending.append(symbol)
                        continue
                    prior = get_event(event_account_key, symbol, now_et.date().isoformat(), "EOD_EXIT")
                    if prior:
                        prior_status, prior_id = prior
                        prior_order = next((item.get("order", item) for item in orders
                                            if isinstance(item, dict)
                                            and item.get("order", item).get("external-identifier") == prior_id), None)
                        if prior_order is None:
                            close_failures.append(f"{symbol}: prior close submission outcome is unconfirmed; check broker orders")
                            continue
                        broker_status = str(prior_order.get("status", "")).lower()
                        if broker_status in TERMINAL_ORDER_STATES:
                            if broker_status in ("filled", "complete", "completed"):
                                continue
                            # A terminal rejected/cancelled/expired order may be retried
                            # against the current position snapshot.
                        else:
                            close_pending.append(symbol)
                            continue
                    quantity = round(abs(qty), 4)
                    if quantity <= 0:
                        close_failures.append(f"{symbol}: position quantity is too small to close")
                        continue
                    quote = quote_map.get(symbol, {})
                    reference_price = numeric(quote, "mark", "mid", "last", "bid", "ask")
                    if reference_price <= 0:
                        reference_price = numeric(position, "mark", "mark-price", "close-price", "average-open-price")
                    if reference_price <= 0:
                        close_failures.append(f"{symbol}: no reference price for order validation")
                        continue
                    action = "Sell to Close" if qty > 0 else "Buy to Close"
                    order = None
                    try:
                        if not math.isclose(quantity, round(quantity), abs_tol=1e-6):
                            if qty < 0:
                                raise ValueError("Fractional short positions cannot be closed by this equity strategy.")
                            definition = payload_data(active_client.get_equity(symbol))
                            if not definition.get("is-fractional-quantity-eligible"):
                                raise ValueError("Tastytrade does not mark this equity eligible for fractional closing.")
                            order = TastytradeClient.build_equity_order(
                                symbol, action, quantity, reference_price,
                                source="StockTrader/EOD-Production", allow_fractional=True)
                        else:
                            order = TastytradeClient.build_equity_market_close(
                                symbol, action, int(round(quantity)),
                                source="StockTrader/EOD-Production")
                        preview = active_client.dry_run_order(account_number, order)
                        preview_data = payload_data(preview)
                        errors = preview_data.get("errors", []) if isinstance(preview_data, dict) else []
                        warnings = preview_data.get("warnings", []) if isinstance(preview_data, dict) else []
                        if errors or warnings:
                            raise TastytradeAPIError(f"EOD close dry-run blocked: errors={errors}, warnings={warnings}")
                        result = active_client.submit_order(
                            account_number, order, confirm_live=True,
                            max_order_notional=quantity * reference_price,
                            risk_reference_price=reference_price if order.get("order-type") == "Market" else None,
                        )
                        active_symbols.add(symbol)
                        close_submitted += 1
                        details = f"Production preclose order submitted ({action} {quantity:g} shares); reference price ${reference_price:.2f}."
                        update_event(event_account_key, symbol, now_et.date().isoformat(), "EOD_EXIT",
                                     "Submitted", details, order["external-identifier"])
                    except Exception as exc:
                        message = f"{symbol}: {str(exc)[:250]}"
                        close_failures.append(message)
                        if order:
                            update_event(event_account_key, symbol, now_et.date().isoformat(), "EOD_EXIT",
                                         "Close error / check broker", message, order.get("external-identifier", ""))
                if close_failures:
                    st.error("Production preclose closeout needs attention: " + "; ".join(close_failures))
                elif close_pending:
                    st.warning("Production closeout is waiting for working-order cancellations or close orders on: " + ", ".join(sorted(set(close_pending))) + ". Keep the app running and verify fills before 1:00 p.m. Pacific.")
                elif close_submitted:
                    st.warning(f"Submitted {close_submitted} Production preclose order(s). Keep the app running and verify fills before 1:00 p.m. Pacific.")
                else:
                    st.info("No open Production equity positions need closing right now.")

        # Refresh account view after any automatic submission.
        if not sandbox_mode and st.session_state.auto_enabled:
            try:
                balances_response = active_client.get_balances(account_number)
                balance = payload_data(balances_response)
                if isinstance(balance, dict) and isinstance(balance.get("balances"), dict):
                    balance = balance["balances"]
                positions = active_client.get_positions(account_number)
                position_items = [p for p in positions if isinstance(p, dict)]
                nlv = numeric(balance, "net-liquidating-value", "net_liquidating_value")
                cash = numeric(balance, "cash-balance", "cash_balance")
                if nlv:
                    persist_equity(event_account_key, nlv, cash)
            except Exception as exc:
                st.warning(f"Order-refresh snapshot unavailable: {exc}")

        if not sandbox_mode and portfolio_rows:
            st.subheader("Current holdings")
            st.dataframe(pd.DataFrame(portfolio_rows), use_container_width=True, hide_index=True)
        elif not sandbox_mode:
            st.info("No open equity holdings returned for this account.")
        equity_history, recent_events = history_tables(event_account_key) if not sandbox_mode else (pd.DataFrame(), pd.DataFrame())
        if not sandbox_mode and not equity_history.empty:
            st.subheader("Production account equity over time")
            render_equity_history(
                equity_history, "net_liquidating_value", "Production account equity over time",
                "production_equity_start_date")
        if not sandbox_mode and not recent_events.empty:
            st.subheader("Signal and order activity")
            st.dataframe(recent_events, use_container_width=True, hide_index=True)
    elif not sandbox_mode:
        st.info("Connect a tastytrade account to see live holdings, P&L, and equity history. Monitor-only chart and signals work without broker credentials.")

    st.subheader("Current simulator signals" if sandbox_mode else "Latest HalfTrend signals")
    if signal_rows:
        signal_df = pd.DataFrame(signal_rows).sort_values(
            ["Sentiment priority", "Symbol"], ascending=[False, True]
        )
        st.dataframe(signal_df, use_container_width=True, hide_index=True)
        choices = sorted(usable_frames)
        selected_symbol = st.selectbox("Chart symbol", choices, key="chart_symbol")
        st.plotly_chart(make_chart(usable_frames[selected_symbol], indicators[selected_symbol], selected_symbol, interval),
                        use_container_width=True)
    else:
        st.warning("No usable Yahoo Finance OHLC bars were returned for this refresh.")

    if not sandbox_mode:
        st.caption("Sell signals open shorts when flat and close longs; buy signals open longs when flat and cover shorts. Every order is broker dry-run first; uncertain submissions are not retried. Review broker order status and this app's signal log.")

render_dashboard()
