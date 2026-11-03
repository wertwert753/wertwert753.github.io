"""Weekday premarket and hourly FinBERT article analysis schedule."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import sqlite3
from zoneinfo import ZoneInfo

from article_sentiment import fetch_stock_articles, score_articles
from sentiment_store import save_sentiment_snapshot

PACIFIC = ZoneInfo("America/Los_Angeles")


def _easter_sunday(year):
    """Gregorian Easter date, used to skip the Good Friday market closure."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(day):
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year, month, weekday, occurrence):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (occurrence - 1))


def _last_weekday(year, month, weekday):
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    last = next_month - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _is_us_market_holiday(day):
    year = day.year
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4), date(year, 12, 25)]
    observed = {_observed(holiday) for holiday in fixed}
    holidays = observed | {
        _nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3),
        _last_weekday(year, 5, 0), _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4), _easter_sunday(year) - timedelta(days=2),
    }
    # New Year's Day observed on December 31 can fall in the prior calendar year.
    if day == date(year, 12, 31) and date(year + 1, 1, 1).weekday() == 5:
        return True
    return day in holidays


def _schedule_db(db_path):
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS sentiment_schedule (schedule_key TEXT PRIMARY KEY, completed_at TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS sentiment_schedule_articles (schedule_date TEXT NOT NULL, article_id TEXT NOT NULL, PRIMARY KEY(schedule_date, article_id))")


def run_due_sentiment(db_path, now=None):
    """Run the current weekday slot once; call this from the app background loop."""
    now = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
    if now.weekday() >= 5 or _is_us_market_holiday(now.date()):
        return None
    if now.hour == 6:
        slot, article_count, premarket = "premarket", 3, True
    elif 7 <= now.hour <= 12:
        slot, article_count, premarket = f"hour-{now.hour:02d}", 1, False
    else:
        return None

    date_key = now.date().isoformat()
    if 6 <= now.hour <= 12:
        _schedule_db(db_path)
        with sqlite3.connect(db_path) as db:
            premarket_state = db.execute(
                "SELECT completed_at FROM sentiment_schedule WHERE schedule_key=?",
                (f"{date_key}:premarket",),
            ).fetchone()
        if not premarket_state:
            # Catch up the full daily article set if StockTrader starts after 6 a.m.
            slot, article_count, premarket = "premarket", 3, True
    schedule_key = f"{date_key}:{slot}"
    _schedule_db(db_path)
    attempt_time = datetime.now(timezone.utc)
    with sqlite3.connect(db_path) as db:
        previous = db.execute("SELECT completed_at FROM sentiment_schedule WHERE schedule_key=?", (schedule_key,)).fetchone()
        if previous:
            if previous[0].startswith("DONE|"):
                return None
            previous_time = datetime.fromisoformat(previous[0].removeprefix("RUN|"))
            if (attempt_time - previous_time).total_seconds() < 900:
                return None
            db.execute("UPDATE sentiment_schedule SET completed_at=? WHERE schedule_key=?",
                       ("RUN|" + attempt_time.isoformat(timespec="seconds"), schedule_key))
        else:
            # Persist before inference so restarts cannot immediately duplicate a batch.
            db.execute("INSERT INTO sentiment_schedule(schedule_key, completed_at) VALUES (?, ?)",
                       (schedule_key, "RUN|" + attempt_time.isoformat(timespec="seconds")))
        known_ids = set() if premarket else {row[0] for row in db.execute(
            "SELECT article_id FROM sentiment_schedule_articles WHERE schedule_date=?", (date_key,)
        )}

    from market_data import fetch_most_active_symbols
    from observed_universe import load_observed_symbols
    symbols = load_observed_symbols(db_path) or fetch_most_active_symbols(100)
    articles, fetch_errors = fetch_stock_articles(symbols, per_symbol=article_count, lookback_days=1)
    if not premarket:
        # Yahoo's first result for each ticker is the hourly headline considered.
        first_by_symbol = {}
        for article in articles:
            first_by_symbol.setdefault(article["symbol"], article)
        articles = [article for article in first_by_symbol.values() if article["article_id"] not in known_ids]
    if not articles:
        if fetch_errors:
            raise RuntimeError("; ".join(fetch_errors[:5]))
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE sentiment_schedule SET completed_at=? WHERE schedule_key=?",
                       ("DONE|" + attempt_time.isoformat(timespec="seconds"), schedule_key))
        return {"slot": slot, "articles": 0}

    scores = score_articles(articles)
    save_sentiment_snapshot(db_path, articles, scores)
    with sqlite3.connect(db_path) as db:
        db.executemany(
            "INSERT OR IGNORE INTO sentiment_schedule_articles(schedule_date, article_id) VALUES (?, ?)",
            ((date_key, article["article_id"]) for article in articles if article["article_id"] in scores),
        )
        db.execute("UPDATE sentiment_schedule SET completed_at=? WHERE schedule_key=?",
                   ("DONE|" + datetime.now(timezone.utc).isoformat(timespec="seconds"), schedule_key))
    return {"slot": slot, "articles": len(scores)}
