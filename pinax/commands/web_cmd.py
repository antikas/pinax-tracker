"""Serve a local, read-only browser view over one Pinax repository."""

from __future__ import annotations

import html
import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

from ..event import DamagedLogLine
from ..fold import fold, read_events
from ..projection import board_sections, render_item, resolved_claims


class WebViewError(Exception):
    """The selected repository cannot serve the web view."""


def _log_dir(repo_root: str) -> str:
    log_dir = os.path.join(repo_root, ".ergon", "log")
    if not os.path.isdir(log_dir):
        raise WebViewError("pinax: .ergon/log/ not found - run 'pinax init' first.")
    return log_dir


def _page(title: str, body: str) -> str:
    escaped_title = html.escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escaped_title}</title>
<style>
:root {{ color-scheme: light; font-family: Inter, "Segoe UI", sans-serif; color: #202730; background: #f5f6f8; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; }}
a {{ color: #204c78; text-underline-offset: .15em; }}
a:focus-visible {{ outline: 3px solid #176fa4; outline-offset: 3px; }}
.skip {{ position: absolute; left: -999rem; }}
.skip:focus {{ left: 1rem; top: 1rem; z-index: 2; background: white; padding: .5rem; }}
main {{ max-width: 62rem; margin: 0 auto; padding: 1.5rem; }}
.surface {{ min-width: 0; }}
nav {{ display: flex; align-items: center; flex-wrap: wrap; gap: 1.5rem; border-bottom: 1px solid #c2cbd4; padding: .65rem 0 1.15rem; }}
nav a:first-child {{ color: #143850; font-weight: 700; }}
h1 {{ font-size: clamp(1.8rem, 4vw, 2.4rem); margin: 1.4rem 0; }}
h2 {{ font-size: 1.08rem; margin: 1.4rem 0 .6rem; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
.projection, .event-payload {{ font-family: ui-monospace, Consolas, monospace; line-height: 1.5; }}
.rows, .events {{ list-style: none; margin: 0; padding: 0; }}
.rows {{ background: white; border: 1px solid #d0d8df; border-radius: .5rem; }}
.rows li {{ padding: .8rem 1rem; }}
.rows li + li {{ border-top: 1px solid #e0e4e8; }}
.row-title {{ font-weight: 600; overflow-wrap: anywhere; }}
.row-meta, .event-meta {{ margin: .35rem 0 0; color: #4a5765; font-size: .9rem; overflow-wrap: anywhere; }}
.row-meta code {{ color: #2d414e; }}
.event-meta {{ display: flex; gap: .7rem; flex-wrap: wrap; }}
.events > li {{ border-top: 1px solid #c2cbd4; padding: .5rem 0; }}
.event-payload {{ max-width: 100%; overflow-x: auto; }}
@media (max-width: 44rem) {{ main {{ padding: .8rem 1rem; }} .rows li {{ padding: .75rem; }} }}
</style>
</head>
<body>
<a class="skip" href="#content">Skip to content</a>
<main id="content" tabindex="-1"><section class="surface">{body}</section></main>
</body>
</html>
"""


def _navigation() -> str:
    return '<nav aria-label="Pinax views"><a href="/">Pinax board</a><a href="/history">Event history</a></nav>'


def _board_page(state: dict) -> str:
    sections = []
    for heading, rows in board_sections(state):
        items = []
        for item_id, title, detail in rows:
            link = f"/items/{quote(item_id, safe='')}"
            items.append(
                f'<li><a class="row-title" href="{link}">{html.escape(title or item_id)}</a>'
                f'<p class="row-meta"><code>{html.escape(item_id)}</code> · {html.escape(detail)}</p></li>'
            )
        sections.append(f'<h2>{html.escape(heading)}</h2><ul class="rows">{"".join(items)}</ul>')
    conflicts = resolved_claims(state)
    if conflicts:
        entries = "".join(f"<li>{html.escape(message)}</li>" for message in conflicts)
        sections.append(f"<h2>Claim conflicts (resolved)</h2><ul>{entries}</ul>")
    body = "".join(sections) or "<p>No items yet.</p>"
    return _page("Pinax board", f"{_navigation()}<h1>Board</h1>{body}")


def _item_page(item_id: str, item: dict, state: dict) -> str:
    content = html.escape(render_item(item_id, item, state))
    return _page(
        f"Pinax item {item_id}",
        f"{_navigation()}<h1>{html.escape(item_id)}</h1>"
        f"<pre class=\"projection\">{content}</pre>",
    )


def _missing_item_page(item_id: str) -> str:
    return _page(
        "Pinax item not found",
        f"{_navigation()}<h1>Item not found</h1>"
        f"<p>{html.escape(item_id) or 'That item'} was not found in this repository.</p>",
    )


def _history_page(events: list[dict]) -> str:
    if not events:
        body = '<p class="empty">No events yet.</p>'
    else:
        entries = []
        for event in events:
            payload = json.dumps(event.get("payload", {}), sort_keys=True, indent=2, ensure_ascii=False)
            entries.append(
                f'<li id="event-{html.escape(str(event["id"]))}">'
                f"<h2>{html.escape(str(event.get('type', 'unknown')))}</h2>"
                "<p class=\"event-meta\">"
                f"<span>#{html.escape(str(event.get('seq', '')))}</span>"
                f"<span>{html.escape(str(event.get('ts', '')))}</span>"
                f"<span>{html.escape(str(event.get('actor', '')))}</span>"
                f"<span>{html.escape(str(event.get('id', '')))}</span>"
                "</p>"
                f"<pre class=\"projection event-payload\">{html.escape(payload)}</pre>"
                "</li>"
            )
        body = '<ol class="events">' + "".join(entries) + "</ol>"
    return _page("Pinax event history", f"{_navigation()}<h1>Event history</h1>{body}")


def _local_authority(headers: list[str], port: int) -> bool:
    """Only the listening socket's local names can address the selected repository."""
    names = {"127.0.0.1", "localhost"}
    allowed = {f"{name}:{port}" for name in names}
    if port == 80:
        allowed.update(names)
    return len(headers) == 1 and headers[0].lower() in allowed


def create_server(repo_root: str, port: int) -> ThreadingHTTPServer:
    """Create a loopback server for repo_root without starting its request loop."""
    log_dir = _log_dir(repo_root)

    class WebViewHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _respond(self, status: int, content: str, include_body: bool = True) -> None:
            encoded = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if include_body:
                self.wfile.write(encoded)

        def _view(self, include_body: bool) -> None:
            if not _local_authority(self.headers.get_all("Host", []), self.server.server_port):
                self._respond(400, _page("Invalid address", "<h1>Invalid local address</h1>"), include_body)
                return
            path = urlsplit(self.path).path
            try:
                state = fold(log_dir)
                events = read_events(log_dir) if path == "/history" else []
            except DamagedLogLine as error:
                self._respond(
                    500,
                    _page(
                        "Pinax event log damaged",
                        f"<h1>Event log damaged</h1><p>{html.escape(str(error))}</p>",
                    ),
                    include_body,
                )
                return
            if path == "/":
                self._respond(200, _board_page(state), include_body)
                return
            if path == "/history":
                self._respond(200, _history_page(events), include_body)
                return
            if path.startswith("/items/"):
                item_id = unquote(path.removeprefix("/items/"))
                item = state.get("items", {}).get(item_id)
                if item is None:
                    self._respond(404, _missing_item_page(item_id), include_body)
                    return
                self._respond(200, _item_page(item_id, item, state), include_body)
                return
            self._respond(
                404,
                _page("Pinax page not found", f"{_navigation()}<h1>Page not found</h1>"),
                include_body,
            )

        def do_GET(self) -> None:
            self._view(True)

        def do_HEAD(self) -> None:
            self._view(False)

        def send_error(self, code, message=None, explain=None) -> None:
            # The base handler reports any unimplemented method through this hook.
            if code != HTTPStatus.NOT_IMPLEMENTED:
                return super().send_error(code, message, explain)
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", port), WebViewHandler)
    server.daemon_threads = True
    return server


def run(repo_root: str, port: int = 0) -> None:
    """Serve the selected repository until an interrupt closes the listener."""
    try:
        read_events(_log_dir(repo_root))
        server = create_server(repo_root, port)
    except WebViewError as error:
        print(error, file=sys.stderr)
        sys.exit(1)
    except OSError as error:
        print(f"pinax: cannot start web view on 127.0.0.1:{port}: {error}", file=sys.stderr)
        sys.exit(1)

    address = f"http://127.0.0.1:{server.server_address[1]}/"
    print(address, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
