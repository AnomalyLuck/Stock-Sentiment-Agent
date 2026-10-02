"""Seeking Alpha source: configured-but-unimplemented stub."""

from datetime import datetime

from . import config
from .social_types import SocialPost, SourceError


class SeekingAlphaSource:
    """Seeking Alpha via a third-party provider; active only when configured."""

    name = "seeking_alpha"

    def __init__(self) -> None:
        self.enabled = bool(config.SEEKING_ALPHA_API_KEY)

    async def fetch(
        self, ticker: str, company: str, since: datetime
    ) -> list[SocialPost]:
        """No provider integrations are implemented yet; surface as unavailable."""
        raise SourceError("Seeking Alpha provider is not supported yet")
