"""Social Sentiment: social posts for one ticker over the last 48 hours.

Ported from github.com/AnomalyLuck/Sentiment-Search, branch feature/x-social-sources,
commit c42585297226030962a1b7c9fef75110ab76d01f (`app/` social modules behind its
`/api/social` endpoint). Module names match the source so the two can be diffed.
Paths such as `tests/fixtures/...` in comments refer to that repository.

`retrieval.py` is the source file in full (one added "48h" window); its `fetch_news` is
also the digest's and material news' only news source, through `stock_digest/news.py`.
"""
