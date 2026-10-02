"""Settings and a single traced digest run, shared by the browser UI and the terminal mode."""
from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from openai import APIError, AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError

from .agents import DEFAULT_VERIFY_MODEL, api_error_message
from .market import DigestError, InputError
from .models import Publication

SETTINGS = ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_RESEARCH_MODEL", "OPENAI_VERIFY_MODEL",
            "STOCK_DIGEST_TIMEOUT", "SEC_USER_AGENT", "STOCK_DIGEST_CACHE_DIR")


@dataclass(frozen=True)
class Settings:
    api_key: str
    models: dict
    timeout: float
    verify: bool = True


class _ModelErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Our handlers report the specific failure once; tracing retains SDK errors.
        return not record.getMessage().startswith("Error getting response")


def load_settings(*, verify: bool = True) -> Settings:
    """Read .env as data (no shell execution or interpolation); exported variables win."""
    local_settings = dotenv_values(Path.cwd() / ".env", interpolate=False)
    for name in SETTINGS:
        value = local_settings.get(name)
        if value is not None:
            os.environ.setdefault(name, value)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise InputError("Set OPENAI_API_KEY in your environment or the current directory's .env file. See README.md.")
    try:
        timeout = float(os.environ.get("STOCK_DIGEST_TIMEOUT", "480"))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except ValueError:
        raise InputError("STOCK_DIGEST_TIMEOUT must be a finite positive number of seconds.") from None
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1").strip()
    if not model:
        raise InputError("OPENAI_MODEL cannot be empty.")
    models = {"writer": model,
              "research": os.environ.get("OPENAI_RESEARCH_MODEL", "").strip() or model,
              "verifier": os.environ.get("OPENAI_VERIFY_MODEL", "").strip() or DEFAULT_VERIFY_MODEL}
    return Settings(api_key=api_key, models=models, timeout=timeout, verify=verify)


_tracing_ready = False


def enable_tracing(api_key: str) -> None:
    global _tracing_ready
    if not _tracing_ready:
        from agents import set_tracing_disabled, set_tracing_export_api_key
        set_tracing_disabled(False)
        set_tracing_export_api_key(api_key)
        logging.getLogger("openai.agents").addFilter(_ModelErrorFilter())
        _tracing_ready = True


def flush_traces(*, final: bool = False) -> None:
    from agents.tracing import get_trace_provider
    provider = get_trace_provider()
    if final:
        # Drain queued spans before a one-shot process exits, with a bounded wait.
        provider.shutdown(timeout=10)
    else:
        provider.force_flush()


def digest_once(ticker: str, settings: Settings, stage) -> tuple[Publication, str]:
    """Run one digest synchronously under its own trace. Returns (publication, trace URL).

    Exceptions propagate; ``describe_error`` turns them into safe messages. The trace
    URL is attached to the exception as ``trace_url`` when a run fails.
    """
    from agents import gen_trace_id, trace
    from .manager import run_digest

    enable_tracing(settings.api_key)
    trace_id = gen_trace_id()
    trace_url = f"https://platform.openai.com/traces/trace?trace_id={trace_id}"

    async def run():
        with trace("Stock Digest", trace_id=trace_id, metadata={"ticker": ticker}):
            deadline = asyncio.get_running_loop().time() + settings.timeout
            async with asyncio.timeout_at(deadline):
                return await run_digest(ticker, settings.api_key, settings.models, stage,
                                        verify=settings.verify, deadline=deadline)

    try:
        return asyncio.run(run()), trace_url
    except BaseException as exc:
        exc.trace_url = trace_url
        raise


def run_combined(raw_ticker: str, settings: Settings, stage, done, *, timezone: str = "UTC",
                 max_results: int | None = None, abort: tuple[type[BaseException], ...] = ()) -> str:
    """Run the material-news agent and the digest together, under one trace and one deadline.

    The digest consumes the material results (``manager.run_digest(material=...)``), so it
    finishes after the material run. ``stage(kind, message)`` reports progress and
    ``done(kind, result, error, trace_url)`` is called exactly once per kind ("material",
    "digest") as each side finishes; ``result`` is a ``MaterialResult`` or ``Publication``.
    Each side fails on its own (a non-US ticker fails only the digest). Exceptions listed in
    ``abort``, such as a closed browser connection, cancel both sides and propagate, as does the
    run deadline. Returns the trace URL.
    """
    from agents import gen_trace_id, trace
    from .manager import gather_or_cancel, run_digest
    from .market import normalize_ticker
    from .material.identity import parse_symbol
    from .material.pipeline import MaterialRequest, run_material

    enable_tracing(settings.api_key)
    trace_id = gen_trace_id()
    trace_url = f"https://platform.openai.com/traces/trace?trace_id={trace_id}"

    async def settle(kind: str, awaitable) -> None:
        try:
            result = await awaitable
        except abort:
            raise
        except Exception as exc:
            exc.trace_url = trace_url
            done(kind, None, exc, trace_url)
        else:
            done(kind, result, None, trace_url)

    async def run():
        # One trace for both: a trace opened inside another is flagged by the SDK.
        with trace("Stock Digest", trace_id=trace_id, metadata={"ticker": raw_ticker.strip().upper()}):
            deadline = asyncio.get_running_loop().time() + settings.timeout
            request = MaterialRequest(ticker=raw_ticker, timezone=timezone, max_results=max_results)
            material = asyncio.ensure_future(run_material(request, settings.api_key, settings.models,
                                                          lambda message: stage("material", message), deadline=deadline))

            async def digest():
                symbol, _ = parse_symbol(raw_ticker)
                ticker = normalize_ticker(symbol)   # the digest covers US listings only
                return await run_digest(ticker, settings.api_key, settings.models,
                                        lambda message: stage("digest", message),
                                        verify=settings.verify, deadline=deadline, material=material)

            async with asyncio.timeout_at(deadline):
                await gather_or_cancel(settle("material", material), settle("digest", digest()))

    try:
        asyncio.run(run())
    except BaseException as exc:
        exc.trace_url = trace_url
        raise
    return trace_url


def describe_error(exc: BaseException, timeout: float | None = None) -> tuple[str, int]:
    """(safe message, exit code). Never echoes exception bodies, which can hold request data or keys."""
    if isinstance(exc, InputError):
        return str(exc), 2
    if isinstance(exc, (AuthenticationError, PermissionDeniedError, NotFoundError, BadRequestError)):
        return api_error_message(exc), 2
    if isinstance(exc, APIError):
        return api_error_message(exc) + " No digest was published.", 1
    if isinstance(exc, DigestError):
        return str(exc), 1
    if isinstance(exc, TimeoutError):
        limit = f" ({timeout:g} seconds)" if timeout else ""
        return f"Run deadline exceeded{limit}; no digest was published. Adjust STOCK_DIGEST_TIMEOUT if needed.", 1
    return (f"Unable to complete the digest ({type(exc).__name__}). Check API access and connectivity; "
            "no draft was published."), 1
