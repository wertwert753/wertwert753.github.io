from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import ipaddress
import os
from urllib.parse import urlsplit
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from trading_service import TradingService

APP_DIR = Path(__file__).resolve().parent
service = TradingService(APP_DIR / "trading_dashboard.sqlite3")
refresh_lock = asyncio.Lock()


async def refresh_worker():
    while True:
        try:
            async with refresh_lock:
                await asyncio.to_thread(service.refresh)
        except Exception as exc:
            service.last_error = f"Refresh failed: {exc}"
        try:
            from sentiment_scheduler import run_due_sentiment
            await asyncio.to_thread(run_due_sentiment, APP_DIR / "trading_dashboard.sqlite3")
        except Exception as exc:
            service.last_error = f"Scheduled sentiment analysis failed: {exc}"
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(refresh_worker())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="StockTrader", docs_url=None, redoc_url=None, lifespan=lifespan)


def is_local_request(request: Request):
    peer = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


@app.middleware("http")
async def local_network(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.netloc != request.headers.get("host") or parsed.scheme != request.url.scheme:
                return JSONResponse({"detail": "Cross-site requests are not accepted."}, status_code=403)
    return await call_next(request)


class Controls(BaseModel):
    settings: dict = Field(default_factory=dict)
    action: str = "refresh"
    arm: bool = False
    production_ack: bool = False
    live_phrase: str = ""
    virtual_equity: float | None = None


class SentimentRequest(BaseModel):
    symbols: list[str]
    per_symbol: int = 3
    lookback_days: int = 1


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse((APP_DIR / "stocktrader.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@app.get("/api/state")
def get_state(request: Request):
    return {**service.snapshot, "last_error": service.last_error,
            "access_mode": "local" if is_local_request(request) else "view-only"}


@app.post("/api/settings")
async def update_settings(request: Controls, _local=Depends(is_local_request)):
    if not _local:
        raise HTTPException(403, "Settings can only be changed on the StockTrader host.")
    async with refresh_lock:
        previous_environment = service.settings["environment"]
        service.configure(request.settings)
        if service.settings["environment"] != previous_environment:
            snapshot = await asyncio.to_thread(service.refresh)
            snapshot.environment
            return {"ok": True, "snapshot": snapshot}
    return {"ok": True}


@app.post("/api/controls")
async def controls(request: Controls, _local=Depends(is_local_request)):
    if not _local:
        raise HTTPException(403, "Trading controls are available only on the StockTrader host.")
    try:
        async with refresh_lock:
            service.configure(request.settings)
            if request.action == "connect":
                result = await asyncio.to_thread(service.connect, service.settings["environment"])
            elif request.action == "set_virtual_equity":
                if request.virtual_equity is None:
                    raise HTTPException(400, "Enter a virtual equity amount.")
                await asyncio.to_thread(service.set_virtual_equity, request.virtual_equity)
                result = {"virtual_equity": request.virtual_equity}
            elif request.action in {"start", "stop"}:
                service.automate(request.action, request.arm, request.production_ack, request.live_phrase)
                result = {"automation": service.auto_enabled}
            elif request.action != "refresh":
                raise HTTPException(400, "Unknown action.")
            else:
                result = None
            snapshot = await asyncio.to_thread(service.refresh)
        return {"result": result, "snapshot": snapshot}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/sentiment/universe")
def sentiment_universe():
    from market_data import fetch_most_active_symbols
    from observed_universe import load_observed_symbols
    symbols = load_observed_symbols(APP_DIR / "trading_dashboard.sqlite3")
    if not symbols:
        try:
            symbols = fetch_most_active_symbols()
        except Exception as exc:
            raise HTTPException(503, f"Could not load the observed stocks: {exc}") from exc
    return {"symbols": symbols}


@app.post("/api/sentiment")
def sentiment(request: SentimentRequest, _local=Depends(is_local_request)):
    if not _local:
        raise HTTPException(403, "Sentiment runs are available only on the StockTrader host.")
    from article_sentiment import fetch_stock_articles, score_articles
    from sentiment_store import save_sentiment_snapshot
    symbols = list(dict.fromkeys(s.strip().upper() for s in request.symbols if s.strip()))
    if not symbols:
        raise HTTPException(400, "Choose at least one stock.")
    try:
        articles, errors = fetch_stock_articles(symbols, per_symbol=max(1, min(8, request.per_symbol)),
                                                lookback_days=max(1, min(30, request.lookback_days)))
    except Exception as exc:
        raise HTTPException(502, f"Yahoo article fetch failed: {exc}") from exc
    scores, scoring_error = {}, ""
    if articles:
        try:
            scores = score_articles(articles)
        except Exception as exc:
            scoring_error = str(exc)
    saved = save_sentiment_snapshot(APP_DIR / "trading_dashboard.sqlite3", articles, scores) if scores else 0
    grouped={}
    for article in articles:
        score=scores.get(article["article_id"])
        if score: grouped.setdefault(article["symbol"],[]).append(score)
    summary=[]
    for symbol, values in grouped.items():
        mean=sum(v["score"] for v in values)/len(values)
        confidence=sum(v["confidence"] for v in values)/len(values)
        summary.append({"symbol":symbol,"average_score":round(mean,1),
                        "mean_confidence":round(confidence,1),"articles":len(values),
                        "tone":"Positive" if mean>=20 else "Negative" if mean<=-20 else "Neutral"})
    return {"articles": [{**article, **scores.get(article["article_id"], {})} for article in articles],
            "summary":summary,"fetch_errors": errors,"scoring_error":scoring_error,"saved_symbols": saved,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("stocktrader:app", host=os.getenv("STOCKTRADER_HOST", "0.0.0.0"),
                port=int(os.getenv("STOCKTRADER_PORT", "8765")), reload=False)
