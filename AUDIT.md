# Stock Digest Audit

Date: 2026-09-28. Scope: all of `stock_digest/` (1,641 lines), `pyproject.toml`, `README.md`, and the build brief. Nothing was changed. Findings marked **confirmed** were reproduced by calling the installed package's functions directly; the rest come from reading the code.

## 0. Implementation status (2026-09-28)

Every item below has been implemented. The offline suite has 46 passing tests (`python -m pytest`), and live NVDA and JPM runs completed with model review on. One new bug surfaced while implementing and is fixed as B17.

| Item | Fix | Where |
| --- | --- | --- |
| B1 update-only dates | Bound is the later of whichever dates exist | `manager.eligible_packets` |
| B2 event dates | Failed event rules fall through to the news-window check | `manager.eligible_packets`, research prompt |
| B3 relevance | Legal suffixes stripped, distinctive words, brand aliases, case-sensitive ticker forms | `manager.company_relevance` |
| B4 renderer crash | Dates normalized at collection; display never raises | `manager.extract_sources`, `dates.display_timestamp` |
| B5 year-less datelines | Year-less and relative datelines resolved against retrieval time | `dates.date_in_passage` |
| B6 truncation | 8,000-token research budget, shorter quotes, explicit truncation message | `agents.py`, `models.py` |
| B7 dedup | By source, then by headline, across all queries | `manager.dedupe_packets` |
| B8 stale history | Earnings-history sources limited to the earnings preview | `manager.check_digest` |
| B9 invented times | Clock time kept only when quoted with a matching timezone | `dates.time_in_passage` |
| B10 whole-narrative drop | Flagged claims removed individually | `manager.prune_digest` |
| B11 headline repair | Writer context kept; citation list trimmed to fit [1] | `manager.repair_headline` |
| B12 target period | Yahoo's current-quarter consensus; research limited to the next quarter | `earnings.py` |
| B13 magnitude | Revenue under $1M or 10x off consensus rejected | `earnings._research_metrics` |
| B14 dead zones | Previous session or last eligible bar instead of errors | `market.choose_session` |
| B15 budget | 60 s research timeouts, one retry, stages skipped near the deadline, 480 s default | `agents.py`, `manager.run_digest` |
| B16 small issues | Safe error text, provider dates protected, New York date, empty digest check, sibling cancellation, cache guard | various |
| B17 (new) evening failure | Yahoo's same-day daily close is empty after the bell; the quote's official close is used | `market._snapshot` |
| 4.1 structured data | Earnings dates, estimates, revisions, rating changes, targets, filings (EDGAR optional), insiders, straddle, benchmarks, peers, relative volume, gap, extended hours | `catalysts.py`, `market.py` |
| 4.2 timing policy | Before/same-day/after classification; hedged same-day links; weekend items classed as later | `manager.py`, prompts |
| 4.3 queries | Seven focused queries, one domain-filtered to issuer and newswires | `manager.run_digest` |
| 4.4 prompts | Numbered rules, field definitions, example, catalyst type and relevance; per-role models | `agents.py` |
| 4.5 review default | On by default with `gpt-4.1-mini`; `--no-verify` opts out | `__main__.py` |
| 4.6 output | Publisher/title/date sources, compact numbering, Generated line, relative ages, short coverage, `--debug` | `render.py`, `manager.renumber` |
| 4.7 tests | pytest suite with a regression per bug and mocked pipeline runs | `tests/` |

## 1. Verdict

The pipeline is sound and unusually careful about provenance: prices come from a structured feed, source IDs come from real tool metadata, and every claim carries citations. The main problems are the opposite of hallucination: the eligibility rules are so strict, and the inputs so limited, that the digest often cannot do the one thing it exists for, which is to connect today's move to today's catalyst. Five real bugs drop or crash on valid evidence, and several design choices throw away most of what the research finds.

Priorities, in order:

