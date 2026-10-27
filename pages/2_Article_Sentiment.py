from __future__ import annotations

import os
from pathlib import Path
import sys

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from article_sentiment import fetch_stock_articles, score_articles
from market_data import fetch_most_active_symbols
from observed_universe import load_observed_symbols
from sentiment_store import save_sentiment_snapshot


st.set_page_config(page_title="Article Sentiment", page_icon="📰", layout="wide")
st.title("Article Sentiment")
st.caption("LLM-measured tone in recent Yahoo Finance articles for the stocks on the dashboard's observed list.")


def configured_setting(name):
    value = os.getenv(name, "")
    if value:
        return value
    try:
        return str(st.secrets.get(name, ""))
    except Exception:
        return ""


@st.cache_data(ttl=300, show_spinner=False)
def observed_universe():
    symbols = load_observed_symbols(APP_DIR / "trading_dashboard.sqlite3")
    return symbols or fetch_most_active_symbols(50)


try:
    available_symbols = observed_universe()
except Exception as exc:
    st.error(f"Could not load the dashboard's observed stock list: {exc}")
    available_symbols = []

if not available_symbols:
    st.info("Open the main dashboard once to populate its observed stock list, or try again after Yahoo Finance is available.")
    st.stop()

with st.sidebar:
    st.header("Article analysis")
    selected_symbols = st.multiselect(
        "Stocks to analyze", available_symbols, default=available_symbols,
        help="Defaults to the same Most Actives and held-stock universe used by the main dashboard.",
    )
    article_count = st.slider("Recent articles per stock", min_value=1, max_value=8, value=3)
    lookback_days = st.slider("Lookback window (days)", min_value=1, max_value=30, value=7)
    ollama_url = st.text_input(
        "Local Ollama address", value=configured_setting("OLLAMA_HOST") or "http://localhost:11434",
        help="Ollama runs the model on this computer by default. The local service must be running.",
    ).strip()
    model = st.text_input(
        "Open model", value=configured_setting("OLLAMA_SENTIMENT_MODEL") or "qwen3:8b",
        help="Default: Qwen3 8B, an open-weight model served locally by Ollama.",
    ).strip()
    run_analysis = st.button(
        "Fetch articles and measure sentiment", type="primary", disabled=not selected_symbols,
    )
    st.caption("Analysis runs locally through Ollama and uses your computer's CPU/GPU. Recent measured scores prioritize matching HalfTrend signals; sentiment never changes signal direction.")

if run_analysis:
    articles = []
    fetch_errors = []
    scores = {}
    scoring_error = ""
    run_config = {
        "symbols": tuple(selected_symbols), "article_count": article_count,
        "lookback_days": lookback_days, "model": model, "ollama_url": ollama_url,
    }
    try:
        with st.spinner(f"Fetching recent articles for {len(selected_symbols)} stocks…"):
            articles, fetch_errors = fetch_stock_articles(
                selected_symbols, per_symbol=article_count, lookback_days=lookback_days,
            )
        st.session_state.article_sentiment_articles = articles
        st.session_state.article_sentiment_scores = scores
        st.session_state.article_sentiment_fetch_errors = fetch_errors
        st.session_state.article_sentiment_config = run_config
        if articles and ollama_url and model:
            try:
                with st.spinner(f"Measuring sentiment across {len(articles)} articles…"):
                    scores = score_articles(ollama_url, model, articles)
            except Exception as exc:
                scoring_error = f"Articles were fetched, but sentiment scoring failed: {exc}"
        elif articles:
            scoring_error = "Enter your Ollama address and model name to score these articles."
        st.session_state.article_sentiment_scores = scores
        st.session_state.article_sentiment_scoring_error = scoring_error
        if scores:
            try:
                saved_count = save_sentiment_snapshot(APP_DIR / "trading_dashboard.sqlite3", articles, scores)
                st.session_state.article_sentiment_saved_count = saved_count
            except Exception as exc:
                st.warning(f"Results are visible here but could not be saved for strategy ranking: {exc}")
    except Exception as exc:
        st.session_state.article_sentiment_articles = articles
        st.session_state.article_sentiment_scores = scores
        st.session_state.article_sentiment_fetch_errors = fetch_errors
        st.session_state.article_sentiment_scoring_error = ""
        st.session_state.article_sentiment_config = run_config
        st.error(f"Article sentiment run failed: {exc}")

