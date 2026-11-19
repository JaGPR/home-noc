"""Data used per WAN per month (app/usage.py): data-driven, offline.
Run:  python -m unittest tests/test_usage.py -v
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import usage as U  # noqa: E402

ADDRS = {"192.0.2.1": "wan1", "192.0.2.2": "wan2"}
T0 = 1000.0


def flow(reply_dst, orig, reply, src="10.0.0.5", dst="198.51.100.9", sport=4000):
    return f"tcp {src} {dst} {sport} 443 {orig} {reply_dst} {reply}"


def count(*samples, gap=30.0):
    """Feed connection-table samples `gap` seconds apart; -> every read's result."""
    c = U.Counter()
    return [c.update(U.speedtab.parse_flows("\n".join(s)), ADDRS, T0 + i * gap) for i, s in enumerate(samples)]


@ddt
class Counting(unittest.TestCase):
    def test_first_two_reads_only_build_the_baseline(self):
        got = count([flow("192.0.2.1", 0, 100)], [flow("192.0.2.1", 0, 200)], [flow("192.0.2.1", 50, 1200)])
        self.assertEqual(got, [{}, {}, {"wan1": [1000, 50]}])

    @data(
        # (reads after the baseline -> bytes per WAN in the last one)
        ([[flow("192.0.2.1", 0, 0)], [flow("192.0.2.1", 10, 5_000)]], {"wan1": [5_000, 10]}),
        # two WANs, each separately
        ([[flow("192.0.2.1", 0, 0), flow("192.0.2.2", 0, 0, sport=1)], [flow("192.0.2.1", 0, 7), flow("192.0.2.2", 3, 9, sport=1)]],
         {"wan1": [7, 0], "wan2": [9, 3]}),
        # a connection that started between reads counts whole
        ([[flow("192.0.2.1", 0, 0)], [flow("192.0.2.1", 0, 0), flow("192.0.2.1", 1, 2_000_000, sport=9)]], {"wan1": [2_000_000, 1]}),
        # one the earlier reads never saw, too big to be new: becomes the baseline, not a spike
        ([[flow("192.0.2.1", 0, 0)], [flow("192.0.2.1", 0, 0), flow("192.0.2.1", 0, 9_000_000_000, sport=9)]], {"wan1": [0, 0]}),
        # a forwarded port: the original direction is the download
        ([[flow("10.0.0.9", 0, 0, src="203.0.113.7", dst="192.0.2.2")], [flow("10.0.0.9", 400, 20, src="203.0.113.7", dst="192.0.2.2")]],
         {"wan2": [400, 20]}),
        # LAN to LAN: not counted
        ([[flow("10.0.0.9", 0, 0, dst="10.0.0.9")], [flow("10.0.0.9", 99, 99, dst="10.0.0.9")]], {}),
    )
    @unpack
    def test_counts(self, reads, want):
        self.assertEqual(count(reads[0], *reads)[-1], want)

    def test_connection_one_read_skipped_keeps_its_baseline(self):
        got = count([flow("192.0.2.1", 0, 500_000_000)], [flow("192.0.2.1", 0, 500_000_000)], [], [flow("192.0.2.1", 0, 500_001_000)])
        self.assertEqual(got[-1], {"wan1": [1_000, 0]})

    def read(self, c, uptime, *flows):
        return c.update(U.speedtab.parse_flows("\n".join(flows)), ADDRS, uptime)

    def test_router_restart_counts_everything_since_the_boot(self):
        """The table after a restart is all new: the first read counts it whole, then deltas carry on."""
        c = U.Counter()
        for up, b in ((50_000, 0), (50_030, 100), (50_060, 300)):
            self.read(c, up, flow("192.0.2.1", 0, b))
        self.assertEqual(self.read(c, 90, flow("192.0.2.1", 7, 5_000), flow("192.0.2.2", 1, 2, sport=1)), {"wan1": [5_000, 7], "wan2": [2, 1]})
        self.assertEqual(self.read(c, 120, flow("192.0.2.1", 7, 6_000), flow("192.0.2.2", 1, 2, sport=1)), {"wan1": [1_000, 0], "wan2": [0, 0]})

    def test_router_restart_noticed_late_builds_a_baseline(self):
        """Back with an uptime past the window: connections may predate what we saw, so nothing is guessed."""
        c = U.Counter()
        for up in (50_000, 50_030, 50_060):
            self.read(c, up, flow("192.0.2.1", 0, 0))
        self.assertEqual(self.read(c, U.REBOOT_WINDOW_S + 1, flow("192.0.2.1", 0, 9_000)), {})

    def test_totals_survive_a_router_restart(self):
        noc = FakeNoc([(50_000.0, flow("192.0.2.1", 0, 0)), (50_030.0, flow("192.0.2.1", 0, 0)), (50_060.0, flow("192.0.2.1", 0, 4_000)),
                       (60.0, flow("192.0.2.1", 0, 1_500))])
        c = U.Collector(noc)
        when = time.mktime((2026, 11, 19, 12, 0, 0, 0, 0, -1))
        for _ in range(4):
            c.step(now=when)
        self.assertEqual(c.store["months"]["2026-11"]["wan1"], [5_500, 0], "added to, never reset")

    def test_gap_starts_over(self):
        """The NOC was stopped: nothing is made up for that time."""
        c = U.Counter()
        for i, b in enumerate((0, 10, 20)):
            c.update(U.speedtab.parse_flows(flow("192.0.2.1", 0, b)), ADDRS, T0 + i * 30)
        self.assertEqual(c.update(U.speedtab.parse_flows(flow("192.0.2.1", 0, 9_999)), ADDRS, T0 + 60 + U.MAX_GAP_S + 1), {})