1. Fix the five confirmed bugs (section 2). Small diffs, immediate coverage gain.
2. Pull structured catalyst data from yfinance that the app already has access to but does not use: earnings dates, consensus estimates, analyst actions, options chain, SEC filings, extended-hours quotes (section 4.1).
3. Relax the timing policy so a same-day press release can be presented as a likely explanation with a caveat, instead of being demoted to "context only" (section 4.2).
4. Rewrite the search queries and prompts (sections 4.3, 4.4).

## 2. Confirmed bugs

### B1. Findings with only an update date are always dropped
`stock_digest/manager.py:264-273`. `bound = max(pub_bound, update_bound) if pub_bound and update_bound else pub_bound` evaluates to `None` whenever the finding has a substantiated `updated` date but no `published` date. The very next check then omits the finding as "publication/update date missing". This defeats the `mutable_page` rule, which asks for an update date and then discards findings that supply one.

Repro: a `reporting` finding with `published=None`, `updated=<today>`, and a matching `timestamp_basis` yields 0 packets. Fix: `bound = max((b for b in (pub_bound, update_bound) if b), default=None)`.

### B2. Fresh news that mentions an event date is discarded entirely
`stock_digest/manager.py:279-286`. If the researcher fills `event_date` for a past event, or for a future event reported by a news outlet rather than a `dated_announcement`, the whole finding is omitted instead of being treated as ordinary news. The prompt does not tell the model that `event_date` is future-only, so "Company held its call on Sept 27" or "Bloomberg reports the investor day is Oct 15" both vanish.

Repro: a same-day `reporting` finding with `event_date=yesterday` yields 0 packets ("event lacks a dated announcement"). Fix: when the event-date rules fail, set `event_date=None` and fall through to the normal news-window check rather than `continue`.

### B3. Yahoo relevance filter is wrong for short tickers and multi-word companies
`stock_digest/manager.py:327-333`. Two problems:

- The ticker regex is compiled with `re.I`, so ticker `A` matches the word "a", `F` matches "f", and `ALL`, `ON`, `IT`, `NOW`, `LOW`, `KEY`, `SO`, `HAS`, `AN`, `BE` match everyday words. Every unrelated market story passes.
- The company name must match the full legal name verbatim ("The Walt Disney Company"), and the brand fallback gives up when the first word is generic. So Disney, Bank of America, General Motors, American Express, United Airlines, and International Business Machines get no brand word at all, and their own headlines are rejected as "not visibly company-related". Alphabet never matches "Google".

Repro: with ticker `A` and company "The Walt Disney Company", "Markets wrap: a quiet day for stocks" is kept and "Disney raises park prices" is dropped. Fix: match the ticker case-sensitively (optionally requiring `$A`, `(A)`, or `A stock`); strip legal suffixes (Inc, Corp, Corporation, Company, Co, Ltd, plc, Holdings, Group, The); use the first one or two non-generic words; also use Yahoo's `shortName` as an alias.

### B4. Renderer crashes on a non-ISO source date
`stock_digest/render.py:68-70`. `Source.published` stores the raw string from tool metadata (`manager.py:213`), which only has to be parseable by `normalize_source_timestamp`. The renderer assumes anything longer than ten characters is ISO and calls `datetime.fromisoformat` directly.

Repro: a source with `published="September 28, 2026"` in `news_source_ids` raises `ValueError: Invalid isoformat string`. This is after all research succeeded, so the whole run dies at the last step. Fix: normalize first and fall back to printing the raw string.

### B5. Year-less datelines never substantiate a date
`stock_digest/manager.py:161-182`. `date_in_passage` requires the year to appear in the quoted passage. Real datelines and search snippets are overwhelmingly "Published Sep 28, 10:15 AM ET", "Sep 28", "3 hours ago", or "Updated 2 hours ago". None match, so the finding is omitted as "not substantiated by a quoted dateline". This is probably the single largest cause of the "price-only" and "1 eligible finding" outcomes recorded in the README.

