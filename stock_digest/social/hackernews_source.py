"""Hacker News source via the free Algolia search API; keyless and default-on."""

import html
import re
from datetime import datetime, timezone

import httpx

from .aliases import search_term
from .social_types import SocialPost

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    """Drop HTML tags and unescape entities (HN comment bodies are HTML)."""
    text_without_tags = _TAG_RE.sub(" ", s)
    return html.unescape(text_without_tags).strip()


class HackerNewsSource:
    """Hacker News via the free Algolia search API; keyless and default-on."""

    name = "hackernews"
    enabled = True

    async def fetch(
        self, ticker: str, company: str, since: datetime
    ) -> list[SocialPost]:
        """Search stories and comments in the window via Algolia."""
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={
                    "query": await search_term(ticker, company),
                    "tags": "(story,comment)",
                    "numericFilters": (
                        f"created_at_i>{int(since.timestamp())},"
                        f"created_at_i<{now_epoch}"
                    ),
                    "hitsPerPage": 100,
                },
            )
            response.raise_for_status()
        posts: list[SocialPost] = []
        for hit in response.json().get("hits") or []:
            created_timestamp = hit.get("created_at_i")
            object_id = hit.get("objectID")
            if not created_timestamp or not object_id:
                continue
            created = datetime.fromtimestamp(int(created_timestamp), tz=timezone.utc)
            if created < since:
                continue
            title = (hit.get("title") or hit.get("story_title") or "").strip()
            text = strip_html(hit.get("comment_text") or hit.get("story_text") or "")
            if not title and not text:
                continue
            if not title:
                title = text.splitlines()[0][:120]
            posts.append(
                SocialPost(
                    type="hackernews",
                    title=title,
                    text=text,
                    url=f"https://news.ycombinator.com/item?id={object_id}",
                    source="Hacker News",
                    author=hit.get("author") or "unknown",
                    published_at=created,
                    likes=int(hit.get("points") or 0),
                    comments=int(hit.get("num_comments") or 0),
                )
            )
        return posts