@ddt
class Store(unittest.TestCase):
    def test_add_to_month_and_day(self):
        when = time.mktime((2026, 11, 19, 12, 0, 0, 0, 0, -1))
        s = U.add(U.add({}, {"wan1": [100, 10]}, when), {"wan1": [5, 1], "wan2": [7, 0]}, when)
        self.assertEqual(s["months"]["2026-11"], {"wan1": [105, 11], "wan2": [7, 0]})
        self.assertEqual(s["days"]["2026-11-19"], {"wan1": [105, 11], "wan2": [7, 0]})

    def test_new_month(self):
        s = U.add({}, {"wan1": [1, 0]}, time.mktime((2026, 11, 30, 23, 59, 0, 0, 0, -1)))
        s = U.add(s, {"wan1": [2, 0]}, time.mktime((2026, 12, 1, 0, 1, 0, 0, 0, -1)))
        self.assertEqual((s["months"]["2026-11"]["wan1"], s["months"]["2026-12"]["wan1"]), ([1, 0], [2, 0]))

    def test_old_days_dropped_months_kept(self):
        s = {"days": {f"{y}-{m:02d}-{d:02d}": {} for y in (2024, 2025) for m in range(1, 13) for d in range(1, 29)},
             "months": {"2025-01": {"wan1": [1, 1]}}}
        s = U.add(s, {"wan1": [1, 0]}, time.mktime((2026, 11, 19, 12, 0, 0, 0, 0, -1)))
        self.assertEqual(len(s["days"]), U.DAYS_KEPT)
        self.assertIn("2026-11-19", s["days"])
        self.assertIn("2025-01", s["months"])

    def test_nothing_measured_changes_nothing(self):
        self.assertEqual(U.add({}, {}, T0), {})

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "usage.json"
            U.save(p, {"months": {"2026-11": {"wan1": [1, 2]}}})
            self.assertEqual(U.load(p), {"months": {"2026-11": {"wan1": [1, 2]}}})
            p.write_text("not json")
            self.assertEqual(U.load(p), {})
            self.assertEqual(U.load(Path(d) / "missing.json"), {})


