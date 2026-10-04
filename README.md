# Stock Digest

A Python terminal application that prints a current, cited digest for one US-listed
common stock or ADR: the price move, what may explain it, relevant company news and
catalysts, broader market context, and the next scheduled event. It combines
structured Yahoo Finance data (prices, earnings, estimates, rating changes, filings,
options, benchmarks) with bounded hosted web research, writes the digest with a model,
and checks it with Python rules and a model reviewer. Only the finished result reaches
stdout. Terminal digest mode needs no browser or server. The material-news agent keeps a
small local cache of company profiles and successfully verified evidence (see below).

## Install and configure

Python 3.11 or newer is required (the system Python on some Macs is too old).

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -c requirements.lock -e ".[dev]"   # dev adds pytest; the lock pins tested versions

export OPENAI_API_KEY='your-openai-key'
export OPENAI_MODEL='gpt-4.1'                 # optional; writer model (default shown)
export OPENAI_RESEARCH_MODEL=''               # optional; defaults to OPENAI_MODEL
export OPENAI_VERIFY_MODEL='gpt-4.1-mini'     # optional; reviewer model (default shown)
export STOCK_DIGEST_TIMEOUT='480'             # optional; whole-run deadline in seconds
export SEC_USER_AGENT='Your Name you@example.com'  # optional; enables SEC EDGAR filings
export STOCK_DIGEST_MAX_RUNS='3'              # optional; UI searches allowed at once
```

The CLI reads `.env` from the current directory (see `.env.example`); exported
variables take precedence. Only the settings above are loaded, without shell
execution or interpolation. Keep credentials private; never commit real keys.

Yahoo Finance requires no API key. The OpenAI account needs API billing, access to
the selected models, and hosted web search for the research model. Every model must
support the Responses API and structured output.

`SEC_USER_AGENT` is sent only to sec.gov, whose fair-access policy requires a
descriptive User-Agent with contact details. Without it, Yahoo's filing list is used;
its titles are generic and its stamps are date-only, while EDGAR supplies 8-K item
codes and exact acceptance times.

## Use

```sh
stock-digest                      # open the browser UI at http://127.0.0.1:8765/
stock-digest NVDA                 # open the UI and run NVDA immediately
stock-digest --port 9000 --no-browser
stock-digest NVDA --no-verify     # skip the model reviewer (basic code checks still run)
stock-digest NVDA --terminal      # print to the terminal instead (also --plain, --no-footer, --debug)
stock-digest NVDA > digest.txt    # redirected output uses terminal mode automatically
material-news NVDA                # material company developments (see "Material news agent")
python -m pytest                  # offline test suite
```

The UI is a single local page served by Python's standard library, bound to
127.0.0.1 only; nothing is exposed to the network. The page's only external requests
are YouTube thumbnails from i.ytimg.com in the Social Sentiment tab. Type a ticker and
press Search. One combined run starts: the material news
agent (below) and the digest, which waits for the material results and uses them as
evidence. Progress streams into each tab; the Material news tab fills first (about
2.5 to 3 minutes) and the Digest tab shortly after (about 3 to 4 minutes in total).
Starting another search or closing the tab abandons the current run. Stop the server
with Ctrl-C. The terminal mode (`--terminal`) still runs the digest on its own, with
its own seven searches, in one to two minutes.

The server runs at most `STOCK_DIGEST_MAX_RUNS` searches at once (default 3), counting
combined, digest-only and material-only runs. A search beyond that ends immediately
with a "server is busy" error in each tab, before any model call. An abandoned run
keeps its slot until its next progress update. Social Sentiment requests are not
counted; they have their own daily provider budgets (see below).

## Running on a server

The UI server stays bound to 127.0.0.1. To reach it from other machines, put an
authenticating HTTPS proxy in front: [`deploy/Caddyfile`](deploy/Caddyfile) does this
with Caddy (automatic TLS and a `basic_auth` password per person). The reusable proxy
and service configuration is versioned in `deploy/`; real credentials stay on the
server. Without authentication, anyone who finds the address can spend your OpenAI
and social-provider credits.

```sh
python -m pip install -c requirements.lock .   # tested dependency versions, without pytest
stock-digest --no-browser                      # serves 127.0.0.1:8765 for the proxy
```

The proxy also requires `Sec-Fetch-Site: same-origin` on API requests, removes query
strings from external page links (so a shared `?t=NVDA` link cannot start paid work),
and prevents framing the UI. Use a current browser. Direct API clients must supply
both Basic Auth and that header; browsers that omit it receive HTTP 403.

### DigitalOcean Droplet (Rocky Linux 10, built by UserData)

The Droplet builds itself from `UserData.sh`, a cloud-init loader generated from a private
UserData kit and kept out of git (see [`adr/`](adr/)). It brings the box to an SSH-only baseline
(nftables with a `TcpOK` port set, hardened sshd), fetches this repository into `/srv/git-ops`,
records the commit, and calls [`stages/StockDigest.sh`](stages/StockDigest.sh). The stage
installs Python 3.12 and Caddy (EPEL), builds `/opt/stock-digest/.venv` from `requirements.lock`,
installs the files in [`deploy/`](deploy/), opens ports 80 and 443, and enables both services.
UserData contains no secrets, so the services wait for them. The fetch uses no credentials, which
is why the repository is public.

1. **Create the Droplet.** Image **Rocky Linux 10 x64**, your SSH key, and the full text of
   `UserData.sh` pasted into **User data**. 1 GB of RAM is enough.
2. **Watch the build.** After a few minutes, `ssh root@DROPLET_IP`. The login banner gives the
   build status. Logs are in `/srv/BldTmp/UserData/`: `loader.log` and `stockdigest.log` end with
   `PASS`/`FAIL` lines and a `VERIFY` summary. A `.partial` file means that stage died there.
3. **Prepare the secrets** in the gitignored `deploy/secrets/` directory. `app.env` is seeded from
   `.env` with the server lines added; review it, set `SEC_USER_AGENT` if you want SEC filings,
   and delete unused provider keys and every `your-...` placeholder. It is a systemd
   `EnvironmentFile`: `KEY=value` lines and `#` comments only, no `export`, no trailing comments.
   Then create the login file; the password is typed at the prompt, never on the command line:
   ```sh
   printf 'admin %s\n' "$(caddy hash-password --algorithm bcrypt)" >> deploy/secrets/stock-digest.users
   ```
   One line per person. The username can be anything.