Repro: `date_in_passage("2026-09-28", "Published Sep 28, 10:15 AM ET")` is `False`. Fix: accept month-day without year when the implied date (current year, or previous year if that would be in the future) is within the search window; also accept `YYYY/MM/DD` and `DD-Mon-YYYY`. Treat relative phrases as date-only at retrieval time.

## 3. Likely bugs and design flaws (from reading)

### B6. Research output truncates and silently loses findings
`stock_digest/agents.py:142`. `max_tokens=3200` for a response that may contain six findings, each with a summary (≤1,500 chars), an excerpt, a dateline quote, and up to eight earnings metrics each with a quote (≤1,500 chars). Earnings-history queries can exceed this several times over. Truncation leaves partial JSON; `parse_research` keeps only complete objects or raises "no complete JSON findings", which is then reported as a generic failure. Raise the limit to ~8,000 and cap `supporting_quote` at ~300 and `reported_excerpt` at ~600 characters.

### B7. Cross-query deduplication depends on model-invented keys
`stock_digest/manager.py:634-642`. `story_key` is generated independently per query, so the same article found by two queries usually produces two packets. A Yahoo feed item and a search hit on the same URL always produce two packets because `research_purpose` is part of the key. Deduplicate news packets by `source_id` first, then by normalized title.

### B8. Stale earnings-history evidence can become "news"
`stock_digest/manager.py:625,631` admit articles up to 550 days old as packets. Nothing in `check_digest` stops the writer from building a topic on a 2025 earnings article in default mode; the verifier only sees this with `--verify`. Tag those packets as earnings-only and reject any non-earnings claim that cites a source older than the news window.

### B9. Model-written clock times are trusted for price ordering
`date_in_passage` verifies only the date portion of `finding.published`. A researcher who writes `2026-09-28T09:00:00-04:00` gets `price_timing_unknown=False` and `later_than_price=False` on the strength of a time it may have invented. This contradicts the design's own rule ("never invent timestamps"). Either verify the time string appears in `timestamp_basis`, or treat the time portion as unverified. See section 4.2 for the policy question underneath this.

### B10. Whole narrative is discarded for one bad citation
`stock_digest/manager.py:739-746`. In default mode any single `check_digest` issue (one sentence citing an ineligible source) drops every topic and prints headlines only. Drop or relabel the offending sentence, drop a topic only if its heading is affected, and fall back to headlines only when nothing remains.

### B11. Headline repair throws away the writer's headline
`stock_digest/manager.py:401-410`. When `{move}` is missing and the exact price phrase is not present once, the headline becomes just `{move}`. The headline is the highest-value line in the digest. Prefer `"{move}; " + original text` with `[1]` prepended.

### B12. Earnings target period is chosen arbitrarily
`stock_digest/earnings.py:39`. The fiscal period is whatever the newest consensus record says, regardless of whether it is the quarter being reported. A next-fiscal-year or two-quarters-out estimate silently becomes "the upcoming release". Derive the expected period from the last reported quarter (yfinance `earnings_dates` or `quarterly_income_stmt`) and require the estimate to match it.

### B13. Revenue magnitude is never sanity-checked
`stock_digest/earnings.py`. A researcher writing `46.7` (billions) instead of `46700000000` passes validation and the writer prints "$46.7". Reject revenue below 1e6 or add a `scale` field, and cross-check against yfinance `revenue_estimate`.

### B14. Dead zones and illiquid names abort the run
`stock_digest/market.py:123-126,175-176`. The app refuses to run 09:30-09:46 ET and 16:00-16:15 ET, and refuses any stock without a 1-minute bar in the last five minutes of the delayed window. For a digest tool, falling back to the last completed session close, or the last eligible bar with its timestamp and a note, is more useful than an error.

### B15. Run deadline is not budget-aware
Up to 11 research calls (3 in flight, 90 s each) plus writer, and with `--verify` two verifier and one revision call, against a 360 s default deadline with `max_retries=0`. The README already records runs dying at the deadline. Skip earnings follow-ups when less than ~120 s remain, shorten research timeouts to 45-60 s, and allow one retry for research calls only.

