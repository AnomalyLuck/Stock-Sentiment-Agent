"""StockTwits source: the public symbol stream, keyless and default-on."""

import html
from datetime import datetime, timezone

import httpx

from .social_types import SocialPost


class StockTwitsSource:
    """Public StockTwits symbol stream; keyless but shallow history."""

    name = "stocktwits"
    enabled = True

    async def fetch(
        self, ticker: str, company: str, since: datetime
    ) -> list[SocialPost]:
        """Fetch the recent symbol stream and window-filter client-side."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
            )
            response.raise_for_status()
        posts: list[SocialPost] = []
        for message in response.json().get("messages") or []:
            raw_created = message.get("created_at")
            body = html.unescape((message.get("body") or "").strip())
            if not raw_created or not body:
                continue
            created = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < since:
                continue
            user = message.get("user") or {}
            username = user.get("username") or "unknown"
            like_data = message.get("likes") or {}
            reshare_data = message.get("reshares") or {}
            conversation = message.get("conversation") or {}
            likes = int(like_data.get("total") or 0)
            reshares = int(reshare_data.get("reshared_count") or 0)
            replies = int(conversation.get("replies") or 0)
            posts.append(
                SocialPost(
                    type="stocktwits",
                    title=body.splitlines()[0][:120],
                    text=body,
                    url=f"https://stocktwits.com/{username}/message/{message.get('id')}",
                    source="StockTwits",
                    author=username,
                    published_at=created,
                    likes=likes + reshares,
                    comments=replies,
                )
            )
        return posts