ROWS = [{"name": "wan1", "device": "pppoe-wan1"}, {"name": "wan2", "device": "pppoe-wan2"}, {"name": "wan10", "device": "eth9"}]
GROUPS = {"isp-a": ["wan1", "wan2"], "isp-a-v6": ["wan1_6"], "isp-b": ["wan10"]}
STORE = {"since": 1795000000,
         "months": {"2026-10": {"wan1": [5, 1]}, "2026-11": {"wan1": [300, 30], "wan2": [100, 10], "wan10": [7, 0]}},
         "days": {"2026-10-31": {"wan1": [5, 1]}, "2026-11-01": {"wan1": [200, 20]}, "2026-11-02": {"wan1": [100, 10], "wan2": [100, 10], "wan10": [7, 0]}}}


@ddt
class View(unittest.TestCase):
    def test_newest_month_by_default(self):
        v = U.view(STORE, ROWS, GROUPS)
        self.assertEqual((v["month"], v["months"], v["since"]), ("2026-11", ["2026-11", "2026-10"], 1795000000))
        self.assertEqual([(w["wan"], w["device"], w["isp"], w["down_bytes"], w["up_bytes"]) for w in v["wans"]],
                         [("wan1", "pppoe-wan1", "isp-a", 300, 30), ("wan2", "pppoe-wan2", "isp-a", 100, 10), ("wan10", "eth9", "isp-b", 7, 0)])
        self.assertEqual(v["isps"], [{"isp": "isp-a", "down_bytes": 400, "up_bytes": 40}, {"isp": "isp-b", "down_bytes": 7, "up_bytes": 0}])
        self.assertEqual(v["days"], [{"day": "2026-11-01", "down_bytes": 200, "up_bytes": 20}, {"day": "2026-11-02", "down_bytes": 207, "up_bytes": 20}])
        self.assertEqual((v["total_down_bytes"], v["total_up_bytes"]), (407, 40))

    @data(("2026-10", "2026-10", 5), ("1999-01", "2026-11", 407), ("../etc", "2026-11", 407))
    @unpack
    def test_month_chosen(self, asked, month, down):
        v = U.view(STORE, ROWS, GROUPS, asked)
        self.assertEqual((v["month"], v["total_down_bytes"]), (month, down))

    def test_a_wan_that_no_longer_exists_still_shows(self):
        v = U.view({"months": {"2026-11": {"old": [9, 9]}}}, ROWS, GROUPS)
        self.assertIn("old", [w["wan"] for w in v["wans"]])

    def test_empty(self):
        v = U.view({}, ROWS, GROUPS)
        self.assertEqual((v["months"], v["since"], v["total_down_bytes"], v["days"]), ([], None, 0, []))
        self.assertEqual(len(v["wans"]), 3)


class FakeNoc:
    def __init__(self, tables):
        self.tables, self.VAR, self.ROUTER_HOST = list(tables), Path(tempfile.mkdtemp()), "192.0.2.254"

    def cached(self, key, ttl, fn, stale=0):
        return fn()

    def collect_router(self):
        return {"interfaces": [{"name": "wan1", "device": "pppoe-wan1", "up": True, "ip": "192.0.2.1/32"}]}

    def wan_ifaces(self):
        return {"wan1"}

    def ssh(self, host, script, timeout=20):
        uptime, table = self.tables.pop(0)
        return f"{uptime}\n@@NEIGH\n@@CT\n{table}"