### B16. Smaller issues
- `manager.py:545-549`: app-authored `DigestError` messages are replaced by the generic "OpenAI response or research processing failed", hiding the real reason on stderr.
- `manager.py:261-263,299`: a search finding that supplies `updated` for a Yahoo-feed source overwrites `timestamp_provenance`, which later allows Yahoo's `published` to be overwritten by search data.
- `earnings.py:86`: `market.as_of.date()` is a UTC date; every other day comparison uses New York.
- `models.py:159`: `Digest.topics` allows zero topics; a writer that returns none still publishes.
- `manager.py:603`: an `InputError` from one search propagates from `gather` while sibling searches keep running until the client closes.
- `market.py:56-58`: private yfinance cache attributes are used; safe only while the version pin holds.

## 4. Changes that would improve the digest

### 4.1 Use structured catalyst data you already have (no new keys)
yfinance 1.7 exposes all of the following, and none of it is used today. Each item replaces a fragile web-search-and-extract step with deterministic data.

| Source | What it gives the digest |
| --- | --- |
| `Ticker.calendar`, `earnings_dates` | Next earnings date and ex-dividend date. Replaces the 90-day event search; keep the search only to confirm the issuer announcement. |
| `earnings_estimate`, `revenue_estimate`, `eps_trend`, `eps_revisions` | Consensus revenue and EPS for the current quarter, plus 7/30-day estimate revisions, which are a catalyst in their own right. Fixes B12 and B13. |
| `upgrades_downgrades`, `analyst_price_targets` | Analyst rating and target changes. These are among the most common single-day movers and there is currently no query for them at all. |
| `option_chain(expiry)` | ATM straddle for the first expiry after earnings, so the implied move is computed rather than searched for. The README's MU runs repeatedly found no published figure. |
| `get_sec_filings()` | Recent 8-K items with exact acceptance timestamps: 2.02 results, 1.01 material agreements (M&A), 5.02 leadership, 7.01/8.01 guidance and other events. This is the canonical source for most of the catalyst types you listed. EDGAR's submissions API is also free and keyless if the yfinance list is thin. |
| `quarterly_income_stmt` | Prior-quarter and year-ago actual revenue/EPS for QoQ/YoY baselines, replacing the 550-day history search. |
| `insider_transactions` | Recent insider buys and sells with transaction versus filing dates. |
| `get_info()` pre/post-market fields | Extended-hours price and change. Earnings and most material 8-Ks land after the close or before the open; without this the digest shows a stale regular-session close on the day that matters most. |
| Daily history for SPY, QQQ, sector ETF, 2-3 peers | "NVDA +4.1% vs. SOX +0.8%" is what Cortex leads with. Also enables "sector-wide" versus "company-specific" framing. |
| Daily volume history | Relative volume (today vs. 20-session average). High relative volume corroborates a catalyst; normal volume supports "no clear catalyst". |
| The 1-minute bars already fetched | Gap at the open vs. intraday drift. Separates overnight news from intraday news. |

Buybacks, dividends, and capital returns are in your target list but appear in no query; add them to the announcements query, and read ex-dividend dates from `calendar`.

### 4.2 Let same-day news explain the move
Under the current rules a web-search finding with a date-only dateline dated today gets `price_timing_unknown=True`, is labeled "(publication time unknown; context only)", and the writer is told it cannot explain the move. Only Yahoo feed items carry a clock time. In practice this means the digest usually cannot attribute today's move to today's press release, which is the core product goal.

Options, from least to most permissive:
- Inherit exact timestamps where they exist: SEC acceptance times for filings; Business Wire, PR Newswire, and GlobeNewswire pages carry exact times the researcher can quote; Yahoo feed items on the same URL or title already merge.
- Allow the writer to say "may be linked to" or "coincides with" for same-day date-only items, with the caveat kept, rather than banning any link.
- Reserve the hard "cannot explain" rule for items whose bound is provably after the price observation.

