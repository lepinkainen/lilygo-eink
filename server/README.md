# lilygo-render

Renders the 960×540 weather image for the LilyGo T5 4.7" e-paper display and serves it over HTTP as a raw 4-bit packed grayscale buffer (259200 bytes).

The firmware on the device just polls `GET /eink.bin` every 15 min and pushes the response straight into the EPD framebuffer. Everything else — met.no fetch, Home Assistant fetch, layout, fonts, icons — lives here.

## Setup

```sh
cd server
uv sync
cp config.toml.example config.toml
$EDITOR config.toml  # fill Wi-Fi-independent settings + HA token
```

## Run locally

```sh
uv run app.py                          # start the HTTP server
uv run app.py --preview /tmp/prev.png  # render once and dump preview PNG
```

Sanity-check the wire format:

```sh
curl -o /tmp/eink.bin http://localhost:8080/eink.bin
ls -l /tmp/eink.bin   # must be exactly 259200 bytes
```

Debug the live frame in a browser:

```
http://localhost:8080/preview.png   # decoded PNG of whatever the device would receive right now
http://localhost:8080/              # index with links
```

## Deploy with Docker / Portainer

Build + run locally:

```sh
docker compose up -d --build
docker logs -f lilygo-render
```

For Portainer: add a new stack pointing at this repo's `server/docker-compose.yml`, or paste the compose contents into the web editor. `config.toml` is bind-mounted read-only from the host, so editing it and running `docker compose restart lilygo-render` (or restarting the container in Portainer) is enough to pick up changes.

## Configuration

Every setting can come from a `config.toml` file or from env vars; env wins. `config.toml` is optional when env vars cover the required keys (`location.lat`, `location.lon`, `metno.email`).

Env-var pattern: `LILYGO_<SECTION>__<KEY>` (uppercased, double underscore between section and key — single `_` is ambiguous because some keys contain it). Examples:

| TOML | Env var |
| --- | --- |
| `location.lat` | `LILYGO_LOCATION__LAT` |
| `location.lon` | `LILYGO_LOCATION__LON` |
| `metno.email` | `LILYGO_METNO__EMAIL` |
| `server.port` | `LILYGO_SERVER__PORT` |
| `server.render_interval_s` | `LILYGO_SERVER__RENDER_INTERVAL_S` |
| `ha.token` | `LILYGO_HA__TOKEN` |

The met.no User-Agent is built as `lilygo-eink/<version> <email>`; only supply the email, not the whole UA string.

For Portainer, the env-only path avoids bind-mounting a host file: drop the `volumes:` block from `docker-compose.yml` and uncomment the `LILYGO_*` env block at the bottom of the same file.

## Iterate on layout

Run `uv run app.py --preview /tmp/prev.png` after each edit to `render.py`. The preview is the same image (composed in grayscale L mode) that the device would receive, so what you see on the PNG is what you'll see on the panel — minus the EPD's slight contrast quirks.

## Where to put the device URL

After this service is up, edit the firmware's `include/secrets.h`:

```c
#define EINK_URL "http://<server-ip>:8080/eink.bin"
```

Then flash. The device caches `Last-Modified` across deep sleep, so re-polls after a 304 cost no bandwidth.
