# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two pieces of one weather display:

1. **`server/`** — Python service (Docker / Portainer). Fetches Norwegian Meteorological Institute (`api.met.no`) data and Home Assistant sensor states, renders a 960×540 grayscale image with Pillow, packs to 4-bit, serves over HTTP as `/eink.bin`. See `server/README.md` for setup.

2. **`src/main.cpp`** (this directory) — Firmware for a LILYGO T5 4.7" e-paper V2.3 (ESP32-S3, ED047TC1 panel, 960×540, 16-level grayscale). Wakes from deep sleep every 15 min, polls the server, streams the response straight into the EPD framebuffer, pushes to the panel, deep-sleeps. Single PlatformIO project, single `src/main.cpp`, Arduino framework.

Sibling project of `../yellow` (CYD/TFT version). `../yellow` does everything on-device (TFT_eSPI + on-device met.no + on-device icons). We deliberately split here because e-paper layout iteration is painful when every change requires a re-flash.

## Toolchain

PlatformIO is installed via `uv tool install platformio`. `pio` is on PATH. pyserial is available via `uvx --from pyserial python ...` — there is no project-level Python environment for the firmware side. The server has its own `uv` project in `server/`.

The first `pio run` downloads ~150 MB of toolchains and clones `LilyGo-EPD47` from GitHub into `.pio/libdeps/`. Subsequent builds are fast (~5 s incremental).

## First-time setup

```sh
# firmware
cp include/secrets.h.example include/secrets.h && $EDITOR include/secrets.h
pio run                                           # downloads toolchain + libs

# server
cd server
uv sync
cp config.toml.example config.toml && $EDITOR config.toml
uv run app.py --preview /tmp/p.png                # smoke-test the renderer
```

Edit `include/secrets.h` with Wi-Fi creds + `EINK_URL` (server host). Edit `server/config.toml` with met.no contact + HA token (or leave HA token empty to disable indoor reading).

## Common firmware commands

- Build: `pio run`
- Flash: `pio run -t upload --upload-port /dev/cu.usbmodem101`
- Read serial (headless): `uvx --from pyserial python scripts/monitor.py [seconds]`
- Clean: `pio run -t clean`
- Static analysis: `scripts/lint.sh` (see "Static analysis" below — runs `pio run` with strict project-code warnings, not clang-tidy)

`pio device monitor` does **not** work in this harness — it requires a real TTY and fails with `termios.error: Operation not supported by device`. Always use `scripts/monitor.py`.

## Common server commands

```sh
cd server
uv sync                         # install deps
cp config.toml.example config.toml  # then edit
uv run app.py                   # serve on :8080
uv run app.py --preview /tmp/p.png  # render once, save PNG, exit
docker compose up -d --build    # deploy as container
```

## Hardware quirks

- USB-serial port: `/dev/cu.usbmodem101` (ESP32-S3 native USB-JTAG/serial, VID `0x303a` / PID `0x1001`). No CH340/CP210x driver needed.
- Native USB-CDC: no DTR/RTS download-mode trap (unlike yellow). `scripts/monitor.py` is intentionally minimal — open the port, read bytes. The running firmware deep-sleeps and the CDC device disappears for ~15 min between wakes; that's normal.
- The `DEBUG_NO_SLEEP=1` build flag (set in `platformio.ini`) keeps the chip awake and runs `cycle()` from `loop()` instead of deep-sleeping. The USB-CDC port stays enumerated, so iterating is fast. Comment it out for production.

## Troubleshooting

- **`/dev/cu.usbmodem101` missing after deep sleep**: the native USB-CDC peripheral powers down with the rest of the chip. Hold `BOOT` (GPIO0) while replugging USB to force the ROM bootloader — the port reappears and stays alive until the next reset. `pio run -t upload` then works.
- **`pio device monitor` errors with `termios.error: Operation not supported by device`**: harness limitation. Use `uvx --from pyserial python scripts/monitor.py [seconds]` instead.
- **Display shows last image after restart**: expected. E-paper is bistable; we only push to the panel on a 200 response. On 304 or any error we leave it alone, so the device "looks like it's working" even when the server is down.
- **Server-rendered preview looks fine but device shows garbage**: pack order. The wire format is two pixels per byte, **low nibble = even column**. If you change `render.py:_pack_4bit`, also re-check `epd_driver.c:695` for the lib's expectation.

## Display configuration

