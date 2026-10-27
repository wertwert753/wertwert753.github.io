"""Persistence and ranking helpers for measured stock article sentiment."""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3


def save_sentiment_snapshot(db_path, articles, scores):
    """Persist per-symbol confidence-weighted sentiment from successfully scored articles."""
    grouped = {}
    for article in articles:
        result = scores.get(article.get("article_id"))
        if not result:
            continue
        symbol = str(article.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        score = max(-100.0, min(100.0, float(result["score"])))
        confidence = max(0.0, min(100.0, float(result["confidence"])))
        grouped.setdefault(symbol, []).append((score, confidence))
    if not grouped:
        return 0

    measured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS stock_sentiment (
            symbol TEXT PRIMARY KEY, score REAL NOT NULL, confidence REAL NOT NULL,
            article_count INTEGER NOT NULL, measured_at TEXT NOT NULL)""")
        for symbol, values in grouped.items():
            weight_sum = sum(confidence for _, confidence in values)
            score = (sum(score * confidence for score, confidence in values) / weight_sum
                     if weight_sum else sum(score for score, _ in values) / len(values))
            confidence = sum(value[1] for value in values) / len(values)
            db.execute("""INSERT INTO stock_sentiment(symbol,score,confidence,article_count,measured_at)
                VALUES(?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET
                score=excluded.score, confidence=excluded.confidence,
                article_count=excluded.article_count, measured_at=excluded.measured_at""",
                (symbol, score, confidence, len(values), measured_at))
    return len(grouped)


def load_sentiment_snapshot(db_path, max_age_hours=24):
    """Load recent scores; stale snapshots do not affect order priority."""
    try:
        with sqlite3.connect(db_path) as db:
            rows = db.execute(
                "SELECT symbol,score,confidence,article_count,measured_at FROM stock_sentiment"
            ).fetchall()
    except sqlite3.OperationalError:
        return {}

    now = datetime.now(timezone.utc)
    result = {}
    for symbol, score, confidence, count, measured_at in rows:
        try:
            stamp = datetime.fromisoformat(measured_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age_hours = (now - stamp.astimezone(timezone.utc)).total_seconds() / 3600
            if 0 <= age_hours <= max_age_hours:
                result[symbol] = {
                    "score": float(score), "confidence": float(confidence),
                    "article_count": int(count), "measured_at": measured_at,
                }
        except (TypeError, ValueError):
            continue
    return result


def sentiment_priority(signal, sentiment):
    """Return a sort key that ranks HalfTrend-aligned conviction ahead of other signals."""
    signal = str(signal).upper()
    if not sentiment or signal not in {"BUY", "SELL"}:
        return (0, 0.0, 0.0)
    score = float(sentiment.get("score", 0.0))
    confidence = max(0.0, min(100.0, float(sentiment.get("confidence", 0.0))))
    aligned = (signal == "BUY" and score >= 20) or (signal == "SELL" and score <= -20)
    if not aligned:
        return (0, 0.0, confidence)
    return (1, abs(score) * confidence, confidence)
