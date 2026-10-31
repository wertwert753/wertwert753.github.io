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
from strategy import crossover_signals
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
            symbols = fetch_most_active_symbols(50)
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
        for symbol, frame in ohlc.items():
            if len(frame) < max(10, int(cfg["amplitude"]) + 1):
                continue
            result = crossover_signals(frame, amplitude=int(cfg["amplitude"]),
                                       channel_deviation=float(cfg["channel_deviation"]))
            stamp = pd.Timestamp(frame.index[-1])
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
            row = result.iloc[-1]
            side = "BUY" if bool(row.buy_signal) else ("SELL" if bool(row.sell_signal) else "HOLD")
            close = float(frame.iloc[-1].Close)
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

        if production and auto and client and account:
            self._execute_orders(cfg, client, account, balance, positions, orders, signals, quote_map, yahoo_prices, warnings)

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

    def _execute_orders(self, cfg, client, account, balance, positions, orders, signals, quotes, yahoo_prices, warnings):
        now=datetime.now(PT)
        qty_by={}
        portfolio=[]
        for p in positions:
            sym=str(p.get("underlying-symbol",p.get("symbol",""))).upper()
            q=number(p,"quantity")*(-1 if str(p.get("quantity-direction","Long")).lower()=="short" else 1)
            qty_by[sym]=q
            mark=number(quotes.get(sym,{}),"mark","mid","last",default=yahoo_prices.get(sym,0))
            portfolio.append((sym,q,mark))
        nlv=number(balance,"net-liquidating-value","net_liquidating_value")
        cash=number(balance,"cash-balance","cash_balance")
        buying=number(balance,"equity-buying-power","equity_buying_power",default=cash)
        active=set()
        for item in orders:
            o=item.get("order",item) if isinstance(item,dict) else {}
            if str(o.get("status","")).lower() not in TERMINAL_ORDER_STATES:
                active.update(str(l.get("symbol","")).upper() for l in o.get("legs",[]))
        with sqlite3.connect(self.db_path) as db:
            dayrows=db.execute("SELECT net_liquidating_value FROM equity WHERE account_key=? AND timestamp LIKE ? ORDER BY timestamp",(f"Production:{account}",now.date().isoformat()+"%")).fetchall()
        dayloss=bool(dayrows and nlv<=float(dayrows[0][0])*(1-float(cfg["daily_loss_pct"])/100))
        if now.weekday()<5 and wall_time(12,45)<=now.time().replace(tzinfo=None)<wall_time(13,0):
            working = []
            for item in orders:
                order=item.get("order",item) if isinstance(item,dict) else {}
                if str(order.get("status","")).lower() in TERMINAL_ORDER_STATES: continue
                for leg in order.get("legs",[]) or []:
                    if str(leg.get("instrument-type","Equity")).lower() not in ("equity","stock"):
                        continue
                    symbol=str(leg.get("symbol","")).upper()
                    working.append(symbol)
                    try: client.cancel_order(account,str(order.get("id",order.get("order-id",""))))
                    except Exception as exc: warnings.append(f"Cancel {symbol}: {exc}")
            if working:
                warnings.append("Production closeout is waiting for working-order cancellations: " + ", ".join(sorted(set(working))))
                return
            for symbol,q,mark in portfolio:
                if not q or mark<=0: continue
                action="Sell to Close" if q>0 else "Buy to Close"
                qty=abs(q)
                try:
                    if not float(qty).is_integer():
                        if q < 0:
                            raise ValueError("Fractional short positions cannot be closed by this strategy.")
                        definition=payload(client.get_equity(symbol))
                        if not definition.get("is-fractional-quantity-eligible"):
                            raise ValueError("The broker does not mark this stock eligible for fractional closing.")
                        order=client.build_equity_order(symbol,action,round(qty,4),mark,
                                                        source="StockTrader/EOD",allow_fractional=True)
                    else:
                        order=client.build_equity_market_close(symbol,action,int(qty),source="StockTrader/EOD")
                    dry=payload(client.dry_run_order(account,order)); errs=dry.get("errors",[]) if isinstance(dry,dict) else []
                    warns=dry.get("warnings",[]) if isinstance(dry,dict) else []
                    if errs or warns: raise TastytradeAPIError(f"EOD dry-run blocked: {errs} {warns}")
                    client.submit_order(account,order,confirm_live=True,max_order_notional=qty*mark,risk_reference_price=mark)
                except Exception as exc: warnings.append(f"EOD close {symbol} needs attention: {exc}")
            return
        if not (now.weekday()<5 and wall_time(9,30)<=now.astimezone(ET).time().replace(tzinfo=None)<wall_time(16,0)):
            return
        ordered=sorted(signals.values(),key=lambda r:(r["priority"],r.get("sentiment",{}).get("confidence",0)),reverse=True)
        for row in ordered:
            symbol,side,stamp=row["symbol"],row["signal"],row["signal_time"]
            if side not in ("BUY","SELL"): continue
            with sqlite3.connect(self.db_path) as db:
                if db.execute("SELECT 1 FROM signal_events WHERE account_key=? AND symbol=? AND signal_time=? AND side=?",(f"Production:{account}",symbol,stamp,side)).fetchone(): continue
            status,details="Observed · monitor only","No order submitted."; external=""
            qte=quotes.get(symbol,{})
            bid,ask=number(qte,"bid"),number(qte,"ask")
            price=yahoo_prices.get(symbol,0)
            source="tastytrade"
            if not bid or not ask:
                if price>0: bid=ask=price; source="Yahoo delayed close"
            if symbol in active: status,details="Blocked","Working broker order exists for symbol."
            elif not row.get("universe_ok",True) and not qty_by.get(symbol): status,details="Blocked","Most Actives did not refresh; new automatic entries are paused."
            elif dayloss and not qty_by.get(symbol): status,details="Blocked","Daily loss limit pauses new entries."
            elif bid<=0 or ask<=0 or ask<bid: status,details="Blocked","No usable quote."
            elif not source.startswith("Yahoo") and abs((ask if side=="BUY" else bid)-row["close"])/row["close"]*100>float(cfg["max_gap_pct"]): status,details="Blocked","Broker quote is too far from the Yahoo signal close."
            else:
                q=qty_by.get(symbol,0); action=None; qty=0; fractional=False
                try:
                    instrument=payload(client.get_equity(symbol)); fractional=bool(instrument.get("is-fractional-quantity-eligible"))
                except Exception: pass
                if side=="BUY":
                    if q>0: status,details="Skipped","Long position already exists."
                    elif q<0: action,qty="Buy to Close",math.floor(abs(q))
                    else:
                        exposure=sum(abs(pq)*pm for _,pq,pm in portfolio if pq>0)
                        budget=min(nlv*float(cfg["allocation_pct"])/100,buying,cash,float(cfg["max_order_notional"]),max(0,nlv*float(cfg["max_portfolio_pct"])/100-exposure))
                        qty=math.floor(budget/ask) if not fractional else math.floor(budget/ask*10000)/10000
                        action="Buy to Open"
                else:
                    if q<0: status,details="Skipped","Short position already exists."
                    elif q>0: action,qty="Sell to Close",round(q,4) if fractional else math.floor(q)
                    else:
                        short_exposure=sum(abs(pq)*pm for _,pq,pm in portfolio if pq<0)
                        budget=min(nlv*float(cfg["allocation_pct"])/100,buying,float(cfg["max_order_notional"]),max(0,nlv*float(cfg["max_portfolio_pct"])/100-short_exposure))
                        qty=math.floor(budget/bid); action="Sell to Open"
                if action and qty>0:
                    try:
                        limit=ask if side=="BUY" else bid
                        order=client.build_equity_order(symbol,action,qty,round(limit,2),source="StockTrader/2.0",
                                allow_fractional=fractional and action in ("Buy to Open","Sell to Close"))
                        external=order["external-identifier"]
                        dry=payload(client.dry_run_order(account,order)); errs=dry.get("errors",[]) if isinstance(dry,dict) else []; warns=dry.get("warnings",[]) if isinstance(dry,dict) else []
                        if errs or warns: raise TastytradeAPIError(f"Broker dry-run blocked: {errs} {warns}")
                        client.submit_order(account,order,confirm_live=True,max_order_notional=float(cfg["max_order_notional"]),risk_reference_price=limit)
                        status,details="Submitted",f"{action} {qty:g}; quote source: {source}"
                        active.add(symbol)
                    except Exception as exc: status,details="Order error / check broker",str(exc)[:500]
            with sqlite3.connect(self.db_path) as db:
                db.execute("INSERT OR REPLACE INTO signal_events(account_key,symbol,signal_time,side,status,details,external_id,created_at) VALUES(?,?,?,?,?,?,?,?)",(f"Production:{account}",symbol,stamp,side,status,details,external,datetime.now(PT).isoformat(timespec="seconds")))
