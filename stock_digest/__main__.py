from __future__ import annotations

import argparse
import os
import shutil
import sys

from .market import InputError, normalize_ticker
from .render import clean, render
from .runner import describe_error, digest_once, flush_traces, load_settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="A current, source-checked digest for one US-listed stock. Opens a local browser UI by default.")
    parser.add_argument("ticker", nargs="?", help="Optional ticker to run immediately; for example NVDA or BRK.B")
    parser.add_argument("--port", type=int, default=8765, help="Local UI port (default 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Start the UI server without opening a browser")
    parser.add_argument("--terminal", action="store_true",
                        help="Print the digest in the terminal instead of the UI (automatic when stdout is redirected)")
    parser.add_argument("--plain", action="store_true", help="Terminal mode without color")
    parser.add_argument("--no-footer", action="store_true", help="Terminal mode: omit coverage and disclosure")
    review = parser.add_mutually_exclusive_group()
    review.add_argument("--no-verify", dest="verify", action="store_false",
                        help="Skip model verification and revision (on by default); basic code checks still apply")
    review.add_argument("--verify", dest="verify", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(verify=True)
    parser.add_argument("--debug", action="store_true", help="Terminal mode: print pipeline diagnostics to stderr")
    args = parser.parse_args()

    terminal = args.terminal or args.plain or args.no_footer or args.debug or (args.ticker and not sys.stdout.isatty())
    try:
        ticker = normalize_ticker(args.ticker) if args.ticker else None
        if not terminal:
            from .web import serve
            return serve(port=args.port, open_browser=not args.no_browser, ticker=ticker, verify=args.verify)
        if ticker is None:
            parser.error("terminal mode needs a ticker, for example: stock-digest NVDA --terminal")
        return _terminal(ticker, args)
    except InputError as exc:
        print(f"Error: {clean(str(exc))}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


def _terminal(ticker: str, args) -> int:
    settings = load_settings(verify=args.verify)
    trace_url, trace_stream = None, sys.stderr

    def stage(message):
        print(clean(message), file=sys.stderr, flush=True)

    try:
        publication, trace_url = digest_once(ticker, settings, stage)
        if args.debug:
            for note in publication.diagnostics:
                print("Diagnostic: " + clean(note), file=sys.stderr)
        color = (not args.plain and sys.stdout.isatty() and "NO_COLOR" not in os.environ
                 and os.environ.get("TERM") != "dumb")
        output = render(publication, color=color, width=max(40, min(110, shutil.get_terminal_size((100, 24)).columns)),
                        show_footer=not args.no_footer)
        sys.stdout.write(output)
        sys.stdout.flush()
        trace_stream = sys.stdout
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # Avoid a second error when stdout is flushed at interpreter shutdown.
        sys.stdout = open(os.devnull, "w")
        trace_stream = None
        return 0
    except Exception as exc:
        trace_url = getattr(exc, "trace_url", trace_url)
        message, code = describe_error(exc, settings.timeout)
        print(f"Error: {clean(message)}", file=sys.stderr)
        return code
    finally:
        flush_traces(final=True)
        if trace_url and trace_stream is not None:
            try:
                print(f"Trace: {trace_url}", file=trace_stream, flush=True)
            except BrokenPipeError:
                sys.stdout = open(os.devnull, "w")


if __name__ == "__main__":
    raise SystemExit(main())
