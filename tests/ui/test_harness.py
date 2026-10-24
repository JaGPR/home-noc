"""Data-driven unit tests for the UI test harness (no browser needed)."""
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from ddt import data, ddt, unpack

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402


@ddt
class ApiName(unittest.TestCase):
    @data(
        ("/api/overview", "overview"),
        ("/api/openwrt?x=1", "openwrt"),
        ("/api/diagnose?target=wan1", "diagnose"),
        ("/api/", None),
        ("/api/actions/preview", None),      # two levels: not a plain GET endpoint
        ("/login", None),
        ("/", None),
    )
    @unpack
    def test_api_name(self, path, want):
        self.assertEqual(H.api_name(path), want)


@ddt
class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = H.FixtureServer().__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.srv.__exit__(None, None, None)

    def fetch(self, path, body=None):
        req = urllib.request.Request(self.srv.url + path, data=body, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    @data(
        ("/", 200, "text/html", b"<!doctype html>"),
        ("/index.html", 200, "text/html", b"<!doctype html>"),
        ("/login", 200, "text/html", b"<!doctype html>"),
        ("/api/overview", 200, "application/json", b"{"),
        ("/api/audit", 200, "application/json", b"["),
        ("/api/diagnose?target=wan1", 200, "application/json", b"{"),
        ("/api/nothing", 404, "application/json", b"{"),
        ("/static/x.css", 404, "application/json", b"{"),
    )
    @unpack
    def test_get(self, path, code, ctype, start):
        status, got_type, body = self.fetch(path)
        self.assertEqual(status, code)
        self.assertTrue(got_type.startswith(ctype), got_type)
        self.assertTrue(body.lstrip().lower().startswith(start.lower()), body[:40])

    @data(*sorted(json.loads(H.FIXTURES.read_text(encoding="utf-8"))))
    def test_every_fixture_is_served(self, name):
        status, _, body = self.fetch(f"/api/{name}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), self.srv.data[name])

    @data(
        ("/api/actions/preview", H.PREVIEW),
        ("/api/actions/confirm", {"ok": True, "result": {}}),
    )
    @unpack
    def test_post(self, path, want):
        status, _, body = self.fetch(path, b'{"kind": "x"}')
        self.assertEqual((status, json.loads(body)), (200, want))


@ddt
class Viewports(unittest.TestCase):
    @data(*H.PHONES.items())
    @unpack
    def test_phones_are_phone_sized(self, name, size):
        self.assertTrue(320 <= size[0] <= 600 and size[1] > size[0], name)

    def test_tabs_match_the_page(self):
        html = (H.STATIC / "index.html").read_text(encoding="utf-8")
        for t in H.TABS:
            self.assertIn(f'data-t="{t}"', html)


if __name__ == "__main__":
    unittest.main()
