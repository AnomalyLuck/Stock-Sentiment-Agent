"""Local browser UI: one page, three streaming endpoints and one JSON endpoint.

Binds to 127.0.0.1 only. GET /api/run?ticker=NVDA&tz=America/New_York&max=5 runs the
material-news agent and the digest together (the digest consumes the material results) and
streams newline-delimited JSON tagged by kind: {"type": "stage", "kind": "digest" | "material",
"message": ...} while the run progresses, then exactly one terminal event per kind,
{"type": "result", "kind": ..., "digest" | "material": {...}} or
{"type": "error", "kind": ..., "message": ...}. The material result usually arrives first.

GET /api/digest?ticker=NVDA and GET /api/material?ticker=NVDA&tz=...&max=5 (always the last
7 days) run one side alone and stream untagged events with exactly one terminal event.
A client that disconnects aborts its run at the next progress message. At most
``Settings.max_runs`` of these runs (combined or single) proceed at once; a request beyond
that ends immediately with a busy error per kind, before any model call.

GET /api/social?query=NVDA (optional window=48h, the only accepted value, and
sources=x,reddit,...) returns one JSON object: social posts from the last 48 hours,
ported from Sentiment-Search's /api/social (see stock_digest/social). Failures use
an HTTP error status with {"detail": ...}, as in the source.
"""
from __future__ import annotations

import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from .market import InputError, normalize_ticker
from .costs import cost_snapshot, track_search_costs
from .material.identity import parse_symbol, resolve_timezone
from .material.pipeline import MaterialRequest, material_once
from .material.render import material_view
from .render import clean
from .runner import Settings, describe_error, digest_once, flush_traces, load_settings, run_combined
from .view import publication_view

KINDS = ("digest", "material")

PAGE = files("stock_digest").joinpath("ui", "index.html")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
    "Content-Security-Policy": ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                "connect-src 'self'; img-src 'self' data: https://i.ytimg.com; base-uri 'none'; "
                                "form-action 'none'"),  # i.ytimg.com: Social tab YouTube thumbnails
}
BUSY = "The server is busy with other searches. Try again in a few minutes."


class ClientGone(Exception):
    """The browser closed the stream; the run is abandoned."""


def _run_social(query: str, only):
    from .social.social import run_blocking
    return run_blocking(query, only)


