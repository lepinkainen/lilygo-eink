"""Entrypoint: background renderer + tiny HTTP server.

The render thread re-renders on a fixed cadence and stores the bytes under a
lock. The HTTP handler serves the cached bytes at /eink.bin and honors
If-Modified-Since so repeat polls cost no bandwidth.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import logging
import pathlib
import sys
import threading
import tomllib
import zoneinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import io

from ha import HAClient, IndoorReading
from metno import MetNoClient
from render import PACKED_SIZE, render, unpack_4bit


log = logging.getLogger(__name__)


class State:
    """Single source of truth for the current rendered image + metadata."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # Start fully white so a poll during the first render returns valid bytes.
        self.buf: bytes = bytes([0xFF] * PACKED_SIZE)
        self.last_modified: str = email.utils.formatdate(usegmt=True)


def _load_config(path: pathlib.Path) -> dict:
    if not path.is_file():
        raise SystemExit(
            f"missing {path}. copy config.toml.example to config.toml and fill it in."
        )
    with path.open("rb") as f:
        return tomllib.load(f)


def _tick(
    state: State,
    metno: MetNoClient,
    ha: HAClient,
    location_name: str,
    tz: dt.tzinfo,
) -> None:
    snap = metno.fetch()
    indoor = ha.fetch_indoor()
    try:
        buf = render(snap, indoor, location_name, tz)
    except Exception:
        log.exception("render failed; keeping previous frame")
        return
    with state.lock:
        state.buf = buf
        state.last_modified = email.utils.formatdate(usegmt=True)
    log.info(
        "rendered: temp=%s indoor=%s sym=%s",
        snap.temp if snap else "n/a",
        indoor.value if indoor.value is not None else "n/a",
        snap.symbol_now if snap else "n/a",
    )


def _schedule_loop(
    state: State,
    metno: MetNoClient,
    ha: HAClient,
    interval_s: int,
    location_name: str,
    tz: dt.tzinfo,
) -> None:
    while True:
        _tick(state, metno, ha, location_name, tz)
        # threading.Event().wait would let us signal shutdown cleanly, but the
        # process is run by Docker which will SIGTERM us; a plain sleep is fine.
        threading.Event().wait(interval_s)


def _make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            log.info("%s - %s", self.address_string(), format % args)

        def do_GET(self) -> None:  # noqa: N802 — http.server contract
            path = self.path.split("?", 1)[0]
            if path == "/eink.bin":
                self._serve_raw()
            elif path == "/preview.png":
                self._serve_preview()
            elif path in ("/", "/index.html"):
                self._serve_index()
            else:
                self.send_error(404, "not found")

        def _serve_raw(self) -> None:
            with state.lock:
                buf = state.buf
                last_modified = state.last_modified

            ims = self.headers.get("If-Modified-Since")
            if ims and ims == last_modified:
                self.send_response(304)
                self.send_header("Last-Modified", last_modified)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(buf)))
            self.send_header("Last-Modified", last_modified)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(buf)

        def _serve_preview(self) -> None:
            with state.lock:
                buf = state.buf
                last_modified = state.last_modified
            # Round-trip through the EPD wire format so the PNG shows
            # exactly what the device will receive (4-bit quantized).
            img = unpack_4bit(buf)
            out = io.BytesIO()
            img.save(out, format="PNG")
            payload = out.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Last-Modified", last_modified)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)

        def _serve_index(self) -> None:
            # Auto-reload the page every 30 s so editing render.py and
            # restarting the container surfaces the new frame without a
            # manual refresh. Cache-busting query string defeats the
            # browser's PNG cache.
            with state.lock:
                last_modified = state.last_modified
            tag = last_modified.replace(" ", "_")
            body = (
                "<!doctype html>"
                "<html><head>"
                "<meta charset='utf-8'>"
                "<meta http-equiv='refresh' content='30'>"
                "<title>lilygo-render</title>"
                "<style>"
                "body{margin:0;background:#222;color:#ddd;font-family:monospace;"
                "display:flex;flex-direction:column;align-items:center;padding:1rem;}"
                "img{max-width:100%;height:auto;border:1px solid #444;background:#fff;}"
                "p{margin:0.5rem 0;font-size:0.85rem;}"
                "a{color:#9cf;}"
                "</style></head><body>"
                f"<img src='/preview.png?t={tag}' alt='current frame'>"
                f"<p>frame: {last_modified} &middot; "
                "<a href='/eink.bin'>/eink.bin</a> (raw 4-bit packed)</p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _build_clients(cfg: dict) -> tuple[MetNoClient, HAClient, str, dt.tzinfo]:
    loc = cfg["location"]
    metno = MetNoClient(
        lat=loc["lat"],
        lon=loc["lon"],
        user_agent=cfg["metno"]["user_agent"],
    )
    ha_cfg = cfg.get("ha", {})
    ha = HAClient(
        base_url=ha_cfg.get("base_url", ""),
        token=ha_cfg.get("token", ""),
        entity=ha_cfg.get("entity_indoor", ""),
    )
    tz = zoneinfo.ZoneInfo(loc.get("timezone", "UTC"))
    return metno, ha, loc.get("name", ""), tz


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path("config.toml"))
    parser.add_argument(
        "--preview",
        type=pathlib.Path,
        help="Render once and save a PNG preview to this path. Skips the HTTP server.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    cfg = _load_config(args.config)
    metno, ha, location_name, tz = _build_clients(cfg)
    state = State()

    if args.preview is not None:
        _tick(state, metno, ha, location_name, tz)
        unpack_4bit(state.buf).save(args.preview)
        log.info("wrote preview to %s", args.preview)
        return 0

    interval_s = int(cfg["server"].get("render_interval_s", 900))
    host = cfg["server"].get("host", "0.0.0.0")
    port = int(cfg["server"].get("port", 8080))

    threading.Thread(
        target=_schedule_loop,
        args=(state, metno, ha, interval_s, location_name, tz),
        daemon=True,
    ).start()

    server = ThreadingHTTPServer((host, port), _make_handler(state))
    log.info("listening on %s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()
    return 0


# Re-export for clarity.
_ = IndoorReading

if __name__ == "__main__":
    sys.exit(main())
