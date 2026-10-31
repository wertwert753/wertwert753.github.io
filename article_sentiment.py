"""Yahoo Finance article retrieval and local FinBERT sentiment scoring."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
from functools import lru_cache
import sys

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer


def _first_text(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _nested_url(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _first_text(value.get("url"), value.get("href"))
    return ""


def _published_datetime(article, content):
    value = content.get("pubDate") or content.get("publishedAt") or content.get("providerPublishTime")
    if value is None:
        value = article.get("providerPublishTime") or article.get("pubDate") or article.get("publishedAt")
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None
    return None


def _normalize_article(symbol, item, cutoff):
    if not isinstance(item, dict):
        return None
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    title = _first_text(content.get("title"), item.get("title"))
    if not title:
        return None
    summary = _first_text(
        content.get("summary"), content.get("description"),
        item.get("summary"), item.get("description"), item.get("snippet"),
    )[:1200]
    published = _published_datetime(item, content)
    if published and published < cutoff:
        return None

    provider = content.get("provider") or item.get("provider") or {}
    if not isinstance(provider, dict):
        provider = {}
    publisher = _first_text(
        content.get("publisher"), item.get("publisher"),
        provider.get("displayName"), provider.get("name"), item.get("source"),
    )
    url = _nested_url(content.get("canonicalUrl")) or _nested_url(content.get("clickThroughUrl"))
    url = url or _nested_url(item.get("canonicalUrl")) or _nested_url(item.get("clickThroughUrl"))
    url = url or _first_text(item.get("link"), item.get("url"))
    article_key = _first_text(content.get("id"), item.get("id"), url)
    if not article_key:
        article_key = f"{title}|{published.isoformat() if published else ''}"
    article_id = hashlib.sha256(f"{symbol}|{article_key}".encode("utf-8")).hexdigest()[:20]
    return {
        "article_id": article_id,
        "symbol": symbol,
        "title": title[:500],
        "summary": summary,
        "publisher": publisher or "Unknown publisher",
        "url": url,
        "published_at": published.isoformat() if published else "Unknown",
        "_published_sort": published.timestamp() if published else 0,
    }


def _fetch_symbol_news(symbol, per_symbol, lookback_days):
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is required to retrieve article news.") from exc

    search = yf.Search(
        query=symbol,
        max_results=1,
        news_count=per_symbol,
        lists_count=0,
        include_cb=False,
        include_nav_links=False,
        include_research=False,
        include_cultural_assets=False,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(lookback_days))
    articles = []
    for item in search.news or []:
        article = _normalize_article(symbol, item, cutoff)
        if article:
            articles.append(article)
    return articles


def fetch_stock_articles(symbols, per_symbol=3, lookback_days=1, max_workers=6):
    """Fetch recent Yahoo Finance search results for each requested ticker."""
    symbols = list(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()))
    if not symbols:
        return [], []
    articles, errors = [], []
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), 8))) as executor:
        futures = {
            executor.submit(_fetch_symbol_news, symbol, per_symbol, lookback_days): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                articles.extend(future.result())
            except Exception as exc:
                errors.append(f"{symbol}: {str(exc)[:240]}")
    articles.sort(key=lambda article: article["_published_sort"], reverse=True)
    return articles, errors


FINBERT_MODEL = "ProsusAI/finbert"
_LABELS = {0: "Positive", 1: "Negative", 2: "Neutral"}


@lru_cache(maxsize=1)
def _load_finbert():
    tokenizer = AutoTokenizer.from_pretrained(FINBERT_MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(FINBERT_MODEL)
    model.eval()
    return torch, tokenizer, model


def score_articles(articles, batch_size=12):
    """Score article headline/summary pairs with the locally cached FinBERT model."""
    if not articles:
        return {}
    torch, tokenizer, model = _load_finbert()
    results = {}
    for start in range(0, len(articles), max(1, int(batch_size))):
        batch = articles[start:start + max(1, int(batch_size))]
        texts = [" ".join(part for part in (a["title"], a.get("summary", "")) if part) for a in batch]
        inputs = tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="pt")
        with torch.inference_mode():
            probabilities = torch.softmax(model(**inputs).logits, dim=-1).cpu().tolist()
        for article, probs in zip(batch, probabilities):
            by_label = {_LABELS[i]: float(prob) for i, prob in enumerate(probs)}
            sentiment = max(by_label, key=by_label.get)
            score = round((by_label["Positive"] - by_label["Negative"]) * 100)
            results[article["article_id"]] = {
                "sentiment": sentiment,
                "score": max(-100, min(100, score)),
                "confidence": round(by_label[sentiment] * 100),
                "rationale": (
                    f"FinBERT probabilities — positive {by_label['Positive']:.0%}, "
                    f"neutral {by_label['Neutral']:.0%}, negative {by_label['Negative']:.0%}."
                ),
            }
    return results
