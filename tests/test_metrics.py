"""/metrics for Prometheus (app/metrics.py) and the data-used settings (usage_set) through the real API.

Offline: the app runs on the example config with dummy secrets in a temporary directory, like smoke_test.py,
with the router read replaced by a fixed answer.  Run:  python -m unittest tests/test_metrics.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

ROOT = Path(__file__).resolve().parents[1]
ETC = Path(tempfile.mkdtemp())
VAR = Path(tempfile.mkdtemp())
EXAMPLE = json.loads((ROOT / "config" / "config.example.json").read_text())
SECRETS = ("PIHOLE_A_PASS=x\nPIHOLE_B_PASS=x\nOMADA_CLIENT_ID=x\nOMADA_CLIENT_SECRET=x\nPVE_TOKEN=x\n"
           "NOC_ADMIN_USER=admin\nNOC_ADMIN_PASS=test-pass\nNOC_SESSION_KEY=" + "ef" * 32 + "\n")
(ETC / "config.json").write_text(json.dumps(EXAMPLE))
(ETC / "secrets.env").write_text(SECRETS)
os.environ["NOC_ETC"], os.environ["NOC_VAR"] = str(ETC), str(VAR)
os.environ["NOC_NO_WORKERS"] = "1"
sys.path.insert(0, str(ROOT / "app"))

from fastapi.testclient import TestClient  # noqa: E402
import app as noc  # noqa: E402
import metrics as M  # noqa: E402

ROUTER = {"interfaces": [{"name": "wan1", "device": "pppoe-wan1", "up": True, "ip": "192.0.2.1/32"},
                         {"name": "wan2", "device": "pppoe-wan2", "up": False, "ip": ""}],
          "mwan": {"wan1": {"latency": 7.5, "loss": 0}, "wan2": {"latency": None, "loss": 100}},
          "onts": [{"name": "ONT \"a\"", "rx_dbm": -18.27, "tx_dbm": 2.43, "temp_c": 39.6}, {"name": "dead", "rx_dbm": None}],
          "leases": [{"ip": "10.0.0.5", "mac": "aa:00:00:00:00:05", "name": "phone"}]}
NOW = time.time()
MONTH = time.strftime("%Y-%m", time.localtime(NOW))
STORE = {"since": 1795000000, "months": {"2000-01": {"wan1": [5, 1]}, MONTH: {"wan1": [300, 30], "wan2": [100, 10]}},
         "devices": {MONTH: {"aa:00:00:00:00:05": {"ips": ["10.0.0.5"], "w": {"wan1": [250, 20]}}}}}
GROUPS = {"isp-a": ["wan1", "wan2"]}


def lines(text, name):
    return [l for l in text.splitlines() if l.startswith(name + "{") or l.startswith(name + " ")]


@ddt
class Render(unittest.TestCase):
    def setUp(self):
        self.text = M.render(M.samples(STORE, {"wan1": [8e6, 1e6]}, ROUTER, GROUPS, {"wan1", "wan2"}, None,
                                       [{"mbps": {"wan1": 300, "wan2": 39}}, {"mbps": {"wan1": 320}}], NOW))

    @data(("noc_wan_up", ['noc_wan_up{wan="wan1",isp="isp-a",device="pppoe-wan1"} 1', 'noc_wan_up{wan="wan2",isp="isp-a",device="pppoe-wan2"} 0']),
          ("noc_wan_rate_bits", ['noc_wan_rate_bits{wan="wan1",isp="isp-a",direction="down"} 8000000',
                                 'noc_wan_rate_bits{wan="wan1",isp="isp-a",direction="up"} 1000000']),
          ("noc_wan_bytes_total", ['noc_wan_bytes_total{wan="wan1",isp="isp-a",direction="down"} 305',
                                   'noc_wan_bytes_total{wan="wan1",isp="isp-a",direction="up"} 31',
                                   'noc_wan_bytes_total{wan="wan2",isp="isp-a",direction="down"} 100',
                                   'noc_wan_bytes_total{wan="wan2",isp="isp-a",direction="up"} 10']),
          ("noc_wan_month_bytes", ['noc_wan_month_bytes{wan="wan1",isp="isp-a",direction="down"} 300',
                                   'noc_wan_month_bytes{wan="wan1",isp="isp-a",direction="up"} 30',
                                   'noc_wan_month_bytes{wan="wan2",isp="isp-a",direction="down"} 100',
                                   'noc_wan_month_bytes{wan="wan2",isp="isp-a",direction="up"} 10']),
          ("noc_device_month_bytes", ['noc_device_month_bytes{device="aa:00:00:00:00:05",name="phone",direction="down"} 250',
                                      'noc_device_month_bytes{device="aa:00:00:00:00:05",name="phone",direction="up"} 20']),
          ("noc_wan_latency_ms", ['noc_wan_latency_ms{wan="wan1"} 7.5']),
          ("noc_wan_loss_percent", ['noc_wan_loss_percent{wan="wan1"} 0', 'noc_wan_loss_percent{wan="wan2"} 100']),
          ("noc_wan_speedtest_mbps", ['noc_wan_speedtest_mbps{wan="wan1",isp="isp-a"} 320', 'noc_wan_speedtest_mbps{wan="wan2",isp="isp-a"} 39']),
          ("noc_ont_rx_dbm", ['noc_ont_rx_dbm{ont="ONT \\"a\\""} -18.27']),
          ("noc_usage_since_seconds", ["noc_usage_since_seconds 1795000000"]))
    @unpack
    def test_metric(self, name, want):
        self.assertEqual(lines(self.text, name), want)

    def test_help_and_type_once_each(self):
        for name, (kind, _) in M.HELP.items():
            self.assertEqual(self.text.count(f"# TYPE {name} "), 1, name)
            self.assertIn(f"# TYPE {name} {kind}", self.text)

    def test_missing_values_are_left_out(self):
        self.assertNotIn('wan="wan2"} None', self.text)
        self.assertEqual(lines(self.text, "noc_ont_tx_dbm"), ['noc_ont_tx_dbm{ont="ONT \\"a\\""} 2.43'], "the dead ONT has no reading")

    @data((5, "5"), (5.0, "5"), (19_191_700_000_123, "19191700000123"), (-18.27, "-18.27"), (0.1, "0.1"), (2.5e300, "2.5e+300"))
    @unpack
    def test_number_is_exact(self, v, want):
        self.assertEqual(M.number(v), want)

    def test_empty_store(self):
        text = M.render(M.samples({}, {}, {}, {}, set(), None, []))
        self.assertEqual(text, "\n")


API = TestClient(noc.app)


def set_token(token):
    (ETC / "secrets.env").write_text(SECRETS + (f"NOC_METRICS_TOKEN={token}\n" if token else ""))


@ddt
class Endpoint(unittest.TestCase):
    def setUp(self):
        noc.collect_router = lambda: ROUTER
        noc._cache.clear()
        noc.USAGE.store = json.loads(json.dumps(STORE))
        self.addCleanup(set_token, None)

    @data((None, None, 404), ("s3cret", None, 401), ("s3cret", "Bearer nope", 401), ("s3cret", "s3cret", 401), ("s3cret", "Bearer s3cret", 200))
    @unpack
    def test_token(self, token, header, status):
        set_token(token)
        r = API.get("/metrics", headers={"Authorization": header} if header else {})
        self.assertEqual(r.status_code, status)
        if status == 200:
            self.assertTrue(r.headers["content-type"].startswith("text/plain"))
            self.assertIn('noc_wan_month_bytes{wan="wan1",', r.text)

    def test_needs_no_login_session(self):
        set_token("s3cret")
        fresh = TestClient(noc.app)
        self.assertEqual(fresh.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code, 200)
        self.assertEqual(fresh.get("/api/usage").status_code, 401, "other pages still need the login")


def WAN():
    """A WAN the example config monitors (caps may name a WAN as well as an ISP group)."""
    return sorted(noc.speedtab.base(w) for w in noc.wan_ifaces())[0]


@ddt
class Settings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert API.post("/login", data={"user": "admin", "password": "test-pass"}, follow_redirects=False).status_code == 303

    def setUp(self):
        noc.C.pop("usage", None)
        noc.collect_router = lambda: ROUTER
        noc._cache.clear()

    def act(self, params):
        r = API.post("/api/actions/preview", json={"kind": "usage_set", "params": params})
        if r.status_code != 200:
            return r.status_code, r.json()
        p = r.json()
        return 200, p, API.post("/api/actions/confirm", json={"id": p["id"]}).json()

    def test_set_caps_and_summary(self):
        group = next(iter(noc.wan_groups()))
        got = self.act({"caps_gb": {group: "3000"}, "daily_summary": True, "summary_hour": 8})
        self.assertEqual(got[0], 200)
        self.assertIn(f"Cap {group}: 3000 GB a month (Telegram at 80% and 100%)", got[1]["detail"])
        self.assertIn("Daily summary on Telegram at 08:00", got[1]["detail"])
        saved = json.loads((ETC / "config.json").read_text())["usage"]
        self.assertEqual(saved, {"caps_gb": {group: 3000.0}, "daily_summary": True, "summary_hour": 8})
        u = API.get("/api/usage").json()
        self.assertEqual((u["settings"]["caps_gb"], u["settings"]["summary_hour"]), ({group: 3000.0}, 8))
        self.assertIn(group, u["groups"])

    @data(({"caps_gb": {"nope": 5}}, "not an ISP group or WAN"), ({"caps_gb": {"WAN": "x"}}, "number of GB"),
          ({"caps_gb": {"WAN": 0.5}}, "1 to 1000000"), ({"summary_hour": 24}, "0-23"), ({}, "nothing changed"),
          ({"caps_gb": []}, "caps_gb must be"))
    @unpack
    def test_refused(self, params, msg):
        wan = WAN()
        got = self.act(json.loads(json.dumps(params).replace('"WAN"', json.dumps(wan))))
        self.assertEqual(got[0], 400, got)
        self.assertIn(msg, json.dumps(got[1]))

    def test_empty_cap_removes_it(self):
        wan = WAN()
        noc.C["usage"] = {"caps_gb": {wan: 50}, "daily_summary": False, "summary_hour": 9}
        got = self.act({"caps_gb": {wan: ""}})
        self.assertIn(f"Cap {wan}: removed (was 50 GB)", got[1]["detail"])
        self.assertEqual(noc.C["usage"]["caps_gb"], {})


if __name__ == "__main__":
    unittest.main()
