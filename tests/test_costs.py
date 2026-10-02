"""Offline billing math and HTTP instrumentation; never contacts OpenAI."""
import asyncio
import importlib
import json

import pytest
from openai import DefaultAsyncHttpxClient

# Mock with the same HTTP backend as the installed SDK (httpx or httpx2).
httpx = importlib.import_module(next(c.__module__.split(".")[0] for c in DefaultAsyncHttpxClient.__mro__
                                    if c.__name__ == "AsyncClient"))

from stock_digest.costs import CostTracker, active_costs, tracking_costs, usage_client


def response(model="gpt-6-luna", id="resp_test", **overrides):
    data = {"id": id, "object": "response", "created_at": 1, "status": "completed", "model": model,
            "service_tier": "default", "output": [],
            "usage": {"input_tokens": 10000, "input_tokens_details": {"cached_tokens": 2000, "cache_write_tokens": 1000},
                      "output_tokens": 1000, "output_tokens_details": {"reasoning_tokens": 600}, "total_tokens": 11000}}
    data.update(overrides)
    return data


def search(action="search", status="completed"):
    return {"type": "web_search_call", "status": status, "action": {"type": action, "queries": ["one", "two"]}}


def test_cached_and_written_tokens_are_partitioned_reasoning_is_not_double_counted():
    tracker = CostTracker()
    tracker.record("material", response(output=[search(), search("open_page"), search("find_in_page"), search()]), {})
    report = tracker.snapshot(complete=True)
    assert report["llm_usd"] == pytest.approx(0.001345)
    assert report["web_search_usd"] == 0.02
    assert report["total_usd"] == pytest.approx(0.021345)
    assert report["by_agent"]["material"]["web_search_calls"] == 2  # actions, not queries or page opens
    assert not report["partial"]


def test_multiple_agents_and_dated_models_aggregate_without_duplicate_response_ids():
    tracker = CostTracker()
    tracker.record("material", response(), {})
    tracker.record("material", response(), {})
    tracker.record("digest", response("gpt-6-luna-2026-09-01", id="resp_other"), {})
    report = tracker.snapshot(complete=True)
    assert report["total_usd"] == pytest.approx(0.00269)
    assert report["by_agent"]["material"]["model_calls"] == report["by_agent"]["digest"]["model_calls"] == 1


def test_long_context_threshold_and_flex_rates():
    tracker = CostTracker()
    data = response(service_tier="flex")
    data["usage"]["input_tokens"] = 272001
    tracker.record("digest", data, {})
    # Long input/cached/writes are doubled, output is 1.5x; Flex halves both.
    expected = ((269001 * .1 + 2000 * .01 + 1000 * .125) * 2 + 1000 * .5 * 1.5) / 1e6 / 2
    assert tracker.snapshot(complete=True)["total_usd"] == pytest.approx(expected)


def test_legacy_mini_search_fixed_content_charge_and_preview_fee():
    data = response("gpt-4.1-mini", output=[search()])
    data["usage"]["input_tokens_details"] = {"cached_tokens": 2000}
    tracker = CostTracker()
    tracker.record("material", data, {"tools": [{"type": "web_search"}]})
    result = tracker.snapshot(complete=True)
    assert result["llm_usd"] == pytest.approx(.005)
    assert result["web_search_usd"] == pytest.approx(.0132)
    tracker = CostTracker()
    tracker.record("material", data, {"tools": [{"type": "web_search_preview"}]})
    assert tracker.snapshot(complete=True)["web_search_usd"] == .025


@pytest.mark.parametrize("change", [
    {"model": "gpt-unpriced"}, {"model": "gpt-6-luna-unpriced"}, {"service_tier": "unknown"},
    {"usage": None}, {"usage": {"input_tokens": -10, "output_tokens": 1}},
    {"usage": {"input_tokens": 10, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 20}}},
])
def test_unknown_prices_or_usage_never_produce_a_complete_zero_estimate(change):
    tracker = CostTracker()
    tracker.record("material", response(output=[search()], **change), {})
    report = tracker.snapshot(complete=True)
    assert report["total_usd"] is None and report["partial"] and report["notes"]
    assert report["known_total_usd"] == .01


def test_missing_cache_details_are_disclosed():
    data = response()
    del data["usage"]["input_tokens_details"]["cache_write_tokens"]
    tracker = CostTracker()
    tracker.record("digest", data, {})
    assert tracker.snapshot(complete=True)["partial"]


def test_pending_usage_becomes_partial_only_when_run_ends():
    tracker = CostTracker()
    tracker.start("digest")
    assert not tracker.snapshot(complete=False)["partial"]
    assert tracker.snapshot(complete=True)["partial"]


def test_http_hooks_capture_retries_and_usage_before_downstream_parsing():
    attempts = []

    async def handle(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, json={"error": {"message": "busy", "type": "rate_limit_error"}},
                                  headers={"retry-after-ms": "1"})
        return httpx.Response(200, json=response(output=[search()]))

    async def run():
        with tracking_costs() as tracker:
            async with usage_client("sk-test", "material", transport=httpx.MockTransport(handle)) as client:
                result = await client.with_options(max_retries=1).responses.create(model="gpt-6-luna", input="private prompt")
                assert result.id == "resp_test"
                # Later JSON/schema rejection must not discard the already-incurred cost.
                with pytest.raises(ValueError):
                    raise ValueError("Rejected model output")
            return tracker.snapshot(complete=True)

    report = asyncio.run(run())
    assert len(attempts) == 2 and report["by_agent"]["material"]["requests"] == 2
    assert report["known_total_usd"] == pytest.approx(.011345)
    assert report["partial"]  # failed request did not disclose usage
    assert "sk-test" not in json.dumps(report) and "private prompt" not in json.dumps(report)


def test_network_failure_leaves_usage_unknown_without_breaking_error_handling():
    async def handle(request):
        raise httpx.ConnectError("offline", request=request)

    async def run():
        with tracking_costs() as tracker:
            async with usage_client("sk-test", "digest", transport=httpx.MockTransport(handle)) as client:
                from openai import APIConnectionError
                with pytest.raises(APIConnectionError):
                    await client.responses.create(model="gpt-6-luna", input="hello")
            return tracker.snapshot(complete=True)

    assert asyncio.run(run())["partial"]


def test_parallel_ticker_searches_and_context_reset_are_isolated():
    async def one(model):
        with tracking_costs() as tracker:
            async def handle(request):
                await asyncio.sleep(0)
                return httpx.Response(200, json=response(model=model))
            async with usage_client("sk-test", "digest", transport=httpx.MockTransport(handle)) as client:
                await client.responses.create(model=model, input="hello")
            assert active_costs.get() is tracker
            return tracker.snapshot(complete=True)

    async def run():
        return await asyncio.gather(one("gpt-6-luna"), one("gpt-6-sol"))

    luna, sol = asyncio.run(run())
    assert luna["total_usd"] == pytest.approx(.001345)
    assert sol["total_usd"] == pytest.approx(.0269)
    assert active_costs.get() is None
    with tracking_costs() as tracker:
        assert tracker.snapshot(complete=True)["total_usd"] == 0  # no charge for local cache hits
