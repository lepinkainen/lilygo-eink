"""Plain-assert check: python test_battery.py"""
import time

from app import State, _parse_battery
from render import battery_key, battery_percent

assert battery_percent(3.0) == 0
assert battery_percent(3.3) == 0
assert battery_percent(3.8) == 50
assert battery_percent(3.85) == 60
assert battery_percent(4.2) == 100
assert battery_percent(5.0) == 100

assert battery_key(None) is None
assert battery_key(3.8) == "50"
assert battery_key(4.15) == "95"
assert battery_key(4.19) == "external"
assert battery_key(4.22) == "external"

assert _parse_battery("batt=3.92") == 3.92
assert _parse_battery("") is None
assert _parse_battery("batt=abc") is None
assert _parse_battery("batt=nan") is None
assert _parse_battery("batt=inf") is None
assert _parse_battery("batt=-1") is None
assert _parse_battery("batt=99") is None

s = State(battery_stale_s=3600)
assert s.current_battery() is None
s.record_battery(3.9)
assert s.current_battery() == 3.9
s.battery_at_monotonic = time.monotonic() - 3601
assert s.current_battery() is None
print("ok")