4. **Point DNS** at the Droplet: the `A` record to its public IPv4, and the `AAAA` record to its
   IPv6 or removed. The `www` CNAME can keep pointing at the root domain.
5. **Push the secrets and start the site:**
   ```sh
   deploy/push-secrets.sh root@DROPLET_IP
   ```
   This copies the two files with root-only modes, runs `stock-digest-activate` on the box, and
   prints its `PASS`/`FAIL` checks: the app answering on loopback only, Caddy redirecting on 80,
   and `https://stocksentimentdigest.com/` answering **401** until you sign in. If the last check
   fails with code 000, Caddy has no certificate yet; confirm DNS and retry. Re-run the same
   command after changing a key or a login.

**Afterwards.** `systemctl status stock-digest caddy` and `journalctl -u stock-digest -u caddy`.
To deploy new code:
```sh
ssh root@DROPLET_IP 'cd /srv/git-ops && git fetch --depth 1 origin main && git checkout -q -f --detach FETCH_HEAD \
  && /opt/stock-digest/.venv/bin/python -m pip install -q -c requirements.lock . && systemctl restart stock-digest'
```
SELinux is permissive for the build boot only; the first reboot returns it to enforcing, so check
both services after that reboot. Ports 8765 (app) and 2019 (Caddy admin) stay closed; the firewall
is nftables inside the box, so a DigitalOcean cloud firewall is optional but harmless. Ports 80
and 443 are re-added to `TcpOK` after every nftables start by `stock-digest-ports`, which a drop-in
runs; a `systemctl reload nftables` drops them until the next restart or until you run
`stock-digest-ports` or `stock-digest-activate`.

`requirements.lock` pins the versions the test suite last passed with; every package in
it has a Linux x86_64 wheel for Python 3.12, so a server needs no compiler. To change
them, run `uv pip compile pyproject.toml --extra dev --universal -o requirements.lock`
(add `--upgrade` for newer releases), reinstall and run the tests.

`BRK.B` and `BRK-B` both request Yahoo symbol `BRK-B`. Yahoo must classify the symbol
as a USD `EQUITY` on a supported US exchange; ETFs, crypto, OTC and foreign venues are
rejected. Untrusted text is always inserted as text, never as HTML, and only http(s)
links are rendered.

Terminal exit codes: `0` full or limited result, `2` input/configuration or unsupported
listing, `1` operational failure, `130` interruption.

## Material news agent

A second agent, in its own **Material news** tab of the UI and as the `material-news`
command, answers a different question: what potentially stock-moving, company-led
developments happened in the last week? It returns one entry per distinct development,
each with a date, a status, a concrete reason it could matter, and a source link. It uses
the same `OPENAI_API_KEY`, models (`OPENAI_RESEARCH_MODEL` for search and verification,
`OPENAI_MODEL` for consolidation) and `STOCK_DIGEST_TIMEOUT` deadline; no other
credentials are needed.

```sh
material-news NVDA                          # Markdown digest to stdout, progress to stderr
material-news NASDAQ:MSFT --tz Asia/Tokyo   # exchange prefix; display timezone
material-news TSX:SHOP --max 3              # an explicit maximum (disclosed)
material-news NVDA --record events.json     # also write the internal event record
python -m stock_digest.material NVDA        # same, without the installed script
```

In the UI, enter a ticker once and press Search: the one search box starts one combined
run (`GET /api/run`) that streams both the **Material news** result and the **Digest**
(the **Social Sentiment** tab loads when it is open; see below). A dot on a tab marks a
run still in progress. The digest covers US listings only, so for `TSX:SHOP` the Digest
tab reports an error while the Material news tab still fills in. Re-submitting the
running ticker is blocked; a different ticker restarts both. The Material news tab sends
the browser's timezone, offers an optional maximum (applied at the next search, to the
table only), and **Copy Markdown** copies the exact text output.

**How the digest uses the material results.** In the combined run the digest does not
search for press releases, deals or reported talks itself; the material agent covers
those with more searches and by opening the sources. The digest keeps four searches the
material agent excludes by design: today's move reporting, analyst actions, the
earnings-date announcement, and sector/macro news. After its own research it waits for
the material run (a material failure fails the digest too), then takes every qualifying
development first reported **today or in the previous trading session** (a Monday digest
sees Friday through Monday) as evidence. Older developments stay in the Material news
tab. Each supplied development becomes a cited source whose timing is classified against
the price observation like any other item, and the writer and reviewer are told that its
supporting material is the material agent's verified key facts, not article text. The
Yahoo headlines, structured Yahoo data and the digest's own checks are unchanged.

