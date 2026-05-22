"""Home Assistant client — single entity state fetch.

Optional dependency: if the token is unset or the request fails, the renderer
falls back to a `--` indoor reading.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests


log = logging.getLogger(__name__)


@dataclass
class IndoorReading:
    value: Optional[float]
    unit: str = ""


class HAClient:
    def __init__(self, base_url: str, token: str, entity: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.entity = entity

    def enabled(self) -> bool:
        return bool(self.token and self.entity)

    def fetch_indoor(self) -> IndoorReading:
        if not self.enabled():
            return IndoorReading(value=None)
        url = f"{self.base_url}/api/states/{self.entity}"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            r = requests.get(url, headers=headers, timeout=10, verify=False)
        except requests.RequestException as e:
            log.warning("HA request failed: %s", e)
            return IndoorReading(value=None)
        if r.status_code != 200:
            log.warning("HA HTTP %d for %s", r.status_code, self.entity)
            return IndoorReading(value=None)
        body = r.json()
        try:
            value = float(body["state"])
        except (KeyError, TypeError, ValueError):
            return IndoorReading(value=None)
        unit = body.get("attributes", {}).get("unit_of_measurement", "")
        return IndoorReading(value=value, unit=unit)
