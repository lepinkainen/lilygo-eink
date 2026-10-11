"""Plain-assert check: python test_metno.py"""
import requests
from unittest import mock

from metno import MetNoClient

c = MetNoClient("0", "0", "test")
with mock.patch("metno.requests.get", return_value=mock.Mock(status_code=304, headers={})):
    c.fetch()
assert c.last_error is None and c.last_ok is not None
ok = c.last_ok

with mock.patch("metno.requests.get", side_effect=requests.ConnectionError("dns")):
    c.fetch()
assert c.last_error and c.last_ok == ok

with mock.patch("metno.requests.get", return_value=mock.Mock(status_code=304, headers={})):
    c.fetch()
assert c.last_error is None and c.last_ok > ok
print("ok")