EPD is driven by `LilyGo-EPD47` (Xinyuan-LilyGo's epdiy fork), pulled directly from GitHub in `platformio.ini`'s `lib_deps`. The driver picks the T5 4.7" V2.3 pinmap when `-DLILYGO_T5_47=1` is set in `build_flags`. There is no User_Setup-equivalent file — all configuration is in `platformio.ini` so the board profile stays self-contained.

The framebuffer is 4-bit grayscale, `EPD_WIDTH * EPD_HEIGHT / 2 = 259200` bytes, allocated in PSRAM via `heap_caps_malloc(..., MALLOC_CAP_SPIRAM)`. The HTTP response body is streamed straight into it; one `epd_draw_grayscale_image(epd_full_screen(), g_fb)` call pushes the panel.

Pixel order in the wire format: two pixels per byte, **low nibble = even column, high nibble = odd column**. `server/render.py:_pack_4bit` produces this layout; `epd_copy_to_framebuffer` in the LilyGo-EPD47 lib reads it. Don't flip the order — confirmed against the lib's source at `epd_driver.c:695`.

## Refresh strategy

`setup()` does one full pass — wake, fetch, push, sleep — and `loop()` is unreachable in production. The wake interval is 15 min (`SLEEP_INTERVAL_US`).

Every wake that produces a new image calls `epd_clear()` before `epd_draw_grayscale_image()`. Partial refreshes alone leave ghost trails on regions that change every cycle (top-right datetime, footer line); the full clear costs ~600 ms of flicker per cycle but the panel is bistable and idle for 15 min between updates, so the tradeoff is heavily in clear's favor.

Boot count and the `Last-Modified` header are persisted in RTC slow memory via `RTC_DATA_ATTR` so a 304 response can skip the EPD push entirely — no clear, no flicker, panel keeps its last image. `String` is **not** safe across deep sleep — `g_last_modified` is therefore a `char[64]`.

## met.no + Home Assistant

Both moved to `server/`. The firmware no longer parses JSON, knows about lat/lon, or holds an HA token. See `server/metno.py` and `server/ha.py`.

The server still honors `If-Modified-Since` upstream of met.no (~30 min rule), so its own bandwidth to api.met.no stays minimal even when the device polls every 15 min.

## Architecture

Single-file firmware state machine. One `setup()` pass:

- Init Serial, increment `g_boot_count`, init EPD, allocate framebuffer in PSRAM.
- Wi-Fi connect → `fetchImage()` which GETs `EINK_URL`, honors `If-Modified-Since`, streams the response into `g_fb`.
- On 200: optional `epd_clear()` every 8th wake, then `epd_draw_grayscale_image(epd_full_screen(), g_fb)`, `epd_poweroff()`.
- On 304: skip flush, panel keeps its last image.
- On error: skip flush, log, sleep — the panel keeps its last image.
- Disconnect Wi-Fi, enter deep sleep for 15 min.

`fetchImage()` validates `Content-Length == FB_BYTES` (259200) before reading and falls back to error if the stream times out. The server always sends explicit `Content-Length`, no chunked transfer.

## Layout iteration loop

The main dev advantage of the split architecture: edit `server/render.py` → `uv run app.py --preview /tmp/p.png` → open the PNG → repeat. No firmware re-flash. The preview path uses the same pack-then-unpack roundtrip that the device sees, so what's in the PNG is what'll be on the panel.

When the layout is right, push a new server image (`docker compose up -d --build` on the host) and the device picks it up on the next 15-min wake automatically — no firmware change needed.

## Server architecture

`server/app.py` runs two things in one process:

- Background `threading.Thread` running `_schedule_loop` — calls `_tick()` every `render_interval_s` (default 900). `_tick()` fetches met.no + HA, renders into bytes via `server/render.py`, atomically swaps under a lock.
- `ThreadingHTTPServer` on `:8080` serving `/eink.bin`. Honors `If-Modified-Since` against the in-memory `Last-Modified` timestamp set when the buffer last changed.

`render.py:render()` returns `bytes` of exactly `259200` length. Pillow renders into `mode="L"` (8-bit grayscale), then `_pack_4bit` shifts down to 4-bit and packs two pixels per byte via numpy.

Met.no icons live in `server/icons/` (PNG, 200×200, color with alpha). They're composited onto white before grayscale conversion so transparent backgrounds read as panel-white. Fonts in `server/fonts/` (Fira Sans Regular + Bold). Both directories are committed.

`server/config.toml` is the only secret-holding file — gitignored. `config.toml.example` is the template.

## Static analysis

`scripts/lint.sh` runs `pio run` and surfaces any `warning:` or `error:` lines whose paths start with `src/` or `include/`. The strict warning set (`-Wall -Wextra -Wshadow -Wuninitialized -Wmaybe-uninitialized -Wmissing-field-initializers -Wpointer-arith -Wcast-align -Wformat -Wnull-dereference`) is configured in `platformio.ini`'s `build_src_flags`, so it applies to project code only, not to LilyGo-EPD47 or Arduino-ESP32.

Mainstream clang-tidy was tried but cannot parse the ESP32-S3 SDK on a macOS host: inline-asm constraints `"a"`/`"=a"` aren't valid for x86_64, `__attribute__((section(".iram1.N")))` requires Mach-O `"segment,section"` syntax, and pointers don't fit in `uint32_t`. The toolchain's xtensa-gcc handles all of that correctly, so we lean on it as the analysis driver and skip clang-tidy entirely.

## Secrets

`include/secrets.h` (firmware) and `server/config.toml` (server) are both gitignored. Templates: `include/secrets.h.example` and `server/config.toml.example`.

Firmware secrets are minimal now: Wi-Fi credentials + `EINK_URL`. Everything else (lat/lon, met.no contact email, HA URL + token) lives in `server/config.toml` — or can be supplied via `LILYGO_<SECTION>__<KEY>` env vars (env wins over file, double-underscore between section and key). `server/config.toml` is optional when env vars cover the required keys (`location.lat`, `location.lon`, `metno.email`). Config is parsed by `pydantic-settings`; the User-Agent sent to met.no is built as `lilygo-eink/<VERSION> <metno.email>`, so only the email is configurable. See `server/README.md` for the full mapping.