Whichever you choose, resolve B9 at the same time so the policy is consistent.

### 4.3 Redesign the queries
`stock_digest/manager.py:593-601`. The current queries are long keyword lists with a textual date range appended, for example "announcements earnings guidance financing SEC filings regulatory decisions lawsuits recalls leadership changes 2026-09-25 to 2026-09-28". Search engines do not treat the range as a filter, and mixing eight topics into one query dilutes ranking. Suggested set:

1. `{Company} ({TICKER}) stock news today` (the "why is it moving" query).
2. `{Company} press release` restricted to the issuer's IR domain and the three newswires via `WebSearchToolFilters.allowed_domains` (supported by the installed SDK; verify the field name). Gives confirmed announcements with exact times.
3. `{TICKER} analyst upgrade downgrade price target` (currently missing).
4. `{Company} earnings date guidance buyback dividend`.
5. `{Company} acquisition merger deal talks`.
6. `{Company} report rumor "sources say"` for unconfirmed reporting.
7. `{sector} stocks today` for context.

Pass the as-of date in the JSON input (already done) and use natural phrasing such as "today" or "this week" in the query text instead of ISO ranges.

### 4.4 Rewrite the prompts
`stock_digest/agents.py:27-133`. The three prompts are compressed into telegraphic clauses ("Options require a reported percentage and observation/event dates; preserve expiry/method, never substitute historical moves"). Models follow numbered rules with definitions and one worked example far more reliably. Specific additions:

- Ask the researcher for `catalyst_type` (earnings, guidance, buyback, dividend, M&A, rumor, analyst, product, legal or regulatory, macro or sector, insider, other) and `move_relevance` (direct, context). The manager can then order topics and the writer can produce Cortex-style headings ("Raised revenue outlook", "Next event: Earnings Oct 22").
- Define `content_kind` and `confirmation_status` with examples; state that `event_date` is future-only (fixes the root of B2).
- Give the writer the target shape explicitly: headline; "What moved the stock" or "No clear catalyst identified in the sources checked"; catalyst topics in order of relevance; "Next event"; 200-350 words. Ask each topic sentence to carry its date ("On Sept 26, ...").
- Consider a newer reasoning-capable model for the writer and verifier, and a cheaper mini-tier model for research extraction; check the current OpenAI model list for web-search support before switching.

### 4.5 Verification on by default, cheaply
The verifier is the only component that checks causality, numbers, and attribution, and it is off by default. Run it on a small, fast model by default; keep blocking behavior for contradictions and numeric errors, and keep labeling for support gaps (already implemented). Fixing B10 makes this less punitive.

### 4.6 Output
- The Sources list prints URLs only. The brief asked for publisher, title, and date, and readers need them to judge a citation. `stock_digest/render.py:98-101`.
- Coverage prints every internal note as one paragraph, often 10+ sentences of pipeline diagnostics. Keep one to three reader-facing limitations in the output and move the rest to stderr or a `--debug` flag.
- Add the "Generated:" line from the brief and a relative freshness tag ("2h ago") next to absolute times.
- Show extended-hours price and relative volume in the header block once 4.1 is in.

### 4.7 Tests
The brief excluded tests, but `availability_bound`, `date_in_passage`, `eligible_packets`, and `yahoo_news_packets` are pure functions and B1 through B5 are all one-line pytest cases. A dozen tests would protect the timing logic, which is the most intricate code in the repo, when you make the changes above.

## 5. Suggested order of work

1. B1, B2, B4, B5, B3 (one afternoon; largest coverage gain per line).
2. B6, B7, B10, B11 (research budget and narrative resilience).
3. Structured data from yfinance: earnings date, estimates, analyst actions, filings, extended hours, index and sector context, relative volume (4.1).
4. Timing policy and B9 (4.2).
5. Query and prompt rewrite (4.3, 4.4), then default-on verification (4.5).
6. Output polish (4.6) and a small test file (4.7).
