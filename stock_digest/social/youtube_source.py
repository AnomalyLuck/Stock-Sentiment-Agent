"""YouTube source via the Data API v3: window-bounded search, statistics,
top comments, plus the quota budget and language/engagement filters."""

import asyncio
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from . import config
from .aliases import search_term
from .social_types import PostComment, SocialPost, SourceError

# search.list draws from a dedicated ~100-calls/day bucket that resets at
# midnight Pacific; leave headroom and track it ourselves (Google returns no
# remaining-quota header).
YOUTUBE_DAILY_SEARCH_BUDGET = 90
_yt_budget_day: date | None = None
_yt_searches_used = 0


def _yt_take_search_budget() -> bool:
    """Consume one search call from today's Pacific-day budget; False if spent."""
    global _yt_budget_day, _yt_searches_used
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    if _yt_budget_day != today:
        _yt_budget_day = today
        _yt_searches_used = 0
    if _yt_searches_used >= YOUTUBE_DAILY_SEARCH_BUDGET:
        return False
    _yt_searches_used += 1
    return True


def _yt_exhaust_search_budget() -> None:
    """Mark today's budget spent (called on a quotaExceeded response)."""
    global _yt_budget_day, _yt_searches_used
    _yt_budget_day = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    _yt_searches_used = YOUTUBE_DAILY_SEARCH_BUDGET


# search.list `relevanceLanguage` only biases ranking, so non-English videos
# still come back; these decide it. Scripts that never carry English text:
_NON_LATIN_SCRIPT = re.compile(
    "["
    "Ѐ-ӿ"  # Cyrillic
    "֐-׿"  # Hebrew
    "؀-ۿݐ-ݿ"  # Arabic
    "ऀ-෿"  # Devanagari through Sinhala (Indic scripts)
    "฀-๿"  # Thai
    "ᄀ-ᇿ"  # Hangul Jamo
    "぀-ヿ"  # Hiragana + Katakana
    "㄰-㆏"  # Hangul Compatibility Jamo
    "㐀-鿿"  # CJK ideographs
    "가-힯"  # Hangul syllables
    "豈-﫿"  # CJK compatibility ideographs
    "]"
)
_INVERTED_PUNCT = re.compile("[¿¡]")  # Spanish-only, decisive on its own

# Finance words that belong to exactly one non-English language and never turn
# up in an English stock title; one is enough to reject ("Nvidia Aktie").
_STRONG_MARKERS = frozenset(
    {
        "aandelen",
        "acciones",
        "ações",
        "acoes",
        "akcje",
        "aktie",
        "aktien",
        "aktienkurs",
        "análisis",
        "analisis",
        "analizi",
        "azioni",
        "bolsa",
        "borsa",
        "bourse",
        "hisse",
        "khoán",
        "phiếu",
        "saham",
    }
)

# Function words common in one non-English language and rare in English titles.
# Weaker signals, so two are needed. Deliberately excludes words English shares
# ("per share", "no", "on") to keep false positives off.
_WEAK_MARKERS = frozenset(
    {
        # Spanish
        "ahora",
        "comprar",
        "cómo",
        "está",
        "hoy",
        "las",
        "los",
        "más",
        "mercado",
        "para",
        "por",
        "qué",
        "vender",
        # Portuguese
        "agora",
        "como",
        "hoje",
        "não",
        "nao",
        "você",
        "voce",
        # French
        "actualité",
        "c'est",
        "des",
        "les",
        "pour",
        "pourquoi",
        # Spanish/French/Italian/Portuguese shared
        "che",
        "con",
        "de",
        "del",
        "della",
        "el",
        "il",
        "la",
        "que",
        # German
        "das",
        "der",
        "die",
        "für",
        "jetzt",
        "kaufen",
        "mit",
        "und",
        "warum",
        # Indonesian / Malay
        "adalah",
        "dan",
        "ini",
        "untuk",
        "yang",
        # Turkish
        "için",
        "icin",
        "nasıl",
        "nasil",
        "senedi",
        # Vietnamese
        "chứng",
        "cổ",
        "của",
        "và",
        # Dutch / Polish
        "het",
        "jest",
        "voor",
    }
)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