class DigestHandler(BaseHTTPRequestHandler):
    server_version = "StockDigest/0.2"
    settings: Settings | None = None
    run_slots: threading.BoundedSemaphore | None = None  # set per server by make_server
    run = staticmethod(digest_once)
    run_material = staticmethod(material_once)
    run_combined = staticmethod(run_combined)
    run_social = staticmethod(_run_social)

    def log_message(self, format, *args):  # Quiet access log; progress is printed per run.
        pass

    def _headers(self, status: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path in {"/", "/index.html"}:
            body = PAGE.read_bytes()
            self._headers(200, "text/html; charset=utf-8")
            self.wfile.write(body)
        elif parts.path == "/api/run":
            self._stream_run(parse_qs(parts.query))
        elif parts.path == "/api/digest":
            self._stream(parse_qs(parts.query).get("ticker", [""])[0])
        elif parts.path == "/api/material":
            self._stream_material(parse_qs(parts.query))
        elif parts.path == "/api/social":
            self._social(parse_qs(parts.query))
        elif parts.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self._headers(404, "text/plain; charset=utf-8")
            self.wfile.write(b"Not found")

    def _send(self, event: dict) -> None:
        try:
            self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise ClientGone from exc

    @track_search_costs
    def _stream(self, raw_ticker: str) -> None:
        self._headers(200, "application/x-ndjson; charset=utf-8")
        try:
            ticker = normalize_ticker(raw_ticker)
        except InputError as exc:
            self._send({"type": "error", "message": clean(str(exc)), "cost": cost_snapshot(complete=True)})
            return

        def work(stage):
            publication, trace_url = self.run(ticker, self.settings, stage)
            return {"digest": publication_view(publication, trace_url)}, trace_url

        self._execute(ticker, "digest", work)

    @staticmethod
    def _material_request(query: dict) -> MaterialRequest:
        """Validate the material-news options; raises InputError."""
        value = lambda name: query.get(name, [""])[0].strip()
        parse_symbol(value("ticker"))
        try:
            limit = int(value("max")) if value("max") else None
        except ValueError:
            raise InputError("Maximum results must be a number.") from None
        if limit is not None and limit < 1:
            raise InputError("The maximum must be at least 1.")
        return MaterialRequest(ticker=value("ticker"), timezone=resolve_timezone(value("tz") or "UTC"), max_results=limit)

    @track_search_costs
    def _stream_material(self, query: dict) -> None:
        self._headers(200, "application/x-ndjson; charset=utf-8")
        try:
            request = self._material_request(query)
        except InputError as exc:
            self._send({"type": "error", "message": clean(str(exc)), "cost": cost_snapshot(complete=True)})
            return

        def work(stage):
            result, trace_url = self.run_material(request, self.settings, stage)
            return {"material": material_view(result, trace_url)}, trace_url

        self._execute(request.ticker.upper(), "material news", work)

    @track_search_costs
    def _stream_run(self, query: dict) -> None:
        """Both runs in one stream; every event carries its kind and each kind ends exactly once."""
        self._headers(200, "application/x-ndjson; charset=utf-8")
        try:
            request = self._material_request(query)
        except InputError as exc:
            for kind in KINDS:
                self._send({"type": "error", "kind": kind, "message": clean(str(exc)),
                            "cost": cost_snapshot(complete=True)})
            return
        settings = self.settings
        label = request.ticker.upper()
        timeout = settings.timeout if settings else None
        finished: set[str] = set()
        print(f"[{label}] digest and material news requested", file=sys.stderr, flush=True)
        if not self._claim_run_slot(label):
            for kind in KINDS:
                self._send({"type": "error", "kind": kind, "message": BUSY, "cost": cost_snapshot(complete=True)})
            return

        def stage(kind: str, message: str) -> None:
            print(f"[{label}] {kind}: {clean(message)}", file=sys.stderr, flush=True)
            self._send({"type": "stage", "kind": kind, "message": clean(message)})

        def done(kind: str, result, error, trace_url) -> None:
            finished.add(kind)
            if error is not None:
                message, _ = describe_error(error, timeout)
                print(f"[{label}] {kind} error: {message}", file=sys.stderr, flush=True)
                self._send({"type": "error", "kind": kind, "message": clean(message), "trace_url": trace_url,
                            "cost": cost_snapshot(complete=len(finished) == len(KINDS))})
                return
            view = publication_view(result, trace_url) if kind == "digest" else material_view(result, trace_url)
            self._send({"type": "result", "kind": kind, kind: view,
                        "cost": cost_snapshot(complete=len(finished) == len(KINDS))})
            print(f"[{label}] {kind} done · {trace_url}", file=sys.stderr, flush=True)

        try:
            self.run_combined(request.ticker, settings, stage, done, timezone=request.timezone,
                              max_results=request.max_results, abort=(ClientGone,))
        except ClientGone:
            print(f"[{label}] browser disconnected; run abandoned", file=sys.stderr, flush=True)
        except Exception as exc:
            # The deadline, or a failure outside either side: close every kind still open.
            message, _ = describe_error(exc, timeout)
            print(f"[{label}] error: {message}", file=sys.stderr, flush=True)
            for kind in KINDS:
                if kind in finished:
                    continue
                finished.add(kind)
                try:
                    self._send({"type": "error", "kind": kind, "message": clean(message),
                                "trace_url": getattr(exc, "trace_url", None),
                                "cost": cost_snapshot(complete=len(finished) == len(KINDS))})
                except ClientGone:
                    break
        finally:
            self._release_run_slot()
            threading.Thread(target=flush_traces, daemon=True).start()

    def _claim_run_slot(self, label: str) -> bool:
        """Take one of the server's run slots without waiting; False (logged) when all are in use."""
        if self.run_slots is None or self.run_slots.acquire(blocking=False):
            return True
        print(f"[{label}] refused: every run slot is in use", file=sys.stderr, flush=True)
        return False

    def _release_run_slot(self) -> None:
        if self.run_slots is not None:
            self.run_slots.release()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            self._headers(status, "application/json; charset=utf-8")
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _social(self, query: dict) -> None:
        from .social import social

        value = lambda name: query.get(name, [""])[0].strip()
        raw = value("query")
        if (value("window") or social.SOCIAL_WINDOW) != social.SOCIAL_WINDOW:
            self._json(400, {"detail": f"The social window is fixed at {social.SOCIAL_WINDOW}."})
            return
        try:
            parse_symbol(raw)
        except InputError as exc:
            self._json(400, {"detail": clean(str(exc))})
            return
        label = raw.upper()
        print(f"[{label}] social posts requested", file=sys.stderr, flush=True)
        try:
            result = self.run_social(raw, social.parse_sources(value("sources")))
        except InputError as exc:
            self._json(404, {"detail": clean(str(exc))})
            return
        except TimeoutError:
            print(f"[{label}] social: timed out", file=sys.stderr, flush=True)
            self._json(504, {"detail": "Social retrieval timed out. Try again."})
            return
        except Exception as exc:  # noqa: BLE001 - never forward provider text to the page
            print(f"[{label}] social error: {type(exc).__name__}", file=sys.stderr, flush=True)
            self._json(502, {"detail": "Social retrieval failed. Try again."})
            return
        body = social.social_response(raw, result)
        print(f"[{label}] social: {len(body['posts'])} posts · " +
              ", ".join(f"{name} {state}" for name, state in result.status.items()), file=sys.stderr, flush=True)
        self._json(200, body)

    def _execute(self, label: str, kind: str, work) -> None:
        """Run ``work(stage)`` and stream its progress and single result or error."""
        settings = self.settings
        print(f"[{label}] {kind} requested", file=sys.stderr, flush=True)
        if not self._claim_run_slot(label):
            self._send({"type": "error", "message": BUSY, "cost": cost_snapshot(complete=True)})
            return

        def stage(message: str) -> None:
            print(f"[{label}] {clean(message)}", file=sys.stderr, flush=True)
            self._send({"type": "stage", "message": clean(message)})

        try:
            payload, trace_url = work(stage)
            self._send({"type": "result", **payload, "cost": cost_snapshot(complete=True)})
            print(f"[{label}] done · {trace_url}", file=sys.stderr, flush=True)
        except ClientGone:
            print(f"[{label}] browser disconnected; run abandoned", file=sys.stderr, flush=True)
        except Exception as exc:
            message, _ = describe_error(exc, settings.timeout if settings else None)
            print(f"[{label}] error: {message}", file=sys.stderr, flush=True)
            try:
                self._send({"type": "error", "message": clean(message), "trace_url": getattr(exc, "trace_url", None),
                            "cost": cost_snapshot(complete=True)})
            except ClientGone:
                pass
        finally:
            self._release_run_slot()
            threading.Thread(target=flush_traces, daemon=True).start()


def make_server(settings: Settings, port: int = 8765) -> ThreadingHTTPServer:
    from .social.security import install_log_redaction
    install_log_redaction()  # keep social provider keys out of any log line
    handler = type("ConfiguredDigestHandler", (DigestHandler,),
                   {"settings": settings, "run_slots": threading.BoundedSemaphore(settings.max_runs)})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def serve(*, port: int = 8765, open_browser: bool = True, ticker: str | None = None, verify: bool = True) -> int:
    settings = load_settings(verify=verify)
    try:
        server = make_server(settings, port)
    except OSError as exc:
        raise InputError(f"Port {port} is unavailable ({exc.strerror}); pass --port to choose another.") from None
    url = f"http://127.0.0.1:{server.server_address[1]}/" + (f"?t={ticker}" if ticker else "")
    print(f"Stock Digest is running at {url}  (Ctrl-C to stop)", file=sys.stderr, flush=True)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    finally:
        server.server_close()
        flush_traces(final=True)
    return 0
