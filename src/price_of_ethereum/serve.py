"""Local read-only HTTP server for the dashboard.

Point it at the JSONL files `poe collect` is appending to and it serves a page
that refreshes as blocks land. Read-only and loopback-bound by default: it
renders measurements already on disk, never talks to Fynd or Tycho, and holds no
state of its own. Each request re-reads the newest block's rows (backwards, so
cost is one block rather than the whole file) plus the block summaries.
"""

from __future__ import annotations

import json
import logging
import socket
import socketserver
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pandas as pd

from price_of_ethereum.storage import load_jsonl, load_latest_block_rows

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_POLL_S = 4.0


def _read_frames(rows_path: Path, blocks_path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Latest block's rows and every block summary; missing files read as empty
    so the page can come up before the collector has written anything."""
    rows = load_latest_block_rows(rows_path) if rows_path.exists() else pd.DataFrame()
    blocks = load_jsonl(blocks_path) if blocks_path.exists() else pd.DataFrame()
    return rows, blocks


class DashboardHandler(BaseHTTPRequestHandler):
    """Serves three things: the page, the payload, and the Plotly bundle."""

    server_version = "poe-dashboard"

    def __init__(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: Any,
        server: socketserver.BaseServer,
        *,
        rows_path: Path,
        blocks_path: Path,
        title: str,
        poll_ms: int,
        plotly_js: str,
    ) -> None:
        # Set before super().__init__, which handles the request inline.
        self.rows_path = rows_path
        self.blocks_path = blocks_path
        self.title = title
        self.poll_ms = poll_ms
        self.plotly_js = plotly_js
        super().__init__(request, client_address, server)

    def do_GET(self) -> None:
        # Imported here, not at module scope: the CLI reads this module's
        # defaults, and a base install without the viz extra has no Plotly.
        from price_of_ethereum.dashboard import build_payload, render_page

        route = self.path.split("?", 1)[0]
        if route in ("/", "/index.html"):
            self._send(
                render_page(title=self.title, poll_ms=self.poll_ms, payload=None, inline_js=False),
                "text/html; charset=utf-8",
            )
        elif route == "/plotly.js":
            # Immutable for the process lifetime, so let the browser keep it.
            self._send(
                self.plotly_js,
                "application/javascript; charset=utf-8",
                extra_headers={"Cache-Control": "max-age=86400"},
            )
        elif route == "/data.json":
            rows, blocks = _read_frames(self.rows_path, self.blocks_path)
            payload = build_payload(rows, blocks)
            self._send(
                json.dumps(payload),
                "application/json; charset=utf-8",
                extra_headers={"Cache-Control": "no-store"},
            )
        else:
            self.send_error(404, "not found")

    def _send(
        self, body: str, content_type: str, *, extra_headers: dict[str, str] | None = None
    ) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("dashboard %s", format % args)


def serve_dashboard(
    rows_path: Path | str,
    blocks_path: Path | str,
    *,
    title: str = "Price of Ethereum — measured depth",
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    poll_s: float = DEFAULT_POLL_S,
) -> None:
    """Serve until interrupted. Binds loopback by default — pass `host="0.0.0.0"`
    only if you intend to expose your measurements on the network."""
    from plotly.offline import get_plotlyjs

    handler = partial(
        DashboardHandler,
        rows_path=Path(rows_path),
        blocks_path=Path(blocks_path),
        title=title,
        poll_ms=int(poll_s * 1000),
        plotly_js=get_plotlyjs(),
    )
    with ThreadingHTTPServer((host, port), handler) as httpd:
        logger.info("dashboard on http://%s:%d (refresh %.1fs)", host, port, poll_s)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            logger.info("dashboard stopped")
