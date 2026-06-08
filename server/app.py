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
import time
import zoneinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import io

from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from ha import HAClient, IndoorReading
from metno import MetNoClient
from render import PACKED_SIZE, render, unpack_4bit


log = logging.getLogger(__name__)

VERSION = "0.1"


class ServerCfg(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    render_interval_s: int = 900
    min_render_interval_s: int = 60


class LocationCfg(BaseModel):
    name: str = ""
    lat: str
    lon: str
    timezone: str = "UTC"


class MetnoCfg(BaseModel):
    # met.no requires an identifying contact in User-Agent or returns 403.
    # We build the UA as "lilygo-eink/<VERSION> <email>".
    email: str


class HaCfg(BaseModel):
    base_url: str = ""
    token: str = ""
    entity_indoor: str = ""


class Settings(BaseSettings):
    """Config loaded from env vars + config.toml.

    Precedence: env > config.toml > field default.
    Env vars use the LILYGO_ prefix with `__` as the section/key delimiter
    (single `_` is ambiguous because some keys themselves contain `_`):

        LILYGO_LOCATION__LAT, LILYGO_METNO__EMAIL,
        LILYGO_SERVER__RENDER_INTERVAL_S, LILYGO_HA__TOKEN, ...
    """

    server: ServerCfg = ServerCfg()
    location: LocationCfg
    metno: MetnoCfg
    ha: HaCfg = HaCfg()

    model_config = SettingsConfigDict(
        env_prefix="LILYGO_",
        env_nested_delimiter="__",
        toml_file="config.toml",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # env first → highest precedence; toml second; init kwargs last
        # so callers passing toml_file=... can still override the location.
        return (
            env_settings,
            TomlConfigSettingsSource(settings_cls),
            init_settings,
        )


class State:
    """Single source of truth for the current rendered image + metadata."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # Start fully white so a poll during the first render returns valid bytes.
        self.buf: bytes = bytes([0xFF] * PACKED_SIZE)
        self.last_modified: str = email.utils.formatdate(usegmt=True)
        # Monotonic timestamp of the last successful render. Used by the
        # on-GET render path to throttle: a burst of polls within
        # min_render_interval_s reuses the cached buffer.
        self.last_render_monotonic: float = 0.0
        # Guards _tick from running concurrently when multiple GETs race.
        self.render_lock = threading.Lock()


def _load_settings(toml_path: pathlib.Path) -> Settings:
    # Point pydantic-settings at the requested TOML path. The file is
    # optional: env vars alone can satisfy the schema.
    if not toml_path.is_file():
        log.info("no %s found; relying on LILYGO_* env vars", toml_path)
    Settings.model_config["toml_file"] = str(toml_path)
    return Settings()


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
        state.last_render_monotonic = time.monotonic()
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


def _make_handler(
    state: State,
    metno: MetNoClient,
    ha: HAClient,
    location_name: str,
    tz: dt.tzinfo,
    min_render_interval_s: int,
):
    def _maybe_render() -> None:
        # Coalesce concurrent GETs: only one render runs at a time.
        # Inside the lock we re-check the age so the second caller through
        # the door reuses the buffer the first caller just produced.
        with state.render_lock:
            with state.lock:
                age = time.monotonic() - state.last_render_monotonic
            if age < min_render_interval_s:
                return
            _tick(state, metno, ha, location_name, tz)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            log.info("%s - %s", self.address_string(), format % args)

        def handle_one_request(self) -> None:
            # The ESP32 client occasionally closes the socket immediately
            # after reading the 259200-byte body, before our wfile.write
            # has fully drained into the kernel. Treat that as success
            # rather than dumping a stack trace.
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError) as exc:
                log.debug("client disconnected mid-response: %s", exc)

        def do_GET(self) -> None:  # noqa: N802 — http.server contract
            path = self.path.split("?", 1)[0]
            if path == "/eink.bin":
                _maybe_render()
                self._serve_raw()
            elif path == "/preview.png":
                _maybe_render()
                self._serve_preview()
            elif path in ("/", "/index.html"):
                _maybe_render()
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


def _build_clients(s: Settings) -> tuple[MetNoClient, HAClient, str, dt.tzinfo]:
    user_agent = f"lilygo-eink/{VERSION} {s.metno.email}"
    metno = MetNoClient(lat=s.location.lat, lon=s.location.lon, user_agent=user_agent)
    ha = HAClient(
        base_url=s.ha.base_url,
        token=s.ha.token,
        entity=s.ha.entity_indoor,
    )
    tz = zoneinfo.ZoneInfo(s.location.timezone)
    return metno, ha, s.location.name, tz


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

    settings = _load_settings(args.config)
    metno, ha, location_name, tz = _build_clients(settings)
    state = State()

    if args.preview is not None:
        _tick(state, metno, ha, location_name, tz)
        unpack_4bit(state.buf).save(args.preview)
        log.info("wrote preview to %s", args.preview)
        return 0

    interval_s = settings.server.render_interval_s
    min_render_interval_s = settings.server.min_render_interval_s
    host = settings.server.host
    port = settings.server.port

    # Background loop now acts as a safety net: keeps the buffer fresh if
    # no GETs arrive for a while (e.g. device offline). On-GET rendering
    # is throttled by min_render_interval_s so bursty polls don't hammer
    # met.no/HA.
    threading.Thread(
        target=_schedule_loop,
        args=(state, metno, ha, interval_s, location_name, tz),
        daemon=True,
    ).start()

    server = ThreadingHTTPServer(
        (host, port),
        _make_handler(state, metno, ha, location_name, tz, min_render_interval_s),
    )
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
