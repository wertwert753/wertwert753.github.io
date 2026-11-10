"""Trading and portfolio service used by the lightweight StockTrader web UI."""
from __future__ import annotations

from datetime import datetime, time as wall_time, timezone
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from zoneinfo import ZoneInfo

import pandas as pd

from market_data import fetch_most_active_symbols, fetch_yfinance_delayed_closes, fetch_yfinance_ohlc
from observed_universe import save_observed_symbols
from sentiment_store import load_sentiment_snapshot, sentiment_priority
from strategy import crossover_signals, get_last_signal
from tastytrade_client_orders import PRODUCTION_URL, SANDBOX_URL, TastytradeAPIError, TastytradeClient

PT, ET = ZoneInfo("America/Los_Angeles"), ZoneInfo("America/New_York")
INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
TERMINAL_ORDER_STATES = {"filled", "cancelled", "canceled", "rejected", "expired"}


def number(obj, *keys, default=0.0):
    for key in keys:
        try:
            value = obj.get(key) if isinstance(obj, dict) else None
            if value is not None and math.isfinite(float(value)):
                return float(value)
        except (ValueError, TypeError):
            continue
    return default


def payload(response):
    return response.get("data", response) if isinstance(response, dict) else response


class TradingService:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.lock = threading.RLock()
        self.client = None
        self.environment = "Sandbox"
        self.account_number = ""
        self.accounts = []
        self.auto_enabled = False
        self.last_error = ""
        self.settings = {
            "environment": "Sandbox", "interval": "1m", "period": "7d",
            "amplitude": 2, "channel_deviation": 2.0, "initial_capital": 500.0,
            "sim_allocation_pct": 20, "allocation_pct": 5,
            "max_portfolio_pct": 50, "max_order_notional": 100.0,
            "max_gap_pct": 0.5, "daily_loss_pct": 2.0,
            "symbols": [], "periods": {"1m": ["7d"], "5m": ["5d", "30d", "60d"],
                       "15m": ["5d", "30d", "60d"], "30m": ["5d", "30d", "60d"],
                       "60m": ["5d", "30d", "60d"]},
        }
        self.snapshot = {"environment": "Sandbox", "signals": [], "positions": [],
                         "simulator": {}, "equity": [], "events": [], "bars": {},
                         "updated_at": None, "warnings": [], "connected": False,
                         "automation": False}
        self.FIRST_TRADE = True
        self.init_db()

    def init_db(self):
        with sqlite3.connect(self.db_path) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS baseline(account_key TEXT PRIMARY KEY,nlv REAL NOT NULL,started_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS equity(id INTEGER PRIMARY KEY,account_key TEXT NOT NULL,timestamp TEXT NOT NULL,net_liquidating_value REAL NOT NULL,cash REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS signal_events(account_key TEXT NOT NULL,symbol TEXT NOT NULL,signal_time TEXT NOT NULL,side TEXT NOT NULL,status TEXT NOT NULL,details TEXT NOT NULL,external_id TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(account_key,symbol,signal_time,side));
            CREATE TABLE IF NOT EXISTS sandbox_sim_positions(account_key TEXT NOT NULL,symbol TEXT NOT NULL,quantity REAL NOT NULL,average_price REAL NOT NULL,last_price REAL NOT NULL,opened_at TEXT NOT NULL,PRIMARY KEY(account_key,symbol));
            CREATE TABLE IF NOT EXISTS sandbox_sim_trades(id INTEGER PRIMARY KEY,account_key TEXT NOT NULL,trading_day TEXT NOT NULL,symbol TEXT NOT NULL,signal_time TEXT NOT NULL,signal TEXT NOT NULL,action TEXT NOT NULL,quantity REAL NOT NULL,price REAL NOT NULL,realized_pnl REAL NOT NULL,created_at TEXT NOT NULL,UNIQUE(account_key,symbol,signal_time,signal));
            CREATE TABLE IF NOT EXISTS sandbox_sim_equity(id INTEGER PRIMARY KEY,account_key TEXT NOT NULL,timestamp TEXT NOT NULL,equity REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sandbox_sim_equity_adjustments(id INTEGER PRIMARY KEY,account_key TEXT NOT NULL,timestamp TEXT NOT NULL,amount REAL NOT NULL);
            """)

    def configure(self, values):
        with self.lock:
            if "environment" in values and values["environment"] != self.settings["environment"]:
                self.auto_enabled = False
            for key in self.settings:
                if key in values:
                    self.settings[key] = values[key]
            interval = self.settings["interval"]
            self.settings["period"] = values.get("period", self.settings["period"])
            allowed = self.settings["periods"].get(interval, ["5d"])
            if self.settings["period"] not in allowed:
                self.settings["period"] = allowed[-1]

    def set_virtual_equity(self, value):
        """Adjust Sandbox equity without altering the persisted trade ledger."""
        target = float(value)
        if not math.isfinite(target) or target <= 0 or target > 100_000_000:
            raise ValueError("Virtual equity must be greater than $0 and no more than $100,000,000.")
        with self.lock:
            if self.settings["environment"] != "Sandbox":
                raise ValueError("Virtual equity can only be changed in Sandbox mode.")
            initial_capital = float(self.settings["initial_capital"])
        key = self._simulator_key()
        with sqlite3.connect(self.db_path) as db:
            trades = db.execute(
                "SELECT COALESCE(SUM(realized_pnl),0) FROM sandbox_sim_trades WHERE account_key=?", (key,)
            ).fetchone()[0]
            positions = db.execute(
                "SELECT quantity,average_price,last_price FROM sandbox_sim_positions WHERE account_key=?", (key,)
            ).fetchall()
            adjustments = db.execute(
                "SELECT COALESCE(SUM(amount),0) FROM sandbox_sim_equity_adjustments WHERE account_key=?", (key,)
            ).fetchone()[0]
            unrealized = sum(q * (mark - avg) for q, avg, mark in positions)
            current = initial_capital + float(trades) + unrealized + float(adjustments)
            adjustment = target - current
            timestamp = datetime.now(PT).isoformat(timespec="seconds")
            db.execute(
                "INSERT INTO sandbox_sim_equity_adjustments(account_key,timestamp,amount) VALUES(?,?,?)",
                (key, timestamp, adjustment),
            )
            db.execute(
                "INSERT INTO sandbox_sim_equity(account_key,timestamp,equity) VALUES(?,?,?)",
                (key, timestamp, target),
            )

    def connect(self, environment):
        if environment == "Sandbox":
            with self.lock:
                self.client, self.environment, self.account_number, self.accounts = None, "Sandbox", "", []
                self.settings["environment"] = "Sandbox"
                self.auto_enabled = False
            return {"account_number": "", "accounts": [], "mode": "local simulator"}
        production = environment == "Production"
        prefix = "TASTYTRADE_PROD_" if production else "TASTYTRADE_"
        secret, token = __import__("os").getenv(prefix + "CLIENT_SECRET", ""), __import__("os").getenv(prefix + "REFRESH_TOKEN", "")
        client_id = __import__("os").getenv(prefix + "CLIENT_ID", "")
        if not secret or not token:
            raise ValueError(f"Set {prefix}CLIENT_SECRET and {prefix}REFRESH_TOKEN before connecting.")
        client = TastytradeClient(secret, token, client_id,
                                  PRODUCTION_URL if production else SANDBOX_URL,
                                  user_agent="StockTrader/2.0")
        accounts = client.get_accounts()
        if not accounts:
            raise RuntimeError("No brokerage accounts were returned.")
        first = accounts[0].get("account", accounts[0])
        account = str(first.get("account-number", ""))
        with self.lock:
            self.client, self.environment = client, environment
            self.account_number, self.accounts = account, accounts
            self.settings["environment"] = environment
            self.auto_enabled = False
        return {"account_number": account, "accounts": accounts}

    def automate(self, action, arm=False, production_ack=False, live_phrase=""):
        with self.lock:
            if action == "stop":
                self.auto_enabled = False
                return
            if action != "start":
                raise ValueError("Unknown automation action.")
            production = self.settings["environment"] == "Production"
            if not arm or (production and (not production_ack or live_phrase != "ENABLE LIVE TRADING")):
                raise PermissionError("Start requires the automatic-orders acknowledgement and, for Production, the live-trading confirmation.")
            if not self.client or self.environment != self.settings["environment"] or not self.account_number:
                raise RuntimeError("Connect to an account in the selected environment before starting automation.")
            self.auto_enabled = True

    def in_beginning_window(self,now_pt=None):
        """6:30 to 8:00 AM Pacific, Monday through Friday."""
        if now_pt is None:
            now_pt = datetime.now(PT)

        local_time = now_pt.astimezone(PT).time().replace(tzinfo=None)

        return (
            now_pt.astimezone(PT).weekday() < 5
            and wall_time(6, 30) <= local_time < wall_time(8, 0)
        )

    def in_preclose_window(self, now_pt=None):
        """12:45 PM to 1:00 PM Pacific, Monday through Friday."""
        if now_pt is None:
            now_pt = datetime.now(PT)

        local_time = now_pt.astimezone(PT).time().replace(tzinfo=None)

        return (
            now_pt.astimezone(PT).weekday() < 5
            and wall_time(12, 45) <= local_time < wall_time(13, 0)
        )

    def refresh(self):
        with self.lock:
            cfg = dict(self.settings)
            client, env, account = self.client, self.environment, self.account_number
            auto = self.auto_enabled
        warnings = []
        production = cfg["environment"] == "Production"
        if production and client and env == cfg["environment"] and account:
            try:
                balance = payload(client.get_balances(account))
                positions = client.get_positions(account)
                orders = client.get_orders(account)
            except Exception as exc:
                self.last_error = f"Broker refresh failed: {exc}"
                balance, positions, orders = {}, [], []
                warnings.append(self.last_error)
        else:
            balance, positions, orders = {}, [], []

        try:
            symbols = fetch_most_active_symbols()
            universe_ok = True
        except Exception as exc:
            universe_ok = False
            warnings.append(f"Yahoo Most Actives unavailable: {exc}")
            symbols = cfg.get("symbols") or ["NVDA", "AAPL", "MSFT"]
        held = [str(p.get("underlying-symbol", p.get("symbol", ""))).upper()
                for p in positions if isinstance(p, dict)]
        symbols = list(dict.fromkeys(symbols + [s for s in held if s]))
        save_observed_symbols(self.db_path, symbols)
        try:
            ohlc = fetch_yfinance_ohlc(symbols, period=cfg["period"], interval=cfg["interval"], auto_adjust=False)
        except Exception as exc:
            warnings.append(f"Yahoo OHLC download failed: {exc}")
            ohlc = {}

        signals, usable, bars = {}, {}, {}
        sentiment = load_sentiment_snapshot(self.db_path, max_age_hours=24)
        now = pd.Timestamp.now(tz="UTC")
        mins = INTERVAL_MINUTES[cfg["interval"]]
        count = 0
        num_symbols = len(ohlc)
        frames = {}
        for symbol, frame in ohlc.items():
            count += 1
            if len(frame) < max(10, int(cfg["amplitude"]) + 1):
                continue
            result = crossover_signals(frame, amplitude=int(cfg["amplitude"]),
                                       channel_deviation=float(cfg["channel_deviation"]))
            stamp = pd.Timestamp(frame.index[-1])
            frames[symbol] = result
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize(ET)
            bar_end = stamp + pd.Timedelta(minutes=mins)
            if bar_end.tz_convert("UTC") > now:
                frame, result = frame.iloc[:-1], result.iloc[:-1]
                if frame.empty:
                    continue
                stamp = pd.Timestamp(frame.index[-1])
                if stamp.tzinfo is None:
                    stamp = stamp.tz_localize(ET)
                bar_end = stamp + pd.Timedelta(minutes=mins)
            if frame.empty:
                continue
            age = max(0.0, (now - bar_end.tz_convert("UTC")).total_seconds())
            loc = -1
            if self.in_beginning_window() and self.FIRST_TRADE:
                signal, loc = get_last_signal(result)
                if count == (num_symbols - 1):
                    self.FIRST_TRADE = False
            row = result.iloc[loc]
            side = "BUY" if bool(row.buy_signal) else ("SELL" if bool(row.sell_signal) else "HOLD")
            close = float(frame.iloc[-1].Close)
            signal_time = str(result.index[-1])

            contrarian_exit = False
            last_position = None
            with sqlite3.connect(self.db_path) as db:
                positions = db.execute("SELECT * FROM sandbox_sim_positions")
                for position in positions:
                    if symbol == position[1]:
                        last_position = position
                        q = float(position[2])
                        last_side, last_loc = get_last_signal(result)
                        contrarian_exit = (
                                        (q > 0 and last_side == "SELL")
                                        or (q < 0 and last_side == "BUY")
                                    )
            if contrarian_exit:
                side = last_side
                signal_time = str(result.index[last_loc])

            eod_sim_close = self.in_preclose_window()
            if eod_sim_close:
                last_order = "BUY" if float(last_position[1]) > 0 else "SELL"
                side = "BUY" if last_order == "SELL" else "SELL"
                signal_time = str(result.index[-1])

            record = {"symbol": symbol, "signal": side, "signal_time": signal_time,
                      "close": close, "bar_age_seconds": round(age),
                      "universe_ok": universe_ok,
                      "sentiment": sentiment.get(symbol), "priority": sentiment_priority(side, sentiment.get(symbol))[1]}
            signals[symbol] = record
            usable[symbol] = frame
            bars[symbol] = {"time": [str(x) for x in frame.index[-300:]],
                            "open": frame.Open.tail(300).astype(float).tolist(),
                            "high": frame.High.tail(300).astype(float).tolist(),
                            "low": frame.Low.tail(300).astype(float).tolist(),
                            "close": frame.Close.tail(300).astype(float).tolist(),
                            "halftrend": result.halftrend.tail(300).astype(object).where(pd.notna(result.halftrend.tail(300)), None).tolist(),
                            "buy": frame.Close.tail(300).astype(object).where(result.buy_signal.tail(300), None).tolist(),
                            "sell": frame.Close.tail(300).astype(object).where(result.sell_signal.tail(300), None).tolist(),
                            "signal": result.signal.tail(300).fillna("").tolist()}
        quote_map = {}
        if production and client and account:
            try:
                quote_map = self._broker_quotes(client, symbols)
            except Exception as exc:
                warnings.append(f"Tastytrade quotes unavailable: {exc}")
        yahoo_prices = {}
        for symbol, frame in usable.items():
            if not frame.empty and float(frame.Close.iloc[-1]) > 0:
                yahoo_prices[symbol] = float(frame.Close.iloc[-1])
        try:
            fallback = fetch_yfinance_delayed_closes(list(set(usable) | set(held) | set(symbols)), period="5d")
            for symbol, pair in fallback.items():
                if pair and float(pair[0]) > 0:
                    yahoo_prices.setdefault(symbol, float(pair[0]))
        except Exception as exc:
            warnings.append(f"Yahoo delayed-price fallback unavailable: {exc}")

        positions_out = []
        if production:
            for item in positions:
                symbol = str(item.get("underlying-symbol", item.get("symbol", ""))).upper()
                qty = number(item, "quantity") * (-1 if str(item.get("quantity-direction", "Long")).lower() == "short" else 1)
                mark = number(quote_map.get(symbol, {}), "mark", "mid", "last", default=yahoo_prices.get(symbol, 0))
                average = number(item, "average-open-price")
                positions_out.append({"symbol": symbol, "quantity": qty, "average_price": average,
                                      "mark": mark, "unrealized_pnl": (mark-average)*qty})
        else:
            self._simulator(cfg, signals, yahoo_prices)
            positions_out = self._sim_positions(cfg)

        # Run normal production trading when automation is enabled.
        # Always run the production closeout during the EOD window,
        # even if automatic trading is disabled.
        eod_closeout = (
            production
            and self.in_preclose_window()
        )

        if (
            production
            and client
            and account
            and env == cfg["environment"]
            and (auto or eod_closeout)
        ):
            self._execute_orders(
                cfg,
                client,
                account,
                balance,
                positions,
                orders,
                signals,
                quote_map,
                yahoo_prices,
                warnings,
                frames
            )
        account_key = f"{cfg['environment']}:{account}"
        equity = []
        events = []
        baseline_nlv = 0.0
        if production and account:
            nlv = number(balance, "net-liquidating-value", "net_liquidating_value")
            cash = number(balance, "cash-balance", "cash_balance")
            if nlv:
                baseline_nlv = self._persist_equity(account_key, nlv, cash)
            with sqlite3.connect(self.db_path) as db:
                rows = db.execute("SELECT timestamp,net_liquidating_value,cash FROM equity WHERE account_key=? ORDER BY timestamp", (account_key,)).fetchall()
                events_rows = db.execute("SELECT created_at,symbol,side,status,details FROM signal_events WHERE account_key=? ORDER BY created_at DESC LIMIT 100", (account_key,)).fetchall()
            equity = [{"time": r[0], "value": r[1], "cash": r[2]} for r in rows]
            events = [{"time": r[0], "symbol": r[1], "signal": r[2], "status": r[3], "details": r[4]} for r in events_rows]

        simulator = self._simulator_summary(cfg) if not production else {}
        snapshot = {"environment": cfg["environment"], "signals": sorted(signals.values(), key=lambda x: x["priority"], reverse=True),
                    "positions": positions_out, "simulator": simulator, "equity": equity, "events": events,
                    "bars": bars, "warnings": warnings, "connected": bool(client and env == cfg["environment"]),
                    "automation": bool(auto), "account_number": account,
                    "refreshed_at": datetime.now(PT).isoformat(timespec="seconds"),
                    "net_liquidation_value": number(balance, "net-liquidating-value", "net_liquidating_value"),
                    "cash": number(balance, "cash-balance", "cash_balance"),
                    "baseline_nlv": baseline_nlv,
                    "profit_since_start": (number(balance, "net-liquidating-value", "net_liquidating_value") - baseline_nlv) if baseline_nlv else 0.0}
        with self.lock:
            self.snapshot = snapshot
        return snapshot

    def _persist_equity(self, account, nlv, cash):
        now = datetime.now(PT).isoformat(timespec="seconds")
        with sqlite3.connect(self.db_path) as db:
            db.execute("INSERT INTO equity(account_key,timestamp,net_liquidating_value,cash) VALUES(?,?,?,?)", (account, now, nlv, cash))
            db.execute("INSERT OR IGNORE INTO baseline(account_key,nlv,started_at) VALUES(?,?,?)", (account, nlv, now))
            row=db.execute("SELECT nlv FROM baseline WHERE account_key=?",(account,)).fetchone()
            return float(row[0]) if row else float(nlv)

    def _broker_quotes(self, client, symbols):
        def status_code(error):
            import re
            match=re.search(r"\((\d{3})\)",str(error))
            return int(match.group(1)) if match else None
        def request(batch):
            last=None
            for attempt in range(3):
                try:
                    response=client._request("GET","/market-data/by-type",params={"equity":",".join(batch)})
                    data=payload(response)
                    rows=data if isinstance(data,list) else data.get("items",data.get("quotes",[])) if isinstance(data,dict) else []
                    return rows
                except Exception as exc:
                    last=exc; code=status_code(exc)
                    if code is not None and code!=429 and code<500: break
                    if attempt<2: time.sleep((0.5*(2**attempt))+(0.05*attempt))
            raise last
        try:
            rows=request(symbols)
        except Exception as exc:
            code=status_code(exc)
            if code==429 or (code is not None and code<500):
                raise
            rows=[]
            for start in range(0,len(symbols),5):
                try: rows.extend(request(symbols[start:start+5]))
                except Exception as batch_exc:
                    if status_code(batch_exc)==429: raise
        return {str(q.get("symbol","")).upper(): {**q,"quote-source":"tastytrade"}
                for q in rows if isinstance(q,dict) and q.get("symbol")}

    def _simulator_key(self):
        with sqlite3.connect(self.db_path) as db:
            row = db.execute("SELECT account_key FROM sandbox_sim_trades WHERE account_key LIKE 'Sandbox:%' ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                row=db.execute("SELECT account_key FROM sandbox_sim_positions WHERE account_key LIKE 'Sandbox:%' LIMIT 1").fetchone()
        return row[0] if row else "Sandbox:simulator"

    def _sim_positions(self, cfg):
        key = self._simulator_key()
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute("SELECT symbol,quantity,average_price,last_price,opened_at FROM sandbox_sim_positions WHERE account_key=?", (key,)).fetchall()
        return [{"symbol": s, "quantity": q, "average_price": a, "mark": p,
                 "unrealized_pnl": q*(p-a), "opened_at": t} for s,q,a,p,t in rows]

    def _simulator(self, cfg, signals, prices):
        key, now = self._simulator_key(), datetime.now(PT)
        with sqlite3.connect(self.db_path) as db:
            for symbol, price in prices.items():
                db.execute("UPDATE sandbox_sim_positions SET last_price=? WHERE account_key=? AND symbol=?", (price,key,symbol))
            day_open = now.astimezone(ET).replace(tzinfo=None).time()
            if now.weekday()<5 and wall_time(9,30)<=day_open<wall_time(16,0):
                for symbol,row in sorted(signals.items(),key=lambda kv:kv[1]["priority"],reverse=True):
                    side=row["signal"]
                    if side not in ("BUY","SELL") or symbol not in prices: continue
                    stamp=row["signal_time"]
                    if db.execute("SELECT 1 FROM sandbox_sim_trades WHERE account_key=? AND symbol=? AND signal_time=? AND signal=?",(key,symbol,stamp,side)).fetchone(): continue
                    position=db.execute("SELECT quantity,average_price FROM sandbox_sim_positions WHERE account_key=? AND symbol=?",(key,symbol)).fetchone()
                    q,a=map(float,position) if position else (0.0,0.0)
                    price=prices[symbol]; action="NO_TRADE"; realized=0.0; trade_qty=abs(q)
                    if q==0:
                        held=db.execute("SELECT quantity,average_price,last_price FROM sandbox_sim_positions WHERE account_key=?",(key,)).fetchall()
                        realized_before=db.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM sandbox_sim_trades WHERE account_key=?",(key,)).fetchone()[0]
                        adjustment_before=db.execute("SELECT COALESCE(SUM(amount),0) FROM sandbox_sim_equity_adjustments WHERE account_key=?",(key,)).fetchone()[0]
                        exposure=sum(abs(hq)*hmark for hq,havg,hmark in held)
                        unrealized_before=sum(hq*(hmark-havg) for hq,havg,hmark in held)
                        equity_before=max(0.0,float(cfg["initial_capital"])+float(realized_before)+unrealized_before+float(adjustment_before))
                        budget=min(equity_before*float(cfg["sim_allocation_pct"])/100,
                                   max(0.0,equity_before-exposure))
                        trade_qty=round(budget/price,6) if budget>0 else 0
                        if trade_qty:
                            q=trade_qty if side=="BUY" else -trade_qty
                            action="SIM_OPEN_LONG" if q>0 else "SIM_OPEN_SHORT"
                            db.execute("INSERT INTO sandbox_sim_positions VALUES(?,?,?,?,?,?)",(key,symbol,q,price,price,now.isoformat()))
                    elif (q>0 and side=="SELL") or (q<0 and side=="BUY"):
                        realized=q*(price-a); action="SIM_CLOSE_LONG" if q>0 else "SIM_CLOSE_SHORT"
                        db.execute("DELETE FROM sandbox_sim_positions WHERE account_key=? AND symbol=?",(key,symbol))
                    db.execute("INSERT OR IGNORE INTO sandbox_sim_trades(account_key,trading_day,symbol,signal_time,signal,action,quantity,price,realized_pnl,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(key,now.date().isoformat(),symbol,stamp,side,action,trade_qty,price,realized,now.isoformat()))
            rows=db.execute("SELECT quantity,average_price,last_price FROM sandbox_sim_positions WHERE account_key=?",(key,)).fetchall()
            realized=db.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM sandbox_sim_trades WHERE account_key=?",(key,)).fetchone()[0]
            adjustments=db.execute("SELECT COALESCE(SUM(amount),0) FROM sandbox_sim_equity_adjustments WHERE account_key=?",(key,)).fetchone()[0]
            pnl=float(realized)+sum(q*(mark-avg) for q,avg,mark in rows)
            equity=float(cfg["initial_capital"])+pnl+float(adjustments)
            db.execute("INSERT INTO sandbox_sim_equity(account_key,timestamp,equity) VALUES(?,?,?)",(key,now.isoformat(),equity))

    def _simulator_summary(self, cfg):
        key=self._simulator_key()
        with sqlite3.connect(self.db_path) as db:
            trades=db.execute("SELECT trading_day,symbol,signal,action,quantity,price,realized_pnl,created_at FROM sandbox_sim_trades WHERE account_key=? ORDER BY id DESC LIMIT 100",(key,)).fetchall()
            daily_history=db.execute("SELECT trading_day,ROUND(SUM(realized_pnl),2) FROM sandbox_sim_trades WHERE account_key=? GROUP BY trading_day ORDER BY trading_day DESC LIMIT 60",(key,)).fetchall()
            equity=db.execute("SELECT timestamp,equity FROM sandbox_sim_equity WHERE account_key=? ORDER BY timestamp",(key,)).fetchall()
            realized=db.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM sandbox_sim_trades WHERE account_key=?",(key,)).fetchone()[0]
            adjustments=db.execute("SELECT COALESCE(SUM(amount),0) FROM sandbox_sim_equity_adjustments WHERE account_key=?",(key,)).fetchone()[0]
            today=datetime.now(PT).date().isoformat()
            daily=db.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM sandbox_sim_trades WHERE account_key=? AND trading_day=?",(key,today)).fetchone()[0]
            open_rows=db.execute("SELECT quantity,average_price,last_price FROM sandbox_sim_positions WHERE account_key=?",(key,)).fetchall()
        total=float(realized); today=datetime.now(PT).date().isoformat()
        unrealized=sum(q*(mark-avg) for q,avg,mark in open_rows)
        return {"initial_capital":cfg["initial_capital"],"daily_profit":daily,"total_profit":total,
                "daily_profit_pct":daily/float(cfg["initial_capital"])*100,
                "total_profit_pct":total/float(cfg["initial_capital"])*100,
                "unrealized_profit":unrealized,"equity_value":float(cfg["initial_capital"])+total+unrealized+float(adjustments),
                "equity":[{"time":r[0],"value":r[1]} for r in equity[-1000:]],
                "daily":[{"day":r[0],"realized_pnl":r[1]} for r in daily_history],
                "trades":[{"day":r[0],"symbol":r[1],"signal":r[2],"action":r[3],"quantity":r[4],"price":r[5],"realized_pnl":r[6],"time":r[7]} for r in trades]}

    def _execute_orders(
        self,
        cfg,
        client,
        account,
        balance,
        positions,
        orders,
        signals,
        quotes,
        yahoo_prices,
        warnings,
        frames
    ):
        now = datetime.now(PT)
        account_key = f"Production:{account}"

        # =========================================================
        # 1. BUILD CURRENT POSITION QUANTITIES
        # =========================================================

        qty_by = {}

        for p in positions:
            symbol = str(
                p.get("underlying-symbol", p.get("symbol", ""))
            ).upper()

            if not symbol:
                continue

            quantity = number(p, "quantity")

            direction = str(
                p.get("quantity-direction", "Long")
            ).lower()

            if direction == "short":
                quantity *= -1

            qty_by[symbol] = qty_by.get(symbol, 0.0) + quantity

        portfolio = []

        for symbol, quantity in qty_by.items():
            mark = number(
                quotes.get(symbol, {}),
                "mark",
                "mid",
                "last",
                default=yahoo_prices.get(symbol, 0)
            )

            if quantity:
                portfolio.append((symbol, quantity, mark))

        # =========================================================
        # 2. ACCOUNT BALANCES
        # =========================================================

        nlv = number(
            balance,
            "net-liquidating-value",
            "net_liquidating_value"
        )

        cash = number(
            balance,
            "cash-balance",
            "cash_balance"
        )

        buying = number(
            balance,
            "equity-buying-power",
            "equity_buying_power",
            default=cash
        )

        # =========================================================
        # 3. IDENTIFY WORKING ORDERS
        # =========================================================

        active = set()
        working_orders = []

        for item in orders:
            order = (
                item.get("order", item)
                if isinstance(item, dict)
                else {}
            )

            if not isinstance(order, dict):
                continue

            status_value = str(
                order.get("status", "")
            ).lower()

            if status_value in TERMINAL_ORDER_STATES:
                continue

            legs = order.get("legs", []) or []

            symbols = [
                str(leg.get("symbol", "")).upper()
                for leg in legs
                if leg.get("symbol")
            ]

            if not symbols:
                continue

            active.update(symbols)

            source = str(
                order.get("source", "")
                or order.get("external-identifier", "")
                or order.get("external_identifier", "")
            )

            working_orders.append({
                "order": order,
                "symbols": symbols,
                "source": source
            })

        # =========================================================
        # 4. DAILY LOSS RESTRICTION
        # =========================================================

        with sqlite3.connect(self.db_path) as db:
            dayrows = db.execute(
                """
                SELECT net_liquidating_value
                FROM equity
                WHERE account_key=?
                AND timestamp LIKE ?
                ORDER BY timestamp
                """,
                (
                    account_key,
                    now.date().isoformat() + "%"
                )
            ).fetchall()

        dayloss = bool(
            dayrows
            and float(dayrows[0][0]) > 0
            and nlv <= float(dayrows[0][0]) * (
                1 - float(cfg["daily_loss_pct"]) / 100
            )
        )

        # =========================================================
        # 5. PRODUCTION END-OF-DAY CLOSEOUT
        #    12:45–1:00 PM PACIFIC
        # =========================================================

        if self.in_preclose_window(now):

            warnings.append(
                "Production EOD closeout active "
                "(12:45–1:00 PM Pacific)."
            )

            working_symbols = set()
            cancellation_failed = False

            # -----------------------------------------------------
            # Identify existing EOD close orders.
            # Do not cancel them or submit duplicate closes.
            # -----------------------------------------------------

            for item in working_orders:

                source = item["source"].lower()

                is_eod_order = (
                    "stocktrader/eod" in source
                )

                if is_eod_order:
                    working_symbols.update(item["symbols"])

            # -----------------------------------------------------
            # Cancel other working equity orders.
            # -----------------------------------------------------

            for item in working_orders:

                order = item["order"]
                symbols = item["symbols"]
                source = item["source"].lower()

                if "stocktrader/eod" in source:
                    continue

                order_id = str(
                    order.get(
                        "id",
                        order.get("order-id", "")
                    )
                )

                if not order_id:
                    warnings.append(
                        f"Cannot cancel working order for {symbols}: "
                        "broker order ID is missing."
                    )

                    cancellation_failed = True
                    working_symbols.update(symbols)
                    continue

                try:
                    client.cancel_order(account, order_id)

                    working_symbols.update(symbols)

                    warnings.append(
                        f"Cancellation requested for working order "
                        f"{order_id}."
                    )

                except Exception as exc:

                    cancellation_failed = True
                    working_symbols.update(symbols)

                    warnings.append(
                        f"Could not cancel working order "
                        f"{order_id}: {exc}"
                    )

            # Do not submit close orders until previous working
            # orders have been reconciled by a subsequent refresh.
            if working_symbols or cancellation_failed:

                warnings.append(
                    "EOD closeout waiting for existing orders "
                    "to clear: "
                    + ", ".join(sorted(working_symbols))
                )

                return

            # -----------------------------------------------------
            # Close every remaining position.
            # -----------------------------------------------------

            for symbol, quantity, mark in portfolio:

                if not quantity:
                    continue

                action = (
                    "Sell to Close"
                    if quantity > 0
                    else "Buy to Close"
                )

                qty = abs(quantity)

                fractional_qty = not float(qty).is_integer()

                try:

                    # ---------------------------------------------
                    # Fractional position
                    # ---------------------------------------------

                    if fractional_qty:

                        if mark <= 0:
                            raise ValueError(
                                "Fractional close requires a usable "
                                "market price."
                            )

                        definition = payload(
                            client.get_equity(symbol)
                        )

                        if not definition.get(
                            "is-fractional-quantity-eligible"
                        ):
                            raise ValueError(
                                f"Position quantity {qty:g} is fractional, "
                                "but the instrument does not support "
                                "fractional orders."
                            )

                        order = client.build_equity_order(
                            symbol,
                            action,
                            round(qty, 4),
                            mark,
                            source="StockTrader/EOD",
                            allow_fractional=True
                        )

                    # ---------------------------------------------
                    # Whole-share position
                    # ---------------------------------------------

                    else:

                        order = client.build_equity_market_close(
                            symbol,
                            action,
                            int(qty),
                            source="StockTrader/EOD"
                        )

                    # ---------------------------------------------
                    # Broker dry run
                    # ---------------------------------------------

                    dry = payload(
                        client.dry_run_order(account, order)
                    )

                    errors = (
                        dry.get("errors", [])
                        if isinstance(dry, dict)
                        else []
                    )

                    broker_warnings = (
                        dry.get("warnings", [])
                        if isinstance(dry, dict)
                        else []
                    )

                    if errors or broker_warnings:
                        raise TastytradeAPIError(
                            f"EOD dry-run blocked: "
                            f"{errors} {broker_warnings}"
                        )

                    # ---------------------------------------------
                    # Submit close order
                    # ---------------------------------------------

                    client.submit_order(
                        account,
                        order,
                        confirm_live=True,
                        max_order_notional=(
                            qty * mark if mark > 0 else None
                        ),
                        risk_reference_price=(
                            mark if mark > 0 else None
                        )
                    )

                    warnings.append(
                        f"EOD close submitted: {symbol}, "
                        f"{action}, quantity={qty:g}."
                    )

                except Exception as exc:

                    warnings.append(
                        f"EOD close {symbol} needs attention: {exc}"
                    )

            # Never process normal signals during EOD closeout.
            return

        # =========================================================
        # 6. REGULAR MARKET HOURS
        # =========================================================

        now_et = now.astimezone(ET)

        if not (
            now_et.weekday() < 5
            and wall_time(9, 30)
            <= now_et.time().replace(tzinfo=None)
            < wall_time(16, 0)
        ):
            return

        # =========================================================
        # 7. ORDER SIGNALS BY PRIORITY
        # =========================================================

        ordered = sorted(
            signals.values(),
            key=lambda row: (
                row["priority"],
                row.get("sentiment", {}).get("confidence", 0)
            ),
            reverse=True
        )

        for row in ordered:

            symbol = row["symbol"]
            side = row["signal"]
            stamp = row["signal_time"]

            if side not in ("BUY", "SELL"):
                continue

            # -----------------------------------------------------
            # Check previous signal event.
            # -----------------------------------------------------

            with sqlite3.connect(self.db_path) as db:

                previous = db.execute(
                    """
                    SELECT status, external_id
                    FROM signal_events
                    WHERE account_key=?
                    AND symbol=?
                    AND signal_time=?
                    AND side=?
                    """,
                    (
                        account_key,
                        symbol,
                        stamp,
                        side
                    )
                ).fetchone()

            if previous:

                previous_status = str(
                    previous[0] or ""
                )

                # Do not submit the same signal twice.
                # Submitted orders require broker reconciliation.
                if previous_status in (
                    "Submitted",
                    "Contrarian close submitted",
                    "Skipped",
                    "Filled"
                ):
                    continue

            # -----------------------------------------------------
            # Current position
            # -----------------------------------------------------

            q = qty_by.get(symbol, 0.0)

            last_side, loc = get_last_signal(frames[symbol])
            contrarian_exit = (
                (q > 0 and last_side == "SELL")
                or (q < 0 and last_side == "BUY")
            )
            print(f"Contrarian Exit for symbol: {symbol} {contrarian_exit}")

            same_direction = (
                (q > 0 and last_side == "BUY")
                or (q < 0 and last_side == "SELL")
            )

            qte = quotes.get(symbol, {})

            bid = number(qte, "bid")
            ask = number(qte, "ask")

            price = yahoo_prices.get(symbol, 0)

            source = "tastytrade"

            if bid <= 0 or ask <= 0:

                if price > 0:
                    bid = price
                    ask = price
                    source = "Yahoo delayed close"

            action = None
            qty = 0
            fractional = False

            status = "Observed · monitor only"
            details = "No order submitted."
            external = ""

            # -----------------------------------------------------
            # Get instrument fractional eligibility
            # -----------------------------------------------------

            try:

                instrument = payload(
                    client.get_equity(symbol)
                )

                fractional = bool(
                    instrument.get(
                        "is-fractional-quantity-eligible"
                    )
                )

            except Exception:
                pass

            # =====================================================
            # 8. PRIORITY 1: CONTRARIAN POSITION EXIT
            # =====================================================

            if contrarian_exit:

                if (
                    bid <= 0
                    or ask <= 0
                    or ask < bid
                ):

                    status = "Close blocked"

                    details = (
                        "No usable quote for contrarian exit."
                    )

                elif symbol in active:

                    status = "Close pending"

                    details = (
                        "An existing working order must clear first."
                    )

                else:

                    action = (
                        "Sell to Close"
                        if q > 0
                        else "Buy to Close"
                    )

                    qty = abs(q)

                    if fractional:

                        qty = round(qty, 4)

                    elif not float(qty).is_integer():

                        status = "Close blocked"

                        details = (
                            f"Position quantity {qty:g} is fractional, "
                            "but the instrument does not support "
                            "fractional orders."
                        )

                        qty = 0

                    else:

                        qty = int(qty)

                    if qty <= 0 and status != "Close blocked":

                        status = "Close blocked"

                        details = (
                            "Position quantity is below supported "
                            "closing precision."
                        )

            # =====================================================
            # 9. PRIORITY 2: PREVENT AUTOMATIC REVERSALS
            # =====================================================

            elif same_direction:

                status = "Skipped"

                details = (
                    "Existing position already matches signal direction."
                )

            # =====================================================
            # 10. PRIORITY 3: NORMAL ENTRY RESTRICTIONS
            # =====================================================

            else:

                if symbol in active:

                    status = "Blocked"

                    details = (
                        "Working broker order exists for symbol."
                    )

                elif not row.get("universe_ok", True):

                    status = "Blocked"

                    details = (
                        "Most Actives unavailable; new entries paused."
                    )

                elif dayloss:

                    status = "Blocked"

                    details = (
                        "Daily loss limit pauses new entries."
                    )

                elif (
                    bid <= 0
                    or ask <= 0
                    or ask < bid
                ):

                    status = "Blocked"

                    details = "No usable quote."

                elif (
                    source == "tastytrade"
                    and (
                        number(row, "close") <= 0
                        or abs(
                            (
                                ask if side == "BUY"
                                else bid
                            ) - number(row, "close")
                        ) / number(row, "close") * 100
                        > float(cfg["max_gap_pct"])
                    )
                ):

                    status = "Blocked"

                    details = (
                        "Broker quote is too far from "
                        "Yahoo signal close."
                    )

                else:

                    # ---------------------------------------------
                    # Long entry
                    # ---------------------------------------------

                    if side == "BUY":

                        long_exposure = sum(
                            abs(pq) * pm
                            for _, pq, pm in portfolio
                            if pq > 0
                        )

                        budget = min(
                            nlv * float(cfg["allocation_pct"]) / 100,
                            buying,
                            cash,
                            float(cfg["max_order_notional"]),
                            max(
                                0,
                                nlv * float(cfg["max_portfolio_pct"]) / 100
                                - long_exposure
                            )
                        )

                        qty = (
                            math.floor(budget / ask * 10000) / 10000
                            if fractional
                            else math.floor(budget / ask)
                        )

                        action = "Buy to Open"

                    # ---------------------------------------------
                    # Short entry
                    # ---------------------------------------------

                    else:

                        short_exposure = sum(
                            abs(pq) * pm
                            for _, pq, pm in portfolio
                            if pq < 0
                        )

                        budget = min(
                            nlv * float(cfg["allocation_pct"]) / 100,
                            buying,
                            float(cfg["max_order_notional"]),
                            max(
                                0,
                                nlv * float(cfg["max_portfolio_pct"]) / 100
                                - short_exposure
                            )
                        )

                        qty = math.floor(budget / bid)

                        action = "Sell to Open"

            # =====================================================
            # 11. SUBMIT ORDER
            # =====================================================

            if action and qty > 0:

                try:

                    limit = (
                        bid
                        if action.startswith("Sell")
                        else ask
                    )

                    order = client.build_equity_order(
                        symbol,
                        action,
                        qty,
                        round(limit, 2),
                        source=(
                            "StockTrader/ContrarianExit"
                            if contrarian_exit
                            else "StockTrader/2.0"
                        ),
                        allow_fractional=(
                            fractional
                            and action in (
                                "Buy to Open",
                                "Sell to Close",
                                "Buy to Close"
                            )
                        )
                    )

                    external = order["external-identifier"]

                    # ---------------------------------------------
                    # Broker dry run
                    # ---------------------------------------------

                    dry = payload(
                        client.dry_run_order(account, order)
                    )

                    errors = (
                        dry.get("errors", [])
                        if isinstance(dry, dict)
                        else []
                    )

                    broker_warnings = (
                        dry.get("warnings", [])
                        if isinstance(dry, dict)
                        else []
                    )

                    if errors or broker_warnings:

                        raise TastytradeAPIError(
                            f"Broker dry-run blocked: "
                            f"{errors} {broker_warnings}"
                        )

                    order_notional = qty * limit

                    # ---------------------------------------------
                    # Submit order
                    # ---------------------------------------------

                    client.submit_order(
                        account,
                        order,
                        confirm_live=True,
                        max_order_notional=(
                            order_notional
                            if contrarian_exit
                            else float(cfg["max_order_notional"])
                        ),
                        risk_reference_price=limit
                    )

                    status = (
                        "Contrarian close submitted"
                        if contrarian_exit
                        else "Submitted"
                    )

                    details = (
                        f"{action} {qty:g}; "
                        f"quote source: {source}"
                    )

                    # Prevent duplicate orders during this cycle.
                    active.add(symbol)

                    # Update in-memory position to prevent another
                    # signal from treating the position as still flat.
                    if contrarian_exit:
                        qty_by[symbol] = 0.0

                except Exception as exc:

                    status = "Order error / check broker"

                    details = str(exc)[:500]

            # =====================================================
            # 12. SAVE SUCCESSFUL OR TERMINAL SIGNAL EVENTS
            # =====================================================

            if status in (
                "Submitted",
                "Contrarian close submitted",
                "Skipped"
            ):

                with sqlite3.connect(self.db_path) as db:

                    db.execute(
                        """
                        INSERT OR REPLACE INTO signal_events
                        (
                            account_key,
                            symbol,
                            signal_time,
                            side,
                            status,
                            details,
                            external_id,
                            created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            account_key,
                            symbol,
                            stamp,
                            side,
                            status,
                            details,
                            external,
                            datetime.now(PT).isoformat(
                                timespec="seconds"
                            )
                        )
                    )

            else:

                warnings.append(
                    f"{symbol} {side}: {status} - {details}"
                )