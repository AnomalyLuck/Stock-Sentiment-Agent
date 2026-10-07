"""The combined UI run: material news and the digest together, with fakes for both pipelines."""
import asyncio
from datetime import timedelta

import pytest
from agents import set_tracing_disabled

import stock_digest.manager as manager
import stock_digest.material.identity as identity
import stock_digest.material.pipeline as pipeline
import stock_digest.news as news
import stock_digest.runner as runner
from conftest import AS_OF, make_market, make_week
from stock_digest.material.models import Issuer, MaterialResult
from stock_digest.models import Publication
from stock_digest.runner import Settings, run_combined


class Gone(Exception):
    """Stands in for the web layer's closed-connection signal."""


@pytest.fixture
def fakes(monkeypatch):
    set_tracing_disabled(True)                       # no exporter threads in the offline suite
    monkeypatch.setattr(runner, "enable_tracing", lambda key: None)
    log = []

    def fake_resolve(ticker, exchange=None):
        log.append(("resolve", ticker))
        symbol, _ = identity.parse_symbol(ticker, exchange)
        return Issuer(ticker=identity.display_ticker(symbol), symbol=symbol, exchange="NasdaqGS", company="NVIDIA Corporation")

    async def fake_week(ticker, company):
        log.append(("fetch", ticker, company))
        return make_week()

    async def fake_material(request, api_key, models, stage, *, deadline=None, run_config=None, issuer=None, news=None):
        stage("resolving")
        try:
            resolved = await issuer
            await news
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            log.append("material cancelled")
            raise
        log.append(("material", request.ticker, request.timezone, request.max_results))
        return MaterialResult(issuer=resolved, run_at=AS_OF, window_start=AS_OF - timedelta(days=7), window_end=AS_OF,
                              timezone=request.timezone, hours=168, outcome="none_found")

    async def fake_digest(ticker, api_key, models, stage, *, verify=True, deadline=None, news=None):
        stage("fetching prices")
        week = await news                             # the same fetch the material run reads
        log.append(("digest", ticker, verify, week.providers))
        return Publication(market=make_market(ticker=ticker), generated_at=AS_OF, news_as_of=AS_OF, digest=None,
                           sources=[], coverage=[])

    monkeypatch.setattr(identity, "resolve_issuer", fake_resolve)
    monkeypatch.setattr(news, "fetch_week", fake_week)
    monkeypatch.setattr(pipeline, "run_material", fake_material)
    monkeypatch.setattr(manager, "run_digest", fake_digest)
    return log


def collect():
    stages, outcomes = [], []
    return (stages, outcomes, lambda kind, message: stages.append((kind, message)),
            lambda kind, result, error, url: outcomes.append((kind, type(result).__name__ if result else None,
                                                              type(error).__name__ if error else None, url)))


def test_one_fetch_feeds_both_sides_and_the_digest_does_not_wait_for_material(fakes):
    stages, outcomes, stage, done = collect()
    url = run_combined("brk-b", Settings(api_key="sk-test", models={}, timeout=5, verify=False), stage, done,
                       timezone="Asia/Tokyo", max_results=3)
    assert url.startswith("https://platform.openai.com/traces/trace?trace_id=")
    assert outcomes == [("digest", "Publication", None, url), ("material", "MaterialResult", None, url)]
    assert sorted(stages) == [("digest", "fetching prices"), ("material", "resolving")]
    assert fakes == [("resolve", "brk-b"), ("fetch", "BRK.B", "NVIDIA Corporation"),
                     ("digest", "BRK.B", False, "Finnhub and Google News"), ("material", "brk-b", "Asia/Tokyo", 3)]


def test_a_non_us_listing_fails_only_the_digest(fakes):
    stages, outcomes, stage, done = collect()
    run_combined("TSX:SHOP", Settings(api_key="sk-test", models={}, timeout=5), stage, done)
    assert outcomes[0][:3] == ("digest", None, "InputError") and outcomes[1][:3] == ("material", "MaterialResult", None)
    assert fakes == [("resolve", "TSX:SHOP"), ("fetch", "SHOP.TO", "NVIDIA Corporation"),
                     ("material", "TSX:SHOP", "UTC", None)]       # run_digest never started


def test_abort_exceptions_cancel_the_other_side(fakes):
    def stage(kind, message):
        if kind == "digest":
            raise Gone
    outcomes = []
    with pytest.raises(Gone) as info:
        run_combined("NVDA", Settings(api_key="sk-test", models={}, timeout=5), stage,
                     lambda *args: outcomes.append(args), abort=(Gone,))
    assert outcomes == [] and "material cancelled" in fakes and info.value.trace_url


def test_the_deadline_ends_the_run(fakes):
    outcomes = []
    with pytest.raises(TimeoutError) as info:
        run_combined("NVDA", Settings(api_key="sk-test", models={}, timeout=0.01), lambda kind, message: None,
                     lambda *args: outcomes.append(args))
    # The digest no longer waits for material, so it may finish first; material never reports.
    assert [kind for kind, *_ in outcomes] in ([], ["digest"]) and "material cancelled" in fakes
    assert info.value.trace_url