# The one search page comes back padded with tiny uploads — a few hundred views
# and no discussion — that crowd real coverage out of the per-source cap.
# Views carry the quality bar; the comment floor only screens out videos with
# discussion switched off. Held at 50 it was the sole binding filter (it alone
# cut a good page from ~32 videos to 10), since comment counts run well under
# 1% of views even on heavily-watched finance uploads.
YOUTUBE_MIN_VIEWS = 10000
YOUTUBE_MIN_COMMENTS = 20


def meets_youtube_engagement(views: int, comments: int) -> bool:
    """Whether a video clears both engagement floors (views and comments)."""
    return views >= YOUTUBE_MIN_VIEWS and comments >= YOUTUBE_MIN_COMMENTS


# Top comments ride along on the highest-engagement videos. commentThreads.list
# costs 1 quota unit per video (vs 100 for a search page), so the cap below is
# about signal, not quota: a handful of comments, not a comment feed.
YOUTUBE_COMMENTS_PER_VIDEO = 3
YOUTUBE_COMMENTS_MIN_PER_VIDEO = 2  # one lone comment isn't worth the row
YOUTUBE_COMMENT_TOTAL_CAP = 10


def comment_slots(video_count: int) -> list[int]:
    """How many comments each video may carry under the global cap, in order."""
    slots: list[int] = []
    remaining = YOUTUBE_COMMENT_TOTAL_CAP
    for _ in range(video_count):
        take = min(YOUTUBE_COMMENTS_PER_VIDEO, remaining)
        if take < YOUTUBE_COMMENTS_MIN_PER_VIDEO:
            break
        slots.append(take)
        remaining -= take
    return slots


def is_english_video(title: str, lang: str | None) -> bool:
    """Whether a YouTube video reads as English; declared language wins if set.

    Judged on the title alone: descriptions routinely mix languages (foreign
    hashtags, translated blurbs) on otherwise English videos.
    """
    if lang:
        return lang.lower().startswith("en")
    if _NON_LATIN_SCRIPT.search(title) or _INVERTED_PUNCT.search(title):
        return False
    # Score 2 rejects, so a single borrowed weak word ("El Paso", "die") doesn't.
    score = 0
    for word in _WORD.findall(title.lower()):
        if word in _STRONG_MARKERS:
            score += 2
        elif word in _WEAK_MARKERS:
            score += 1
    return score < 2


def _popularity(post: SocialPost) -> int:
    """Same signal as social.popularity_score; local so this module stays a leaf."""
    return post.likes + 2 * post.comments


def _video_post(video: dict, since: datetime) -> SocialPost | None:
    """Convert a video to a post if it passes the time, language, and activity filters."""
    snippet = video.get("snippet") or {}
    statistics = video.get("statistics") or {}
    raw_published = snippet.get("publishedAt")
    video_id = video.get("id")
    if not raw_published or not video_id:
        return None

    published_at = datetime.fromisoformat(raw_published.replace("Z", "+00:00"))
    if published_at < since:
        return None
    title = (snippet.get("title") or "").strip()
    language = snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage")
    if not is_english_video(title, language):
        return None

    # Hidden likes and disabled comments come back without a count.
    views = int(statistics.get("viewCount") or 0)
    comments = int(statistics.get("commentCount") or 0)
    if not meets_youtube_engagement(views, comments):
        return None

    return SocialPost(
        type="youtube",
        title=title,
        text=(snippet.get("description") or "").strip(),
        url=f"https://www.youtube.com/watch?v={video_id}",
        source="YouTube",
        author=(snippet.get("channelTitle") or "unknown").strip(),
        published_at=published_at,
        likes=int(statistics.get("likeCount") or 0),
        comments=comments,
        views=views,  # display only
    )


