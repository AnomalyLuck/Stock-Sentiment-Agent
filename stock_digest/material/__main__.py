"""material-news NVDA — print the material company developments digest as Markdown."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..render import clean
from ..runner import describe_error, flush_traces, load_settings
from .identity import resolve_timezone
from .pipeline import MaterialRequest, material_once
from .render import markdown


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="material-news",
        description="Source-linked digest of potentially stock-moving company developments for one ticker "
                    "over the last 7 days.")
    parser.add_argument("ticker", help="Ticker, e.g. NVDA, BRK.B, NASDAQ:MSFT or TSX:SHOP")
    parser.add_argument("--tz", help="Display timezone (IANA name); default: system timezone, else UTC")
    parser.add_argument("--exchange", help="Exchange, e.g. NASDAQ or NYSE (same as the NASDAQ:MSFT form)")
    parser.add_argument("--max", dest="max_results", type=positive_int, help="Maximum entries (disclosed when applied)")
    parser.add_argument("--language", default="English", help="Language of entry text (default English)")
    parser.add_argument("--record", type=Path, help="Write the internal event record (JSON) to this file")
    parser.add_argument("--debug", action="store_true", help="Print pipeline diagnostics to stderr")
    args = parser.parse_args()

    trace_url = None
    try:
        settings = load_settings(verify=False)
        request = MaterialRequest(ticker=args.ticker, timezone=resolve_timezone(args.tz),
                                  max_results=args.max_results, language=args.language, exchange=args.exchange)
        result, trace_url = material_once(request, settings, lambda m: print(clean(m), file=sys.stderr, flush=True))
        sys.stdout.write(markdown(result))
        if args.debug:
            for note in result.diagnostics:
                print("Diagnostic: " + clean(note), file=sys.stderr)
        if args.record:
            args.record.write_text(json.dumps(result.record, indent=2, ensure_ascii=False))
            print(f"Event record written to {args.record}", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        trace_url = getattr(exc, "trace_url", trace_url)
        message, code = describe_error(exc, None)
        print(f"Error: {clean(message)}", file=sys.stderr)
        return code
    finally:
        try:
            flush_traces(final=True)
        except Exception:
            pass
        if trace_url:
            print(f"Trace: {trace_url}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
