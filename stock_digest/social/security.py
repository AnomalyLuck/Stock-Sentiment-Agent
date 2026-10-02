"""Safe provider diagnostics and credential redaction for application logs."""

import logging
import re
from urllib.parse import quote, quote_plus

import httpx

from . import config

_QUERY_CREDENTIAL = re.compile(
    r"([?&](?:token|key|api[_-]?key|access_token)=)[^&\s\"'<>]*",
    re.IGNORECASE,
)


def _redact_configured_keys(text: str) -> str:
    """Remove configured credentials even when they appear outside a URL."""
    secrets: set[str] = set()
    for name, value in vars(config).items():
        if not name.endswith("_API_KEY"):
            continue
        if not isinstance(value, str) or not value:
            continue
        secrets.add(value)
        secrets.add(quote(value, safe=""))
        secrets.add(quote_plus(value))

    # Replace longer keys first so shorter keys cannot hide part of a longer one.
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


def redact_credentials(text: str) -> str:
    """Remove query credentials and configured keys, including URL-encoded keys."""
    text = _QUERY_CREDENTIAL.sub(r"\1[REDACTED]", text)
    return _redact_configured_keys(text)


def provider_error_message(exc: Exception) -> str:
    """Describe a provider failure without exposing its URL, body, or message."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Provider returned HTTP {exc.response.status_code}."
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "Provider request timed out. Please try again shortly."
    return "Provider request failed. Please try again shortly."


def install_log_redaction() -> None:
    """Redact records before any handler sees HTTP client or application logs."""
    previous_factory = logging.getLogRecordFactory()
    if getattr(previous_factory, "_redacts_credentials", False):
        return

    # A root-logger filter does not cover child loggers; a record factory also
    # covers httpx, SDK debug logs, and handlers installed later by the server.
    def factory(*args, **kwargs):
        record = previous_factory(*args, **kwargs)
        # Preserve argument shapes: Uvicorn's access formatter unpacks its
        # five arguments. Do not replace a URL template's token=%s placeholder.
        if record.args:
            record.msg = _redact_configured_keys(str(record.msg))
        else:
            record.msg = redact_credentials(str(record.msg))

        def safe_arg(value):
            text = str(value)
            sanitized_text = redact_credentials(text)
            if sanitized_text != text:
                return sanitized_text
            return value

        if isinstance(record.args, dict):
            sanitized_arguments = {}
            for key, value in record.args.items():
                sanitized_arguments[key] = safe_arg(value)
            record.args = sanitized_arguments
        elif record.args:
            sanitized_arguments = []
            for value in record.args:
                sanitized_arguments.append(safe_arg(value))
            record.args = tuple(sanitized_arguments)
        if record.exc_info:
            record.exc_text = redact_credentials(
                logging.Formatter().formatException(record.exc_info)
            )
            # Keep the sanitized traceback, not exception objects a formatter
            # could serialize again with their original credential-bearing URL.
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact_credentials(record.stack_info)
        return record

    factory._redacts_credentials = True
    logging.setLogRecordFactory(factory)
