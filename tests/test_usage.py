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


if __name__ == "__main__":
    unittest.main()
