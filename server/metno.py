"""met.no locationforecast client.

Mirrors the C `fetchMet()` in the original firmware: User-Agent header,
If-Modified-Since caching, and a shallow parse that only keeps the fields
needed for the display. The client owns its own `Last-Modified` cache, so
calling `fetch()` repeatedly is cheap once the upstream returns 304.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional

import requests


log = logging.getLogger(__name__)


@dataclass
class HourSlot:
    ts: dt.datetime
    temp: Optional[float]
    precip_1h: float
    symbol: str


@dataclass
class WeatherSnapshot:
    updated: dt.datetime
    temp: Optional[float]
    wind_speed: Optional[float]
    wind_dir: Optional[float]
    humidity: Optional[float]
    precip_1h: float
    symbol_now: str
    next: list[HourSlot] = field(default_factory=list)


class MetNoClient:
    def __init__(self, lat: str, lon: str, user_agent: str) -> None:
        self.url = (
            "https://api.met.no/weatherapi/locationforecast/2.0/compact"
            f"?lat={lat}&lon={lon}"
        )
        self.user_agent = user_agent
        self._last_modified: Optional[str] = None
        self._last_snapshot: Optional[WeatherSnapshot] = None

    def fetch(self) -> Optional[WeatherSnapshot]:
        """Return a snapshot, possibly the cached one if upstream sent 304.

        Returns None only when we have no cached snapshot AND the request
        failed (network or non-200/304 response).
        """
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified

        try:
            r = requests.get(self.url, headers=headers, timeout=15)
        except requests.RequestException as e:
            log.warning("met.no request failed: %s", e)
            return self._last_snapshot

        if r.status_code == 304:
            log.info("met.no: 304 not modified")
            return self._last_snapshot
        if r.status_code != 200:
            log.warning("met.no HTTP %d", r.status_code)
            return self._last_snapshot

        self._last_modified = r.headers.get("Last-Modified") or self._last_modified

        try:
            snap = _parse(r.json())
        except Exception as e:
            log.exception("met.no parse failed: %s", e)
            return self._last_snapshot

        self._last_snapshot = snap
        return snap


def _parse(payload: dict) -> WeatherSnapshot:
    ts = payload["properties"]["timeseries"]
    if not ts:
        raise ValueError("empty timeseries")

    first = ts[0]
    inst = first["data"]["instant"]["details"]
    next_1 = first["data"].get("next_1_hours", {})
    next_6 = first["data"].get("next_6_hours", {})

    symbol_now = (
        next_1.get("summary", {}).get("symbol_code")
        or next_6.get("summary", {}).get("symbol_code")
        or ""
    )
    precip_1h = float(next_1.get("details", {}).get("precipitation_amount", 0.0))

    forecast: list[HourSlot] = []
    for step in (3, 6, 9, 12):
        if step >= len(ts):
            break
        t = ts[step]
        det = t["data"]["instant"]["details"]
        n1 = t["data"].get("next_1_hours", {})
        n6 = t["data"].get("next_6_hours", {})
        sym = (
            n1.get("summary", {}).get("symbol_code")
            or n6.get("summary", {}).get("symbol_code")
            or ""
        )
        precip = float(
            n1.get("details", {}).get(
                "precipitation_amount",
                n6.get("details", {}).get("precipitation_amount", 0.0),
            )
        )
        forecast.append(
            HourSlot(
                ts=dt.datetime.fromisoformat(t["time"].replace("Z", "+00:00")),
                temp=det.get("air_temperature"),
                precip_1h=precip,
                symbol=sym,
            )
        )

    return WeatherSnapshot(
        updated=dt.datetime.now(dt.timezone.utc),
        temp=inst.get("air_temperature"),
        wind_speed=inst.get("wind_speed"),
        wind_dir=inst.get("wind_from_direction"),
        humidity=inst.get("relative_humidity"),
        precip_1h=precip_1h,
        symbol_now=symbol_now,
        next=forecast,
    )