class YouTubeSource:
    """YouTube Data API v3; optional, activates when YOUTUBE_API_KEY is set."""

    name = "youtube"

    _SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
    _VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
    _COMMENTS_URL = "https://www.googleapis.com/youtube/v3/commentThreads"

    def __init__(self) -> None:
        self.enabled = bool(config.YOUTUBE_API_KEY)

    async def _get(
        self, client: httpx.AsyncClient, url: str, params: dict
    ) -> httpx.Response:
        """GET with 429 backoff-and-retry; 403 quotaExceeded spends the budget."""
        for attempt in range(3):
            response = await client.get(url, params=params)
            if response.status_code == 429:  # per-minute rate limit: back off, retry
                await asyncio.sleep(1 + attempt)
                continue
            if response.status_code == 403:
                try:
                    reason = response.json()["error"]["errors"][0]["reason"]
                except (KeyError, IndexError, ValueError):
                    reason = ""
                if reason == "quotaExceeded":  # daily bucket: no point retrying
                    _yt_exhaust_search_budget()
                    raise SourceError("YouTube quota reached for today")
            response.raise_for_status()
            return response
        raise SourceError("YouTube rate limit; try again in a minute")

    async def fetch(
        self, ticker: str, company: str, since: datetime
    ) -> list[SocialPost]:
        """Find window-bounded videos (1 search call), then batch statistics."""
        if not _yt_take_search_budget():
            raise SourceError("YouTube quota reached for today")

        async with httpx.AsyncClient(timeout=10.0) as client:
            # One page only: every search page costs 100 quota units.
            company_search_name = await search_term(ticker, company)
            search = await self._get(
                client,
                self._SEARCH_URL,
                {
                    "part": "snippet",
                    "q": f"{company_search_name} stock",
                    "type": "video",
                    # publishedAfter/Before already bound the window, so this
                    # picks the *best* 50 in it, not the newest 50 of tens of
                    # thousands — which was almost entirely tiny fresh uploads.
                    "order": "viewCount",
                    "publishedAfter": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "publishedBefore": datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "maxResults": 50,
                    "relevanceLanguage": "en",
                    "key": config.YOUTUBE_API_KEY,
                },
            )
            video_ids = []
            for item in search.json().get("items") or []:
                item_id = item.get("id") or {}
                video_id = item_id.get("videoId")
                if video_id:
                    video_ids.append(video_id)
            if not video_ids:
                return []
            videos = await self._get(
                client,
                self._VIDEOS_URL,
                {
                    "part": "snippet,statistics",
                    "id": ",".join(video_ids[:50]),
                    "key": config.YOUTUBE_API_KEY,
                },
            )

        posts: list[SocialPost] = []
        for video in videos.json().get("items") or []:
            post = _video_post(video, since)
            if post is not None:
                posts.append(post)
        await self._attach_top_comments(posts)
        return posts

    async def _attach_top_comments(self, posts: list[SocialPost]) -> None:
        """Load top comments for the most-discussed videos, in place."""
        candidates = sorted(posts, key=_popularity, reverse=True)
        slots = comment_slots(len(candidates))
        if not slots:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            requests = []
            for post, limit in zip(candidates, slots):
                requests.append(self._comments_for(client, post.url, limit))
            batches = await asyncio.gather(*requests)
        for post, comments in zip(candidates, batches):
            post.top_comments = comments

    async def _comments_for(
        self, client: httpx.AsyncClient, video_url: str, limit: int
    ) -> list[PostComment]:
        """Fetch up to `limit` relevance-ordered comments; [] on any failure."""
        video_id = video_url.rsplit("v=", 1)[-1]
        try:
            response = await self._get(
                client,
                self._COMMENTS_URL,
                {
                    "part": "snippet",
                    "videoId": video_id,
                    "order": "relevance",
                    "maxResults": limit,
                    "textFormat": "plainText",
                    "key": config.YOUTUBE_API_KEY,
                },
            )
        # Comments are a bonus: a video with them turned off mid-flight, or any
        # other hiccup, must not cost us the video itself.
        except (SourceError, httpx.HTTPError):
            return []
        comments: list[PostComment] = []
        for item in (response.json().get("items") or [])[:limit]:
            thread_snippet = item.get("snippet") or {}
            top_comment = thread_snippet.get("topLevelComment") or {}
            comment_snippet = top_comment.get("snippet") or {}
            text = (
                comment_snippet.get("textOriginal")
                or comment_snippet.get("textDisplay")
                or ""
            ).strip()
            raw_published = comment_snippet.get("publishedAt")
            if not text or not raw_published:
                continue
            comments.append(
                PostComment(
                    author=(
                        comment_snippet.get("authorDisplayName") or "unknown"
                    ).strip(),
                    text=text,
                    likes=int(comment_snippet.get("likeCount") or 0),
                    published_at=datetime.fromisoformat(
                        raw_published.replace("Z", "+00:00")
                    ),
                )
            )
        return comments
