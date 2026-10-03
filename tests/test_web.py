import json
import threading
import urllib.request

import pytest

from conftest import AS_OF, make_market
from stock_digest.market import DigestError
from stock_digest.models import Publication
from stock_digest.runner import Settings
from stock_digest.web import DigestHandler, make_server


@pytest.fixture
def server(monkeypatch):
    calls = []

    def fake_run(ticker, settings, stage):
        calls.append(ticker)
        if ticker == "FAIL":
            raise DigestError("Yahoo Finance rate limit reached.")
        stage("Researching 7 focused queries…")
        return Publication(market=make_market(ticker=ticker), generated_at=AS_OF, news_as_of=AS_OF, digest=None,
                           sources=[], coverage=["Searches completed with no relevant findings."]), "https://example.com/trace"

    monkeypatch.setattr(DigestHandler, "run", staticmethod(fake_run))
    httpd = make_server(Settings(api_key="sk-test", models={}, timeout=60), port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", calls
    httpd.shutdown()
    httpd.server_close()


def events(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        assert response.headers["Content-Type"].startswith("application/x-ndjson")
        return [json.loads(line) for line in response.read().decode().splitlines() if line.strip()]


def test_page_is_served_with_a_strict_policy(server):
    base, _ = server
    with urllib.request.urlopen(base + "/", timeout=10) as response:
        body = response.read().decode()
        assert "Stock Digest" in body and "/api/run" in body
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert "img-src 'self' data: https://i.ytimg.com;" in response.headers["Content-Security-Policy"]  # thumbnails
        assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_stream_sends_progress_then_result(server):
    base, calls = server
    stream = events(base + "/api/digest?ticker=brk-b")
    assert [event["type"] for event in stream] == ["stage", "result"]
    assert calls == ["BRK.B"]  # normalized before running
    digest = stream[-1]["digest"]
    assert digest["ticker"] == "BRK.B" and digest["notice"].startswith("Searches completed")
    assert digest["trace_url"] == "https://example.com/trace"


def test_invalid_ticker_and_failures_are_safe_errors(server):
    base, calls = server
    assert events(base + "/api/digest?ticker=not%20a%20ticker")[0]["type"] == "error"
    assert calls == []
    failure = events(base + "/api/digest?ticker=FAIL")
    assert len(failure) == 1
    assert failure[0]["type"] == "error" and failure[0]["message"] == "Yahoo Finance rate limit reached."
    assert failure[0]["trace_url"] is None and failure[0]["cost"]["complete"]


def test_material_stream_validates_options_and_returns_the_view(monkeypatch, server):
    from stock_digest.material.models import Issuer, MaterialResult

    seen = []

    def fake_material(request, settings, stage):
        seen.append(request)
        stage("Searching 13 queries…")
        issuer = Issuer(ticker="MSFT", symbol="MSFT", exchange="NasdaqGS", company="Microsoft Corporation")
        return MaterialResult(issuer=issuer, run_at=AS_OF, window_start=AS_OF, window_end=AS_OF,
                              timezone=request.timezone, hours=request.hours, outcome="none_found"), "https://example.com/t"

    monkeypatch.setattr(DigestHandler, "run_material", staticmethod(fake_material))
    base, _ = server
    stream = events(base + "/api/material?ticker=NASDAQ:MSFT&tz=Asia/Tokyo&hours=24&max=3")
    assert [event["type"] for event in stream] == ["stage", "result"]
    view = stream[-1]["material"]
    assert view["empty_message"].startswith("No qualifying material") and "Asia/Tokyo" in view["window"]
    assert view["markdown"].startswith("**MSFT — material company developments**")
    assert (seen[0].hours, seen[0].max_results, seen[0].timezone) == (168, 3, "Asia/Tokyo")  # window is fixed
    for bad in ("ticker=NVDA&tz=Mars/Base", "ticker=NVDA&max=0", "ticker=NVDA&max=x", "ticker=FOO:BAR"):
        assert events(base + "/api/material?" + bad)[0]["type"] == "error"
    assert len(seen) == 1


def test_combined_stream_tags_events_and_ends_each_kind_once(monkeypatch, server):
    from stock_digest.market import InputError
    from stock_digest.material.models import Issuer, MaterialResult

    seen = []

    def fake_combined(raw, settings, stage, done, *, timezone, max_results, abort):
        seen.append((raw, timezone, max_results, abort))
        url = "https://example.com/trace"
        stage("material", "Searching…")
        issuer = Issuer(ticker="NVDA", symbol="NVDA", exchange="NasdaqGS", company="NVIDIA Corporation")
        done("material", MaterialResult(issuer=issuer, run_at=AS_OF, window_start=AS_OF, window_end=AS_OF, timezone=timezone,
                                        hours=168, outcome="none_found"), None, url)
        stage("digest", "Writing the digest…")
        if raw == "SHOP.TO":
            done("digest", None, InputError("Enter one US stock symbol."), url)
        elif raw == "SLOW":
            raise TimeoutError            # the run deadline: the digest never finished
        else:
            done("digest", Publication(market=make_market(), generated_at=AS_OF, news_as_of=AS_OF, digest=None, sources=[],
                                       coverage=["Searches completed with no relevant findings."]), None, url)
        return url

    monkeypatch.setattr(DigestHandler, "run_combined", staticmethod(fake_combined))
    base, _ = server
    stream = events(base + "/api/run?ticker=nvda&tz=Asia/Tokyo&max=3")
    assert [(e["type"], e["kind"]) for e in stream] == [("stage", "material"), ("result", "material"),
                                                        ("stage", "digest"), ("result", "digest")]
    assert stream[1]["material"]["empty_message"].startswith("No qualifying") and stream[3]["digest"]["ticker"] == "NVDA"
    assert seen[-1] == ("nvda", "Asia/Tokyo", 3, (__import__("stock_digest.web", fromlist=["ClientGone"]).ClientGone,))
    # A listing the digest does not cover fails only the digest.
    stream = events(base + "/api/run?ticker=SHOP.TO")
    assert [(e["type"], e["kind"]) for e in stream][1::2] == [("result", "material"), ("error", "digest")]
    assert stream[-1]["message"] == "Enter one US stock symbol." and stream[-1]["trace_url"] == "https://example.com/trace"
    # The deadline closes whichever kind is still open.
    stream = events(base + "/api/run?ticker=SLOW")
    assert [(e["type"], e["kind"]) for e in stream] == [("stage", "material"), ("result", "material"),
                                                        ("stage", "digest"), ("error", "digest")]
    assert stream[-1]["message"].startswith("Run deadline exceeded")
    # Invalid input ends both kinds before any run starts.
    stream = events(base + "/api/run?ticker=NVDA&max=0")
    assert [(e["type"], e["kind"]) for e in stream] == [("error", "digest"), ("error", "material")]
    assert len(seen) == 3


def test_cost_updates_after_each_result_and_survives_a_deadline(monkeypatch, server):
    from stock_digest.costs import active_costs
    from stock_digest.material.models import Issuer, MaterialResult

    def fake_combined(raw, settings, stage, done, **kwargs):
        tracker = active_costs.get()
        tracker.record("material", {
            "id": "resp_material", "model": "gpt-6-luna", "output": [],
            "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                      "output_tokens": 1000},
        }, {})
        issuer = Issuer(ticker=raw, symbol=raw, exchange="NasdaqGS", company="Example Inc.")
        done("material", MaterialResult(issuer=issuer, run_at=AS_OF, window_start=AS_OF, window_end=AS_OF,
                                        timezone="UTC", hours=168, outcome="none_found"), None, "https://example.com/trace")
        if raw == "SLOW":
            tracker.start("digest")
            raise TimeoutError
        tracker.record("digest", {
            "id": "resp_digest", "model": "gpt-6-luna", "output": [],
            "usage": {"input_tokens": 2000, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                      "output_tokens": 1000},
        }, {})
        done("digest", Publication(market=make_market(), generated_at=AS_OF, news_as_of=AS_OF, digest=None,
                                    sources=[], coverage=[]), None, "https://example.com/trace")

    monkeypatch.setattr(DigestHandler, "run_combined", staticmethod(fake_combined))
    base, _ = server
    for ticker in ("NVDA", "MSFT"):  # new ticker starts at zero, despite reused fake response IDs
        stream = events(base + f"/api/run?ticker={ticker}")
        initial, final = [event["cost"] for event in stream]
        assert not initial["complete"] and initial["total_usd"] == pytest.approx(.0006)
        assert final["complete"] and final["total_usd"] == pytest.approx(.0013)
        assert set(final["by_agent"]) == {"material", "digest"}
    stream = events(base + "/api/run?ticker=SLOW")
    assert stream[-1]["type"] == "error" and stream[-1]["cost"]["complete"]
    assert stream[-1]["cost"]["partial"] and stream[-1]["cost"]["known_total_usd"] == pytest.approx(.0006)


def test_runs_beyond_the_limit_are_refused_until_a_slot_frees(monkeypatch):
    from stock_digest.web import BUSY

    calls, started, release = [], threading.Event(), threading.Event()

    def fake_combined(raw, settings, stage, done, **kwargs):
        calls.append(raw)
        if raw == "HOLD":
            started.set()
            release.wait(10)
        elif raw == "BOOM":
            raise DigestError("Yahoo Finance rate limit reached.")
        return "https://example.com/trace"

    monkeypatch.setattr(DigestHandler, "run_combined", staticmethod(fake_combined))
    monkeypatch.setattr(DigestHandler, "run", staticmethod(lambda ticker, settings, stage: calls.append(ticker)))
    httpd = make_server(Settings(api_key="sk-test", models={}, timeout=60, max_runs=1), port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        holder = threading.Thread(target=events, args=(base + "/api/run?ticker=HOLD",))
        holder.start()
        assert started.wait(10)
        stream = events(base + "/api/run?ticker=NVDA")
        assert [(e["type"], e["kind"], e["message"]) for e in stream] == [("error", "digest", BUSY),
                                                                          ("error", "material", BUSY)]
        assert stream[-1]["cost"]["complete"]
        assert [(e["type"], e["message"]) for e in events(base + "/api/digest?ticker=NVDA")] == [("error", BUSY)]
        release.set()
        holder.join(10)
        events(base + "/api/run?ticker=BOOM")      # a failed run frees its slot too
        events(base + "/api/run?ticker=NVDA")
        assert calls == ["HOLD", "BOOM", "NVDA"]
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()


def test_the_run_limit_is_read_from_the_environment(monkeypatch, tmp_path):
    from stock_digest.market import InputError
    from stock_digest.runner import load_settings

    monkeypatch.chdir(tmp_path)                       # no .env
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("STOCK_DIGEST_MAX_RUNS", raising=False)
    assert load_settings().max_runs == 3
    monkeypatch.setenv("STOCK_DIGEST_MAX_RUNS", " 5 ")
    assert load_settings().max_runs == 5
    for bad in ("0", "-1", "two", "1.5"):
        monkeypatch.setenv("STOCK_DIGEST_MAX_RUNS", bad)
        with pytest.raises(InputError, match="STOCK_DIGEST_MAX_RUNS"):
            load_settings()
