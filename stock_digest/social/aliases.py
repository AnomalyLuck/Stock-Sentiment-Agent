"""LLM-derived company names, one cached call per distinct ticker.

Ported from Sentiment-Search `app/aliases.py`; the one change is the model call,
which uses this app's OpenAI key (Responses API) instead of Anthropic.

Headlines and comments rarely use a stock's registered legal name ("ALPHABET
INC-CL C"); they say "Google". A single cheap model call per ticker — cached
for the process lifetime — supplies both:

- a *primary search name*: the colloquial name used as the query term on
  general platforms (Google News, Hacker News, YouTube, Reddit search)
- *aliases*: every name worth matching in headlines/comments (brand names,
  parents/subsidiaries, misspellings, sibling share-class tickers)

STATIC_NAMES seeds known cases and overrides the model; name lookups can never
break retrieval (callers fall back to the core company name / ticker).
"""

import json
import logging
import re

from . import config
from .cache import alias_cache

logger = logging.getLogger(__name__)

# Trailing legal suffixes stripped when deriving a usable company name
# ("NVIDIA CORP" -> "nvidia"). Share-class tails ("ALPHABET INC-CL C") must go
# too, or the core name never appears in any headline.
_NAME_SUFFIX_RE = re.compile(
    r"(\s+(inc|corp|corporation|co|company|ltd|plc|holdings|group|sa|ag|nv)\.?"
    r"|[-\s]+(cl|class)\s+[a-c])$",
    re.IGNORECASE,
)


def _core_company_name(company_name: str) -> str:
    """Lowercase a company name and strip trailing legal/share-class suffixes."""
    name = company_name.strip().lower()
    while True:
        stripped = _NAME_SUFFIX_RE.sub("", name)
        if stripped == name:
            return name
        name = stripped


# Seed/override entries; "search" wins over the model's primary, and seed
# aliases are merged ahead of learned ones.
STATIC_NAMES: dict[str, dict] = {
    "GOOG": {"search": "google", "aliases": ("google", "googl")},
    "GOOGL": {"search": "google", "aliases": ("google", "goog")},
    "META": {"search": "meta", "aliases": ("facebook",)},
    "BRK.B": {"search": "berkshire hathaway", "aliases": ("berkshire",)},
    "NVDA": {"search": "nvidia", "aliases": ("nvida", "nivida")},
    "SPCX": {"search": "spacex", "aliases": ("spacex",)},
}

_MAX_ALIASES = 8
_RETRY_TTL = 300  # seconds; failed/unparseable lookups retry after this

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

_PROMPT = (
    'Stock ticker {ticker} has the registered company name "{company}".\n'
    "Identify the names media and retail investors actually use for it.\n"
    "Return ONLY a JSON object, no prose, no markdown fences, shaped like:\n"
    '{{"primary": "google", "aliases": ["alphabet", "googl"]}}\n'
    '- "primary": the single most common colloquial company name (lowercase), '
    "suitable as a news search query.\n"
    '- "aliases": up to 6 other lowercase names: brand/parent/subsidiary '
    "names, common misspellings, sibling share-class ticker symbols. Exclude "
    "the ticker {ticker} itself and generic words."
)

_client = None


def _get_client():
    """Return a shared async OpenAI client (one per process; social.py runs one loop)."""
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        _client = AsyncOpenAI(api_key=config.OPENAI_API_KEY, max_retries=1, timeout=10.0)
    return _client


def _clean_name(item: object) -> str:
    """Validate one model-returned name; '' when unusable."""
    if not isinstance(item, str):
        return ""
    name = item.strip().lower()
    if len(name) < 3:
        return ""
    return name


def parse_names_json(raw: str) -> tuple[str, list[str]]:
    """Parse the {"primary", "aliases"} object; ("", []) on any failure."""
    cleaned = _FENCE_RE.sub("", raw.strip()).strip()
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        return "", []
    if not isinstance(data, dict):
        return "", []
    primary = _clean_name(data.get("primary"))
    aliases: list[str] = []
    raw_aliases = data.get("aliases")
    if not isinstance(raw_aliases, list):
        raw_aliases = []
    for item in raw_aliases:
        name = _clean_name(item)
        if name and name not in aliases:
            aliases.append(name)
    return primary, aliases[:_MAX_ALIASES]


async def names_for_ticker(ticker: str, company: str) -> tuple[str, tuple[str, ...]]:
    """(search_name, match_aliases) for a ticker; LLM-derived once, cached."""
    key = ticker.upper()
    cached = alias_cache.get(key)
    if cached is not None:
        return cached

    seed = STATIC_NAMES.get(key, {})
    core_name = ""
    if company:
        core_name = _core_company_name(company)

    fallback_search = seed.get("search")
    if not fallback_search:
        if len(core_name) > 2:
            fallback_search = core_name
        else:
            fallback_search = key
    seed_aliases: tuple[str, ...] = tuple(seed.get("aliases", ()))

    if not config.OPENAI_API_KEY:
        result = (fallback_search, seed_aliases)
        alias_cache.set(key, result, ttl=None)
        return result

    try:
        response = await _get_client().responses.create(
            model=config.ALIAS_MODEL,
            max_output_tokens=250,
            input=_PROMPT.format(ticker=key, company=company),
        )
        raw = response.output_text or ""
        primary, learned = parse_names_json(raw)
    except Exception:  # noqa: BLE001 - optional name lookup must never break retrieval
        logger.warning("name lookup failed for %s; using fallbacks", key)
        result = (fallback_search, seed_aliases)
        alias_cache.set(key, result, ttl=_RETRY_TTL)
        return result

    search = seed.get("search") or primary or fallback_search
    candidate_aliases = list(seed_aliases)
    if primary:
        candidate_aliases.append(primary)
    candidate_aliases.extend(learned)

    unique_aliases = []
    for name in candidate_aliases:
        if name not in unique_aliases:
            unique_aliases.append(name)
    aliases = tuple(unique_aliases)
    result = (search, aliases)
    # Unparseable output: keep fallbacks but retry later rather than pinning.
    ttl = _RETRY_TTL
    if primary or learned:
        ttl = None
    alias_cache.set(key, result, ttl=ttl)
    logger.info("names for %s: search=%r aliases=%s", key, search, list(aliases))
    return result


async def search_term(ticker: str, company: str) -> str:
    """Search keyword general platforms know the company by (LLM-derived,
    cached per ticker; e.g. SPCX -> "spacex", not the truncated legal name)."""
    search_name, _ = await names_for_ticker(ticker, company)
    if len(search_name) > 2:
        return search_name
    return ticker