**Cost per ticker search.** After each result, the UI shows an estimated USD cost below
the results, split between Material news and Digest. The first result shows the running
subtotal; the last result (or error) shows the final reported amount. Expand **Cost
breakdown** for model-token costs, web-search charges, model names and token counts.
Each new search starts a fresh total. Concurrent ticker requests are accounted separately.

The server observes raw Responses API usage before the agent SDK parses the output. This
includes research, writing, review, revisions and any retry response that reports usage,
even if the application later rejects its output. Local cache hits incur no new model
charge. Cached-input and cache-write tokens use separate rates; reasoning tokens are
already part of output tokens and are not charged twice. Only completed web `search`
actions receive a search-call fee; page-open and find actions do not receive that fee.

Prices are maintained in `stock_digest/costs.py`, checked against
[OpenAI pricing](https://developers.openai.com/api/docs/pricing) on **2026-10-01**. The
initial table covers GPT-6 Luna/Sol/Astra, GPT-6.1 Sol and GPT-4.1/mini/nano, including
dated snapshots, GPT-6 long-context pricing and Flex rates. Unknown models or service
tiers, missing cache/usage details and interrupted requests show a **partial** estimate
instead of being silently treated as free. This is not an invoice: pricing changes,
unreported usage, regional premiums, discounts and other account-specific billing can
make the actual charge differ. Social Sentiment and third-party data-provider fees are
outside this Material + Digest total. Cost reports are per request and are not persisted
as a billing history; no prompts, source text or credentials are stored by the tracker.

The `/api/run`, `/api/digest` and `/api/material` result/error events include a `cost`
object with `known_total_usd`, `total_usd` (`null` when partial), `llm_usd`,
`web_search_usd`, `by_agent`, `complete`, `partial`, `notes` and `pricing_as_of`.
`complete` means that the search has ended, not that every billed token was observable.

The window is always the last 7 days (168 hours ending at the run time); it is not
configurable. Times use the system timezone (CLI) or the browser's (UI), else UTC; text is
English. Tickers resolve through Yahoo Finance: `NVDA`, `BRK.B`, `NASDAQ:MSFT`, `SHOP.TO`
or `TSX:SHOP`, `HKEX:700`. The agent asks for clarification only when Yahoo cannot resolve
the symbol (listing any different companies that share it) or the given exchange does not
match. Funds and other non-company listings are rejected.

**How it works.**

1. **Resolve and profile.** The ticker is resolved to the issuer, and one search builds a
   profile: aliases, brands, subsidiaries, and investor-relations and newsroom pages.
   Successful profiles are reused for seven days, keyed by issuer identity, model and prompt.
2. **Discovery.** Fifteen searches run on every request, up to eight at once:
   - one broad search and one restricted to the company's domains, newswires and regulators;
   - two outlet searches covering Reuters/Bloomberg and CNBC/WSJ/FT;
   - four grouped topic searches covering earnings/capital, deals/contracts/partnerships/
     reported talks, product news in the words it is reported with ("unveils OR launches OR
     delays OR reschedules"), and regulation/legal/incidents/leadership;
   - one dated search per window day for the seven most recent days, such as "NVIDIA news
     September 28, 2026". Topic searches over a heavily covered company return its biggest
     or evergreen stories; dated searches surface each day's coverage.

   Up to 40 dated Yahoo Finance headlines are classified in **one separate model call with
   no web tools**. The compact classifications reference lead indexes; Python attaches the
   provider's URL, title, summary and timestamp, so the model cannot replace that metadata.
   Missing or invalid classifications are disclosed as incomplete feed coverage. If Yahoo
   returns no headlines at all, usually because it is rate-limiting requests, the coverage
   note says the run relied on web search alone.

   At most two additional searches run if time allows, prioritizing failed or malformed
   queries, undated potentially material leads, then sparse coverage (fewer than three
   candidate development groups). Sparse coverage prompts brand/subsidiary and alias
   searches. This is a coverage heuristic, not a guarantee that every important story was
   found.
3. **Gates in Python.** Each extracted candidate must pass four gates:
   - **Subject:** the company is the main subject, or a named counterparty in a
     partnership, contract, investment, acquisition or financing deal. Roundups, lists,
     tags and partner-led product stories fail.
   - **Materiality:** not a default-excluded category (routine releases, SDKs, game
     integrations, patches, awards, analyst commentary, price action, sector news), not
     rated "none", with a "changes ___ through ___" mechanism that is not generic.
     Contracts and partnerships under 0.01% of market value fail on size.
   - **Time:** a publication date from search metadata, the feed, or the source's stated
     date falls in the window, or an in-window update adds a material new fact. Rehashes
     fail. Undated items that discovery judges older than the window are dropped; other
     undated items go to verification.
   - **Evidence:** the URL appeared in the search tool's own metadata or the feed.
4. **Consolidation.** Candidates are grouped by development key and by copies of the same
   article (tracking-parameter variants included). A model then merges groups that
   describe the same development. Distinct milestones of one deal stay separate.
5. **Verification.** Up to 24 developments, eight at once, are each verified by opening
   the most original accessible source (up to five search or page actions). Dated
   in-window developments go first, then undated ones by materiality. The verifier
   confirms the timestamp, status and subject, and writes the entry text. It rejects only
   unsupported, off-subject or old stories; doubts about size become lower scores.
   Paywalled stories are verified from authorized republications, never social posts.
   An included development whose primary source was opened can reuse its verification for
   **30 minutes** when all rediscovered evidence matches. New URLs, timestamps, summaries,
   classifications or source snippets invalidate the match; ordering, exact duplicate
   candidates and URL tracking parameters do not. Matches also require the same issuer,
   model, verifier prompt, language, timezone and window length. The sliding window is
   rechecked by the gates on every run. Failures, exclusions and search-only fallbacks are
   not cached. Conservative matching can miss the cache when model wording changes.
6. **Re-gating and ranking.** The gates run again on the verified reading. Reported items
   keep a "— reported" label and attribution: a named outlet ("Reuters reported"),
   "reportedly" or "according to", or else a "Reported by …" prefix. Wording that turns a
   report into a confirmed outcome excludes the entry. Entries are ranked by magnitude,
   directness, novelty, evidence quality and status, with recency as the tiebreaker.

When a source cannot be opened, an entry may still appear from its search-result content,
from any source except social-media posts. It is attributed to the originating outlet,
ranked conservatively (lowest when the source is an aggregator), flagged "Not opened", and
a coverage note is added.

**Date handling.** A date-only stamp is compared by calendar day in the run timezone;
date-only items on the window's first, partial day are accepted.

**Output.** The Markdown table from the specification, with nothing appended: no appendix,
no minor-news section, no methodology. If the search succeeded and nothing qualified, the
digest says so in one line. If every search failed, the run reports a research limitation,
never "no news". The internal record (`--record`) lists every query, every excluded
candidate with its gate and reason, and every development with its rationale, lineages and
duplicates. It also records agent-run counts by stage, cache hits, and each event's original
verification time and whether it was reused. These counts exclude SDK retries and extra
turns, and are not token or billing totals.

**Local cache.** Profiles and verified evidence persist across UI restarts and CLI runs in
`~/.cache/stock-digest/material.sqlite3`. Set `STOCK_DIGEST_CACHE_DIR` in `.env` to choose
another directory, or set it to `off` to disable caching. Delete that file while the app is
stopped to clear it. The cache holds at most 1,000 entries, removes expired entries on
writes, and never extends an entry's expiry on a cache hit. Cache failures fall back to
normal research with a diagnostic. It stores public evidence and model outputs, not API
keys. Discovery always runs afresh; updates not surfaced by discovery may remain unseen
until the verification expires. Simultaneous requests can still duplicate uncached work.

**Limits.**
- Hosted search has no pagination, and coverage is not exhaustive.
- Paywalled sources are never bypassed.
- Model judgment and search results vary between runs, so the same ticker can yield
  different entries. Python rules, not the models, make the final inclusion decisions.
- The wording checks for reported items apply only to English output.
- A material run starts with fifteen discovery calls, at most one profile call, one feed
  classification, two follow-ups and one consolidation, plus up to 24 uncached event
  verifications: **at most 44 agent runs before SDK retries/extra turns**. For example,
  a cached profile, fifteen searches, one feed batch, one consolidation and five new events
  take 22 agent runs with no follow-ups. Reused verifications reduce this further.
  The digest adds its own calls. Latency has not been benchmarked for this search plan;
  keep `STOCK_DIGEST_TIMEOUT` at 360 seconds or more.

The filters were relaxed on 2026-09-30 after a two-result NVDA run. The changes:
- model-stated dates are accepted when search metadata has none;
- "low" materiality and first-day date-only items pass;
- deal counterparties count as the subject;
- the size floor dropped from 0.1% to 0.01% of market value;
- the verifier no longer rejects on importance alone.

Recall was raised with outlet-led and per-day searches and the Yahoo feed. The
lookback selector was removed; the window is fixed at 7 days.

Recall fixes (2026-10-01), after a TSLA run missed Tesla's September 28 postponement of the
Roadster reveal. Live replays of the discovery step showed none of the eight searches surfaced
it, while a dated search and a news-worded product search both did, and it passed every gate:
- the seven per-day searches, dropped when the plan was cut to eight, are back;
- the product search uses news verbs, and contracts and partnerships moved to the deals search;
- a prompt example listed categories as "…, or excluded: …", so the model wrote
  "excluded: analyst_commentary". Those candidates failed validation and the follow-up
  searches were spent retrying them. The example now lists bare values, and the parser
  strips the label;
- yfinance returns an empty list rather than an error when Yahoo answers HTTP 429. An
  empty feed now adds a coverage note.

Tests: `tests/test_material.py` covers the specification's acceptance criteria offline
(deduplication of copies and tracking URLs, two developments in one article, roundups and
partner-led launches versus counterparty deals, routine items, refreshed dates, reported
status, syndication, milestones, relative size, empty state versus failure, grounding,
feed leads, tickers, timezones and boundaries). `tests/test_web.py` covers the streaming
endpoint, including that the window cannot be changed.

Live checks (2026-09-30), after relaxing the filters: two consecutive NVDA runs (CLI and
UI endpoint, about 2.5 to 3 minutes each) returned the same four developments:
- the $150B buyback authorization (NVIDIA investor relations);
- China weighing permission for Alibaba and ByteDance to buy NVIDIA chips (reported,
  Reuters citing The Information);
- NVIDIA's talks with insurers on chip-backed loans (reported talks, FT via an authorized
  republication);
- the Open Agent Safety Platform launch.

The verifier rejected about a dozen older stories that searches resurfaced. Examples: the
Hugging Face acquisition (Sep 3), Q2 results (Aug 26) and the Meta partnership (Feb 17).
The SpaceX partnership was announced Aug 24, 2026, outside the 7-day window.

The UI tab was exercised in headless Chromium at desktop and phone widths: loading,
result, empty state, research failure, invalid input, tab switching during a run, and
Copy Markdown, with no page errors.

## Social Sentiment tab

The third UI tab lists individual social posts about the selected ticker that were
published in the last 48 hours, newest first. Each post shows its source, author,
publication time, full returned text, engagement and a link to the original. It uses the
same search box as the other tabs. Opening the tab, or searching while it is open, loads
the posts; **Refresh** fetches a new 48-hour window.

**Source.** The social code is ported from
[AnomalyLuck/Sentiment-Search](https://github.com/AnomalyLuck/Sentiment-Search), branch
`feature/x-social-sources`, commit `c42585297226030962a1b7c9fef75110ab76d01f`. It lives
in `stock_digest/social/`. The per-platform scrapers are copied byte-for-byte:
`x_source.py`, `reddit_source.py`, `stocktwits_source.py`, `hackernews_source.py`,
`youtube_source.py`, `seeking_alpha_source.py`, `social_types.py`, `cache.py` and
`security.py`. The other modules differ from the source as follows:
- `aliases.py` calls OpenAI (`OPENAI_API_KEY`, model `SOCIAL_ALIAS_MODEL` or
  `OPENAI_VERIFY_MODEL`) instead of Anthropic. Without a key it falls back to the Yahoo
  company name plus static seeds, as the original does.
- `config.py` reads only the social settings, from the environment or `.env`.
- `retrieval.py` keeps only the window table (adding `48h`) and URL canonicalization.
- `social.py` replaces the source's orchestration and its `/api/social` handler:
  - Tickers resolve through Yahoo Finance; Finnhub is not used.
  - Each request fixes one UTC interval: it ends at the request time and starts exactly
    48 hours earlier, boundaries included. Every post is checked against the interval by
    publication time. Posts outside it are counted and not shown.
  - Duplicates are removed by provider and post ID, taken from the post URL. Distinct
    posts with identical text are kept; the source's fuzzy text dedupe is not used.
  - The source's merged top-150 ranking and per-source caps are not applied. Every post
    the scrapers return is listed.
  - There is no whole-response cache. The X and Reddit caches inside the scrapers still
    apply.

**Endpoint.** `GET /api/social?query=NVDA` takes an optional `window=48h` (the only
accepted value) and an optional `sources=x,reddit,...` allowlist. It returns the
source's fields: `query`, `ticker`, `company`, `window`, a `sources` status map, and
`posts` with the original post fields. It adds:
- `window_start`, `window_end` and `retrieved_at`;
- `providers`, with each source's state, returned and shown counts, and limits;
- `excluded` counts;
- per post: `id`, `sentiment` and labelled `engagement`.

Errors return an HTTP status with `{"detail": ...}`: 400 for invalid input, 404 for an
unresolvable ticker, 502 or 504 for a failed or timed-out retrieval. Provider
credentials stay on the server. Provider error text is redacted, never forwarded.

**Configuration** (see `.env.example`). Each source is optional; a missing key disables
it and the tab says which settings are missing.

| Source | Settings | Notes |
| --- | --- | --- |
| X | `X_API_PROVIDER=twitterapi`, `X_API_KEY` | twitterapi.io, $0.15 per 1,000 tweets returned; scraper-backed, not licensed by X |
| Reddit | `REDDIT_API_KEY` | redditapis.com, $0.002 per read; daily call budget `REDDIT_DAILY_CALL_BUDGET` |
| YouTube | `YOUTUBE_API_KEY` | Data API v3; one search page uses 100 of 10,000 daily units |
| StockTwits, Hacker News | none | public APIs |
| Seeking Alpha | — | no provider implemented in the source |

**Coverage.** The scrapers read a bounded slice of each platform, so the tab never claims
complete coverage. It names the source that hit its limit:
- X: at most 15 posts (2 per account), chosen by the source's ranking from up to 2 pages
  of X's Top search. Results are reused for 15 minutes to limit cost.
- Reddit: at most 90 comments (7 per thread) from up to 16 threads in 13 subreddits.
  Comment text is a 400-character excerpt; the link opens the full comment.
- StockTwits: only the newest page of up to 30 messages. For busy tickers this covers
  minutes, not 48 hours.
- Hacker News: up to 100 newest stories and comments.
- YouTube: up to 50 most-viewed videos, English only, with at least 10,000 views and
  20 comments. The text shown is the video description.

The scrapers' quality filters also apply: X follower, length and promotion filters, and
Reddit comment-length and bot filters. The scrapers do not report how many posts these
filters drop. Results are delivered in one response and rendered 50 at a time with
**Show 50 more**. A source filter narrows the feed.

**States.** The tab distinguishes:
- loading;
- results;
- a confirmed empty result ("No social posts found for NVDA in the last 48 hours.");
- partial results when some sources fail;
- a failed retrieval when every source fails, which is never shown as empty;
- missing configuration;
- request errors, with a retry.

During a refresh, the existing posts stay visible, labelled as previous results. A
failed refresh keeps them and says so. A response that arrives after the ticker changed
is discarded, so posts never appear under the wrong ticker.

**Sentiment.** None of the source's scrapers supplies a sentiment label or score, so
every post shows "Not scored", never neutral. StockTwits returns author-tagged
Bullish/Bearish labels that the copied scraper does not keep. Adding them would be a
small change to `stocktwits_source.py`.

**Engagement.** Counts are shown as the scrapers produce them. X folds reposts and quotes
into likes, and StockTwits folds reshares into likes, so the labels say so. Hacker News
and YouTube record unreported counts as zero, so zeros from those sources are omitted.

**Live check (2026-10-01).** The configured keys were used. An NVDA request returned 160
posts in about 7 seconds: 15 from X, 30 from StockTwits, 23 from Reddit, 82 from Hacker
News and 10 from YouTube. Every post was inside the fixed window, ordered newest first,
with a unique provider ID. No credential appeared in the response. The tab was also
exercised in headless Chromium at desktop and phone widths:
- loading, Show more, the source filter and tab switching without refetching;
- arrow-key tab navigation and Refresh with previous results labelled;
- a failed refresh, and out-of-order responses for rapid ticker changes;
- the empty, partial, all-failed, not-configured and error states.

## Output

The UI follows the reading order of a brokerage single-stock digest (it is not
affiliated with or branded as any brokerage product):

1. Ticker and price, colored by direction, with the signed change, session label, and
   any after-hours or pre-market quote.
2. A dot-matrix band (green up, orange-red down) and "Updated … ago".
3. Small badges for the publishers the digest cites.
4. A short green headline in plain English, such as "NVIDIA climbs after announcing
   $150 billion buyback expansion". Prices stay in the header; the headline may use
   the token `{move}`, which renders as "up 1.68%".
5. Two to four sections with short headings ("Record buyback", "Sector momentum"),
   each a 2 to 4 sentence paragraph in news-brief style with small citation numbers.
   An earnings section, when a release is due within 30 days, adds figure tiles for
   consensus EPS and revenue and the options-implied move.
6. Compact tags where they apply: "Unverified" (not fully substantiated), "Time
   unknown" (same-day report of unknown time), "Unconfirmed report". Hover for details.
7. Collapsible panels: market data (previous close, open and gap, volume and relative
   volume, same-session SPY, QQQ, sector, industry and peer moves), recent headlines,
   and numbered sources with publisher, title and date.
8. Fine print with price, news and generation times, reader-facing coverage notes,
   the AI disclosure, and a link to the run trace.

Terminal mode prints the same content as text.

## Market data and time

| yfinance call | Purpose |
| --- | --- |
| `Ticker.get_info()` | Identity, exchange, sector/industry, website, extended-hours quote, official close stamp |
| `Ticker.history("1d")` | Closes, open, daily volume, 20-session average volume |
| `Ticker.history("1m")` | Eligible minute bars during an open session; benchmark bars |
| `Ticker.get_news()` | Titles, summaries, publishers, URLs and publication times |
| `get_earnings_dates`, `earnings_estimate`, `revenue_estimate`, `eps_trend`, `eps_revisions`, `earnings_history` | Next/last earnings with times, consensus, revisions, prior actuals |
| `quarterly_income_stmt` | Reported quarterly revenue for comparisons and results |
| `upgrades_downgrades`, `analyst_price_targets` | Rating and price-target changes; target summary |
| `get_sec_filings()` or SEC EDGAR submissions | Recent filings (EDGAR adds item codes and acceptance times) |
| `insider_transactions` | Recent insider purchases and sales (transaction date only) |
| `options`, `option_chain()` | At-the-money straddle for the first expiry covering the release |
| `yf.Industry(...).top_companies` | Industry peers |

This is an unofficial Yahoo integration for personal use; availability, fields and
rate limits can change. Each structured category fails independently and becomes a
diagnostic, never a crash. yfinance's on-disk caches are disabled through private
names in the pinned 1.7 series; a later release that renames them only re-enables
yfinance's default caches.

Prices use Yahoo's split-adjusted `Close` (never dividend-adjusted `Adj Close`),
regular session only, with Decimal arithmetic before display rounding. The app never
reports a bar newer than now minus a 15-minute cutoff. The XNYS calendar supplies
sessions, holidays and early closes:

- During a session, the price is the latest eligible completed minute bar. In the
  first 16 minutes after the open, the previous completed session is shown with a
  note. In the first 15 minutes after the close, the last eligible minute bar is shown.
- After the cutoff, the price is the completed session close. Yahoo leaves the day's
  daily close empty for hours after the bell, so the quote's regular-market price,
  stamped at the closing print, is used then, with a diagnostic.
- A stock with no eligible minute bar today falls back to the previous completed
  session. An unusually old last bar is shown with a note instead of failing.
- Sunday compares Friday with Thursday. A weekend close is not stale.

Extended-hours quotes are shown separately, only after a completed session and
before the next open, against that session's close.

## Research and evidence

Seven focused queries run, at most three at once, each limited to one hosted search
(`max_tool_calls=1`, `max_turns=1`) with one automatic retry (in the UI's combined run
only queries 1, 3, 4 and 7 run and the 7-day expansion is skipped; the material news
agent supplies the rest, see above):

1. Stock news today.
2. Press releases, restricted to the company's own domain, the major newswires and
   sec.gov through the search tool's domain filter.
3. Analyst upgrades, downgrades and price targets.
4. Earnings date, guidance, buybacks and dividends (45-day search for announcements).
5. Acquisitions, mergers and deal talks.
6. Reported talks and anonymous-source reporting.
7. Industry and market context.

If fewer than three news findings survive, one more query expands the window from
72 hours to 7 days, disclosed in coverage. When an earnings release is due within 30
days, targeted searches run only for pieces Yahoo could not supply (estimates,
comparable history, options), and only when at least 150 seconds of the run deadline
remain. At most eleven searches run in total.

Research returns a cited response with a JSON block, parsed and validated per finding.
Source URLs come only from SDK search metadata and citation annotations. Each finding
carries a confirmation status, a catalyst type and a move-relevance hint. Yahoo
headlines must visibly name the company: names are matched without legal suffixes
("Walt Disney", "Bank of America"), a short list of brand aliases covers cases such as
Google, and the ticker counts only as `$A`, `(A)`, `NYSE: A` or, for tickers of three
or more letters that are not common words, as a bare symbol.

**Dates.** A research date counts only when the quoted dateline shows it: full dates in
common formats, year-less "Sep 28" (resolved to its most recent occurrence), or "3 hours
ago" relative to retrieval. A model-written clock time counts only when the dateline
shows the same time and a compatible timezone; otherwise only the date is kept. A search
result whose headline matches a Yahoo feed item inherits Yahoo's timestamp. Research
cannot overwrite provider timestamps. Findings with only an update date are kept.

**Timing policy.** Every item is classified against the price observation:

- published before it: may be presented as a reported or possible explanation;
- same-day with an unknown time: may "coincide with" or "may be linked to" the move,
  labeled "(same-day report; exact publication time unknown)";
- published after it: later news, never an explanation.

A date-only item from a later day, such as a weekend article after a Friday close, is
classified as later, not unknown. Certain-causation wording is rejected in headlines.

**Deduplication.** News is deduplicated by source and by headline across all queries;
syndicated copies keep the most cautious confirmation status. Earnings-history research
older than the news window may be cited only in the earnings preview.

## Earnings preview

Yahoo's structured data is preferred: consensus revenue and EPS for the quarter being
reported with analyst counts and ranges, year-ago figures from the same Yahoo records,
the last reported quarter's EPS and revenue for QoQ, 30/90-day estimate trends and
revision counts, and an at-the-money straddle computed from the option chain (bid/ask
midpoints, or recent last trades) for the first expiry that includes the release.
The straddle prices movement in either direction through expiry, not only the
earnings reaction, and says so. An issuer announcement found by research outranks
Yahoo's calendar; an estimated calendar date ranks last. For financial companies,
consensus revenue versus reported total revenue carries a comparability caveat.

Research extractions are a fallback, restricted to the quarter after the latest
reported actual (or, without history, estimates the source ties to the release date).
Revenue below $1 million or more than ten times off Yahoo's consensus is rejected as a
unit error. Python computes every comparison; zero or negative baselines show a dollar
change instead of a percentage.

## Writing, checks and review

The writer receives market data, evidence packets, the source registry, the earnings
context, the candidate move explanations and a catalyst status. When no candidate
exists, the first topic states that no clear company-specific catalyst was identified
in the sources checked. Python validates citations and cutoff eligibility, repairs the
headline template (keeping the writer's context), and flags inline citations and
misplaced price tokens.

The model reviewer runs by default (`OPENAI_VERIFY_MODEL`). It checks support, numbers,
citations, timing, attribution, uncertainty, recommendations and contradictions. Support
gaps become visible "Unsupported claim" labels; other issues trigger one revision and a
second review. Claims still flagged after that are removed individually; the narrative
is withheld only when nothing publishable remains or an unlocated serious issue is
raised, and then retrieved headlines (or prices only) are shown with a note. A writer,
reviewer or revision API failure never discards retrieved headlines. With `--no-verify`,
only the basic Python checks run and flagged claims are removed the same way.

## Limits

OpenAI research calls time out after 60 seconds and writer/reviewer calls after 90.
The run deadline defaults to 480 seconds; later stages are skipped (earnings follow-ups,
the review, the revision) when too little time remains, and every skip is disclosed.
Structured Yahoo data is fetched in the background while research runs. SDK tracing is
enabled and the CLI prints the trace link last. Responses use `store=False`.

Not included: technical indicators, recommendations, historical mode, caching,
persistence, exports or deployment. The app does not claim complete news coverage,
full-article retrieval, or access to customer trading activity.

## Review status and gaps

UI (2026-09-28): the browser UI replaced the terminal as the default interface. Live
NVDA and JPM runs went through the streaming endpoint (about 80 and 90 seconds); JPM's
review flagged one earnings sentence, the revision did not fix it, and that sentence was
removed. Rendering was checked in headless Chromium at phone and desktop widths for the
empty, loading, up-move, down-move and earnings states, with no page errors.

Audit fixes (2026-09-28): every item in `AUDIT.md` was implemented, plus a new bug
found during the work: after the close, Yahoo's daily bar for the session has no close,
which made every evening run fail with "invalid daily close". The offline suite (53
tests) covers each audit bug with a regression case, plus mocked end-to-end pipeline
runs. Live runs, both after the close with model review on:

- NVDA: 7/7 queries, 21 usable findings including 4 structured records, first-pass
  review approval, 83 seconds. The digest identified the $150 billion buyback
  announced before the open, compared the move with SPY, QQQ, XLK, SMH and three peers,
  and showed the after-hours quote, gap and relative volume.
- JPM: earnings due Oct 13. The issuer's confirmed announcement outranked Yahoo's
  calendar; the preview showed consensus, QoQ/YoY, revisions and a 5.2% straddle
  through the Oct 16 expiry. 59 seconds.

Not yet exercised live: an intraday run, the pre-market and first-16-minutes paths,
SEC EDGAR (needs `SEC_USER_AGENT`), and a ticker with a same-week filing or rating change.

The notes below predate the audit fixes and describe earlier behavior.

Direct-news correction (2026-09-28): Yahoo's NVDA news endpoint returned 30 records;
13 passed company-relevance and publication-window checks. A manual headlines-only
render showed dated stories without any writer output. The full CLI then completed
7/7 research queries, retained 14 usable findings including 13 Yahoo headlines,
and published six recent headline links plus narrative sections covering buybacks,
AI-agent software and sector context (exit 0; no verifier or revision). The fallback
for writer API/schema failures was inspected but was not triggered in this live run.
General event dates no longer trigger earnings follow-ups; explicit earnings dates
are required, avoiding dividend/keynote dates taken from earnings-report pages.

Verification disabled by default (2026-09-28): a live `stock-digest NVDA --no-footer`
run completed eight research queries and one writer pass, then published with a
final trace link (exit 0), without verifier or revision calls. One eligible finding
was retained; this check establishes the execution path, not broad news coverage.
The new `--verify` flag restores the earlier optional review/revision flow.
An initial run exposed a missing rumor label; that wording is now added by the
renderer instead of blocking default-mode publication.

Current-news correction (2026-09-28): a live NVDA diagnostic found a September 28
article rejected because its date-only bound was September 29 at noon UTC. A
separate news-listing result lacked a substantiated update date. After correcting
availability bounds and separating the news cutoff, a full NVDA run completed
10/10 queries, retained seven findings without the seven-day fallback, and
published after one revision. The same previously rejected September 28 article
appeared in the digest with the publication-time-unknown/context-only label.
Same-day availability and genuinely future date bounds were manually inspected;
no automated tests or fixtures were added. These checks do not establish exhaustive
news coverage or independent verification of article text.

Headline relaxation (2026-09-28): a live MU run exercised the automatic fallback
when the writer omitted `{move}`. It completed 10 of 11 research queries with two
eligible findings, went through one review/revision, and published the digest
with source URLs and the final trace link (exit 0). Earnings estimates and options
data were unavailable in that run and were explicitly identified as missing.
The formatter repair does not bypass factual, timing, or citation review.

Timeout update (2026-09-28): OpenAI requests now allow 90 seconds and the default
run deadline is 360 seconds, including the local `.env` setting. A live MU run
completed all 10 research queries with five eligible findings, plus writing,
review, revision and re-review, without a request timeout. Publication was still
blocked by the existing headline token/source-1 check after revision; no draft
was published. Specific API error reporting is implemented, but no API failure
occurred during this verification run.

Yahoo migration (2026-09-28): installed the editable package and checked dependency
compatibility. Live MU and BRK-B market retrieval returned validated intraday
snapshots, previous-session comparisons, timestamps and Yahoo source URLs. SPY
was rejected as an unsupported ETF. Both CLI help entry points work. A full MU
run fetched Yahoo prices, completed 5 of 11 research queries with one eligible
finding, then exceeded the 180-second deadline during writing after several
OpenAI request failures. No digest was published; the final trace link was printed.
The completed-session Yahoo branch has not yet been manually exercised outside
market hours. Earlier observations below predate the provider migration.

No automated tests, fixtures, evaluations, or CI were created, as requested.
Manually reviewed: editable installation, both help entry points, missing-input,
invalid-symbol and missing-credential errors (exit 2), the interactive ticker
prompt, SDK structured-output schema
construction, module imports, and the Sunday calendar resolving to Friday
with Thursday as the comparison session. Live NVDA initially completed price-only;
investigation of MU exposed missing citations in strict research JSON, an overly
restrictive raw-metadata gate, and a headline check that also rejected event dates.
After correcting those, a live MU run completed with five successful searches,
three eligible findings, and a narrative accepted by the verifier. Its earnings
announcement was also checked against Micron's investor-relations release. The
one-revision and blocked-output paths were observed during debugging. No full
article retrieval or independent corroboration of all research is claimed.
After hardening parsing for recurring price-only output, another live MU run with
`--no-footer` parsed all five research responses, retained the confirmed earnings
event, passed verification, and ended its output at the cited source URLs.
The expanded catalyst search was also manually run on MU: all seven initial
queries completed, eligible findings reached the writer, and the digest passed
runtime checks and the verifier. This does not establish complete news coverage.
Strict timestamp/content eligibility and
paywalls can reduce coverage. The app does not claim complete news coverage or
access to customer trading activity. There are no technical indicators,
recommendations, historical mode, caching, persistence, exports, or deployment.