articles = st.session_state.get("article_sentiment_articles", [])
scores = st.session_state.get("article_sentiment_scores", {})
fetch_errors = st.session_state.get("article_sentiment_fetch_errors", [])
scoring_error = st.session_state.get("article_sentiment_scoring_error", "")
run_config = st.session_state.get("article_sentiment_config")

if run_config:
    current_config = {
        "symbols": tuple(selected_symbols), "article_count": article_count,
        "lookback_days": lookback_days, "model": model, "ollama_url": ollama_url,
    }
    if run_config != current_config:
        st.info("The displayed results use previous settings. Click **Fetch articles and measure sentiment** to refresh them.")

if scoring_error:
    st.warning(scoring_error)
for error in fetch_errors:
    st.caption(f"Article fetch issue · {error}")

if articles:
    scored_articles = [
        {**article, **scores[article["article_id"]]}
        for article in articles if article["article_id"] in scores
    ]
    if scored_articles:
        table = pd.DataFrame(scored_articles)
        mean_score = float(table["score"].mean())
        positive_count = int((table["sentiment"] == "Positive").sum())
        negative_count = int((table["sentiment"] == "Negative").sum())
        metrics = st.columns(4)
        metrics[0].metric("Articles analyzed", f"{len(scored_articles)}")
        metrics[1].metric("Average sentiment", f"{mean_score:+.0f} / 100")
        metrics[2].metric("Positive articles", f"{positive_count}")
        metrics[3].metric("Negative articles", f"{negative_count}")

        by_stock = table.groupby("symbol", as_index=False).agg(
            score=("score", "mean"),
            articles=("article_id", "count"),
            confidence=("confidence", "mean"),
        )
        by_stock["sentiment"] = by_stock.score.map(
            lambda value: "Positive" if value >= 20 else ("Negative" if value <= -20 else "Neutral")
        )
        by_stock = by_stock.sort_values("score", ascending=False)
        colors = ["#168a54" if value >= 20 else "#c44343" if value <= -20 else "#7b8494"
                  for value in by_stock.score]
        figure = go.Figure(go.Bar(
            x=by_stock.symbol, y=by_stock.score, marker_color=colors,
            customdata=by_stock[["articles", "sentiment", "confidence"]].to_numpy(),
            hovertemplate=("%{x}<br>Score: %{y:.0f}<br>Articles: %{customdata[0]}<br>"
                           "Tone: %{customdata[1]}<br>Mean confidence: %{customdata[2]:.0f}%<extra></extra>"),
        ))
        figure.add_hline(y=0, line_color="#888", line_width=1)
        figure.update_layout(
            title="Average article sentiment by stock", height=390,
            yaxis={"title": "Negative ← sentiment score → Positive", "range": [-100, 100]},
            margin={"l": 15, "r": 15, "t": 45, "b": 20},
        )
        st.plotly_chart(figure, use_container_width=True)
        st.dataframe(
            by_stock[["symbol", "sentiment", "score", "articles", "confidence"]].rename(columns={
                "symbol": "Stock", "sentiment": "Measured tone", "score": "Average score",
                "articles": "Articles analyzed", "confidence": "Mean confidence (%)",
            }),
            use_container_width=True, hide_index=True,
        )

        st.subheader("Article-level measurements")
        for symbol in selected_symbols:
            stock_articles = [article for article in scored_articles if article["symbol"] == symbol]
            if not stock_articles:
                continue
            average = sum(article["score"] for article in stock_articles) / len(stock_articles)
            with st.expander(f"{symbol} · {len(stock_articles)} articles · average {average:+.0f}"):
                for article in stock_articles:
                    st.markdown(f"**{article['sentiment']} · {article['score']:+d} · confidence {article['confidence']}%**")
                    if article["url"]:
                        st.link_button(article["title"], article["url"])
                    else:
                        st.write(article["title"])
                    detail = " · ".join(part for part in (article["publisher"], article["published_at"]) if part)
                    st.caption(detail)
                    if article["summary"]:
                        st.write(article["summary"])
                    if article["rationale"]:
                        st.caption(f"Why: {article['rationale']}")
                    st.divider()
    else:
        st.info("Articles were fetched, but there are no LLM scores to show yet.")
elif run_config:
    st.info("No recent articles were returned for this selection and lookback window.")
else:
    st.info(f"The page is set to analyze {len(available_symbols)} stocks from the dashboard's observed list. Choose stocks and click the button to run an analysis.")

st.caption("Scores describe the tone expressed in each article's headline and available snippet. Recent, confident sentiment can prioritize an aligned HalfTrend order, but it does not create or reverse a signal by itself.")