class Collecting(unittest.TestCase):
    def test_reads_count_and_persist(self):
        noc = FakeNoc([(100.0, flow("192.0.2.1", 0, 0)), (130.0, flow("192.0.2.1", 0, 10)), (160.0, flow("192.0.2.1", 5, 1_010))])
        c = U.Collector(noc)
        when = time.mktime((2026, 11, 19, 12, 0, 0, 0, 0, -1))
        got = [c.step(now=when) for _ in range(3)]
        self.assertEqual(got, [{}, {}, {"wan1": [1_000, 5]}])
        saved = json.loads((noc.VAR / "usage.json").read_text())
        self.assertEqual((saved["months"]["2026-11"], saved["since"]), ({"wan1": [1_000, 5]}, int(when)))
        self.assertEqual(U.Collector(noc).store["months"]["2026-11"], {"wan1": [1_000, 5]}, "kept across a restart")

    def test_router_unreadable_raises_and_loop_keeps_going(self):
        noc = FakeNoc([])
        noc.collect_router = lambda: {"error": "no router"}
        with self.assertRaisesRegex(RuntimeError, "no router"):
            U.Collector(noc).step()


# ---------------------------------------------------------------- devices, alerts
WHEN = time.mktime((2026, 11, 19, 12, 0, 0, 0, 0, -1))
ROUTER = {"leases": [{"ip": "10.0.0.5", "mac": "aa:00:00:00:00:05", "name": "phone"}], "_arp": {"aa:00:00:00:00:06": "10.0.0.6"},
          "interfaces": [{"name": "wan1", "ip": "192.0.2.1/32", "ip6": ""}]}


@ddt
class Devices(unittest.TestCase):
    def test_counter_splits_by_device(self):
        c = U.Counter()
        lines = [[flow("192.0.2.1", 0, 0), flow("192.0.2.2", 0, 0, src="10.0.0.6", sport=1)]] * 2 + \
                [[flow("192.0.2.1", 10, 1_000), flow("192.0.2.2", 5, 300, src="10.0.0.6", sport=1)]]
        for i, s in enumerate(lines):
            c.update(U.speedtab.parse_flows("\n".join(s)), ADDRS, T0 + i * 30)
        self.assertEqual(c.hosts, {"10.0.0.5": {"wan1": [1_000, 10]}, "10.0.0.6": {"wan2": [300, 5]}})

    @data(("10.0.0.5", "aa:00:00:00:00:05"), ("10.0.0.6", "aa:00:00:00:00:06"), ("192.0.2.1", "router"), ("10.0.0.77", "10.0.0.77"))
    @unpack
    def test_identity(self, addr, key):
        v6 = U.speedtab.exploded("2001:db8::5")
        mac_of, own = U.identity_maps(ROUTER, {v6: "aa:00:00:00:00:05"})
        self.assertEqual(U.identity(addr, mac_of, own), key)
        self.assertEqual(U.identity(v6, mac_of, own), "aa:00:00:00:00:05", "IPv6 joins the same device")

    def test_add_devices(self):
        mac_of, own = U.identity_maps(ROUTER, {})
        s = U.add_devices({}, {"10.0.0.5": {"wan1": [100, 10], "wan2": [50, 5]}, "10.0.0.77": {"wan1": [7, 0]}}, mac_of, own, WHEN)
        s = U.add_devices(s, {"10.0.0.5": {"wan1": [1, 1]}}, mac_of, own, WHEN)
        self.assertEqual(s["devices"]["2026-11"]["aa:00:00:00:00:05"], {"ips": ["10.0.0.5"], "w": {"wan1": [101, 11], "wan2": [50, 5]}})
        self.assertEqual(s["device_days"]["2026-11-19"], {"aa:00:00:00:00:05": [151, 16], "10.0.0.77": [7, 0]})

    def test_device_days_kept_short(self):
        mac_of, own = U.identity_maps({}, {})
        s = {}
        for d in range(1, 13):
            s = U.add_devices(s, {"10.0.0.5": {"wan1": [1, 0]}}, mac_of, own, time.mktime((2026, 11, d, 12, 0, 0, 0, 0, -1)))
        self.assertEqual(len(s["device_days"]), U.DEVICE_DAYS_KEPT)
        self.assertEqual(s["devices"]["2026-11"]["10.0.0.5"]["w"]["wan1"], [12, 0], "the month keeps everything")

    @data(([{"name": "NAS", "target": "10.0.0.6"}], "aa:00:00:00:00:06", ["10.0.0.6"], "NAS"),
          ([{"name": "TV", "mac": "AA:00:00:00:00:06", "target": "x"}], "aa:00:00:00:00:06", [], "TV"),
          ([{"name": "Web", "target": "10.0.0.6:8080"}], "aa:00:00:00:00:06", ["10.0.0.6"], "Web"),
          (None, "aa:00:00:00:00:05", ["10.0.0.5"], "phone"),
          (None, "router", [], "Router (its own traffic)"),
          (None, "10.0.0.77", ["10.0.0.77"], ""))
    @unpack
    def test_names(self, devices, key, ips, want):
        self.assertEqual(U.namer(ROUTER, devices)(key, ips), want)

    def test_view_lists_devices_busiest_first(self):
        store = {"months": {"2026-11": {"wan1": [10, 1]}},
                 "devices": {"2026-11": {"aa:00:00:00:00:05": {"ips": ["10.0.0.5"], "w": {"wan1": [5, 1], "wan10": [100, 0]}},
                                         "10.0.0.77": {"ips": ["10.0.0.77"], "w": {"wan1": [7, 0]}}}}}
        v = U.view(store, ROWS, GROUPS, "2026-11", ROUTER, None)
        self.assertEqual([(d["key"], d["name"], d["mac"], d["down_bytes"], d["up_bytes"]) for d in v["devices"]],
                         [("aa:00:00:00:00:05", "phone", "aa:00:00:00:00:05", 105, 1), ("10.0.0.77", "", None, 7, 0)])
        self.assertEqual([(x["wan"], x["isp"], x["device"]) for x in v["devices"][0]["via"]],
                         [("wan10", "isp-b", "eth9"), ("wan1", "isp-a", "pppoe-wan1")])
        self.assertEqual(v["devices_total"], 2)


