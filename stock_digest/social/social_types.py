"""Shared social post types and the error every source adapter raises.

Kept apart from `social.py` so each `*_source.py` module can build posts
without importing the orchestrator that imports it.
"""

from dataclasses import dataclass, field
from datetime import datetime


class SourceError(Exception):
    """Raised by an adapter that is configured but cannot serve requests."""


@dataclass
class PostComment:
    """A top comment shown alongside its parent post (YouTube videos today)."""

    author: str
    text: str
    likes: int
    published_at: datetime  # UTC


@dataclass
class SocialPost:
    """Normalized social post carrying timing and engagement for ranking."""

    type: str  # "reddit" | "stocktwits" | "hackernews" | "youtube" | "x" | ...
    title: str
    text: str
    url: str
    source: str  # e.g. "r/wallstreetbets", "StockTwits"
    author: str
    published_at: datetime  # UTC
    likes: int = 0
    comments: int = 0
    views: int | None = None  # display only (YouTube); never a ranking input
    # Thread context for comment-type items (Reddit): secondary in UI, required.
    context: str | None = None  # parent post title
    is_megathread: bool = False
    author_flair: str | None = None
    # A few top comments, display only; never a ranking input.
    top_comments: list[PostComment] = field(default_factory=list)