GB = 1_000_000_000
NAME = U.namer(ROUTER, None)


def at(h, d=19):
    return time.mktime((2026, 11, d, h, 0, 0, 0, 0, -1))


@ddt
class Alerts(unittest.TestCase):
    def store(self, a_gb, b_gb=0):
        return {"months": {"2026-11": {"wan1": [int(a_gb * GB * 0.9), int(a_gb * GB * 0.1)], "wan10": [int(b_gb * GB), 0]}}}

    @data((79, []), (80, ["🟠", "80% of its 100 GB cap"]), (99, ["🟠"]), (100, ["🔴", "100% of its 100 GB cap"]), (250, ["🔴", "250%"]))
    @unpack
    def test_cap_steps(self, used, want):
        msgs = U.check(self.store(used), {"caps_gb": {"isp-a": 100}, "daily_summary": False}, GROUPS, "Home", NAME, at(12))
        self.assertEqual(len(msgs), 1 if want else 0)
        for w in want:
            self.assertIn(w, msgs[0])

    def test_each_step_once_a_month(self):
        s, cfg = self.store(85), {"caps_gb": {"isp-a": 100}, "daily_summary": False}
        self.assertEqual(len(U.check(s, cfg, GROUPS, "Home", NAME, at(12))), 1)
        self.assertEqual(U.check(s, cfg, GROUPS, "Home", NAME, at(13)), [], "80% already sent")
        s["months"]["2026-11"]["wan1"][0] += 20 * GB
        self.assertIn("🔴", U.check(s, cfg, GROUPS, "Home", NAME, at(14))[0])
        self.assertEqual(U.check(s, cfg, GROUPS, "Home", NAME, at(15)), [])

    def test_straight_to_100_sends_one_message(self):
        msgs = U.check(self.store(120), {"caps_gb": {"isp-a": 100}, "daily_summary": False}, GROUPS, "Home", NAME, at(12))
        self.assertEqual(len(msgs), 1)

    def test_cap_on_one_wan(self):
        msgs = U.check(self.store(0, 9), {"caps_gb": {"wan10": 10}, "daily_summary": False}, GROUPS, "Home", NAME, at(12))
        self.assertIn("wan10", msgs[0])

    def test_summary_once_a_day_after_the_hour(self):
        s = {"days": {"2026-11-18": {"wan1": [40 * GB, 2 * GB], "wan10": [5 * GB, 0]}},
             "months": {"2026-11": {"wan1": [400 * GB, 20 * GB], "wan10": [5 * GB, 0]}},
             "device_days": {"2026-11-18": {"aa:00:00:00:00:05": [30 * GB, 1 * GB], "10.0.0.77": [2 * GB, 0], "router": [1, 0], "x": [0, 0]}},
             "devices": {"2026-11": {"aa:00:00:00:00:05": {"ips": ["10.0.0.5"], "w": {}}}}}
        cfg = {"summary_hour": 9}
        self.assertEqual(U.check(s, cfg, GROUPS, "Home", NAME, at(8)), [], "not before the hour")
        msgs = U.check(s, cfg, GROUPS, "Home", NAME, at(9))
        self.assertEqual(len(msgs), 1)
        m = msgs[0]
        for want in ("data used on 2026-11-18", "isp-a: 42.0 GB (↓40.0 GB ↑2.0 GB)", "isp-b: 5.0 GB", "Total: 47.0 GB",
                     "Top devices: phone 31.0 GB, 10.0.0.77 2.0 GB", "Month so far: 425.0 GB"):
            self.assertIn(want, m)
        self.assertLess(m.index("isp-a"), m.index("isp-b"), "busiest ISP first")
        self.assertEqual(U.check(s, cfg, GROUPS, "Home", NAME, at(20)), [], "once a day")

    @data(({"daily_summary": False}, {"days": {"2026-11-18": {"wan1": [1, 0]}}}), ({}, {}))
    @unpack
    def test_no_summary(self, cfg, store):
        """Off, or nothing counted yesterday: no message."""
        self.assertEqual(U.check(store, cfg, GROUPS, "Home", NAME, at(10)), [])

    @data(({}, {"caps_gb": {}, "daily_summary": True, "summary_hour": 9}),
          ({"caps_gb": {"a": "50", "b": 0}, "summary_hour": 7}, {"caps_gb": {"a": 50.0}, "daily_summary": True, "summary_hour": 7}))
    @unpack
    def test_settings(self, cfg, want):
        self.assertEqual(U.settings(cfg), want)

    @data((12e6, "12 MB"), (3.25e9, "3.2 GB"), (1.5e12, "1.50 TB"))
    @unpack
    def test_human(self, b, want):
        self.assertEqual(U.human(b), want)


class Telegram(unittest.TestCase):
    def test_collector_queues_what_is_due(self):
        noc = FakeNoc([(100.0, flow("192.0.2.1", 0, 0)), (130.0, flow("192.0.2.1", 0, 0)), (160.0, flow("192.0.2.1", 0, 90 * GB))])
        noc.C = {"usage": {"caps_gb": {"wan1": 100}, "daily_summary": False}}
        noc.SITE_NAME = "Home"
        noc.TG = type("TG", (), {"sent": [], "queue": lambda self, m: self.sent.append(m)})()
        c = U.Collector(noc)
        for _ in range(3):
            c.step(now=WHEN)
        self.assertEqual(len(noc.TG.sent), 1)
        self.assertIn("90% of its 100 GB cap", noc.TG.sent[0])
        self.assertIn("wan1:80", json.loads((noc.VAR / "usage.json").read_text())["sent"]["caps"]["2026-11"], "remembered across restarts")
        self.assertGreater(c.rates["wan1"][0], 0, "bits per second kept for /metrics")


if __name__ == "__main__":
    unittest.main()
