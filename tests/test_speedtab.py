"""Data-driven tests of the Speed tab (app/speedtab.py): live load from the connection table, and on-demand tests.

Unit tests use made-up interfaces and addresses.  Run:  python -m unittest tests/test_speedtab.py

The integration test runs the real code on the NOC against the live router and is skipped unless NOC_HOST is set
(SSH as root with a key), e.g.  NOC_HOST=192.0.2.20 python -m unittest tests/test_speedtab.py
It reads the connection table twice while the NOC's own host downloads through one WAN, then tests that one WAN:
about SPEED_TEST_MB (default 100) MB of real download, and one line in the speed history.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import speedtab as S  # noqa: E402

V6 = "2001:0db8:0000:0000:0000:0000:0000:0001"
ADDRS = {"192.0.2.1": "wan1", "192.0.2.2": "wan2", V6: "wan1"}
T0 = 1000.0


def flow(reply_dst, orig, reply, src="10.0.0.5", dst="198.51.100.9", sport=4000, proto="tcp", dport=443):
    return f"{proto} {src} {dst} {sport} {dport} {orig} {reply_dst} {reply}"


def rates(*samples, gap=2.0):
    """Feed connection-table samples 2 s apart; -> the last result."""
    load, out = S.Load(), None
    for i, lines in enumerate(samples):
        out = load.update(S.parse_flows("\n".join(lines)), ADDRS, T0 + i * gap)
    return out


@ddt
class LoadMaths(unittest.TestCase):
    def test_first_sample_has_nothing_to_compare(self):
        self.assertEqual(rates([flow("192.0.2.1", 10, 10)]), {})

    @data(
        # (first sample, second sample, -> per WAN: download Mbps, upload Mbps, connections)
        ([flow("192.0.2.1", 0, 0)], [flow("192.0.2.1", 1_000_000, 25_000_000)], {"wan1": (100.0, 4.0, 1)}),
        # two connections on one WAN add up; another WAN is separate
        ([flow("192.0.2.1", 0, 0), flow("192.0.2.1", 0, 0, sport=4001), flow("192.0.2.2", 0, 0, sport=4002)],
         [flow("192.0.2.1", 0, 5_000_000), flow("192.0.2.1", 0, 5_000_000, sport=4001), flow("192.0.2.2", 0, 2_500_000, sport=4002)],
         {"wan1": (40.0, 0.0, 2), "wan2": (10.0, 0.0, 1)}),
        # a connection that started between samples: all its bytes belong to this interval
        ([flow("192.0.2.1", 0, 0)], [flow("192.0.2.1", 0, 0), flow("192.0.2.1", 0, 2_500_000, sport=5000)], {"wan1": (10.0, 0.0, 2)}),
        # a connection that ended does not pull the total below zero (per-connection, not a sum)
        ([flow("192.0.2.1", 0, 9_000_000), flow("192.0.2.1", 0, 0, sport=4001)], [flow("192.0.2.1", 0, 2_500_000, sport=4001)],
         {"wan1": (10.0, 0.0, 1)}),
        # a counter that went backwards (a reused tuple) counts as 0
        ([flow("192.0.2.1", 100, 9_000_000)], [flow("192.0.2.1", 50, 1)], {"wan1": (0.0, 0.0, 1)}),
        # IPv6 is added to its link
        ([flow(V6, 0, 0, src="2001:db8::5")], [flow(V6, 0, 2_500_000, src="2001:db8::5")], {"wan1": (10.0, 0.0, 1)}),
        # a forwarded port: traffic started outside, so the original direction is the download
        ([flow("10.0.0.5", 0, 0, src="203.0.113.7", dst="192.0.2.2")], [flow("10.0.0.5", 2_500_000, 500_000, src="203.0.113.7", dst="192.0.2.2")],
         {"wan2": (10.0, 2.0, 1)}),
        # LAN to LAN, or a WAN that isn't monitored: not counted
        ([flow("10.0.0.9", 0, 0, dst="10.0.0.9")], [flow("10.0.0.9", 99, 99, dst="10.0.0.9")], {}),
    )
    @unpack
    def test_rates(self, first, second, want):
        got = rates(first, second)
        self.assertEqual({k: (v["down_mbps"], v["up_mbps"], v["flows"]) for k, v in got.items()}, want)

    def test_long_gap_is_not_compared(self):
        """Every connection would look new, and show its whole lifetime's bytes as a huge spike."""
        self.assertEqual(rates([flow("192.0.2.1", 0, 0)], [flow("192.0.2.1", 0, 9_000_000_000)], gap=S.MAX_GAP_S + 1), {})

    def test_each_sample_compares_with_the_one_before(self):
        got = rates([flow("192.0.2.1", 0, 0)], [flow("192.0.2.1", 0, 5_000_000)], [flow("192.0.2.1", 0, 7_500_000)], gap=2.0)
        self.assertEqual(got["wan1"]["down_mbps"], 10.0)

    def test_parse_skips_bad_lines(self):
        self.assertEqual(S.parse_flows("rubbish\ntcp a b 1 2 x r 3\n\n" + flow("192.0.2.1", 1, 2)),
                         {"tcp 10.0.0.5 198.51.100.9 4000 443": ("192.0.2.1", 1, 2, "198.51.100.9")})

    def test_load_script_on_a_real_table(self):
        """The awk itself, on lines as the kernel writes them: established, offloaded and IPv6 ones."""
        table = (
            "ipv4     2 tcp      6 7393 ESTABLISHED src=10.0.0.5 dst=198.51.100.9 sport=25390 dport=443 packets=22 bytes=23187 "
            "src=198.51.100.9 dst=192.0.2.1 sport=443 dport=25390 packets=65 bytes=71060 [ASSURED] mark=768 zone=0 use=2\n"
            "ipv4     2 tcp      6 src=10.0.0.5 dst=198.51.100.8 sport=38489 dport=443 packets=31 bytes=5568 "
            "src=198.51.100.8 dst=192.0.2.2 sport=443 dport=38489 packets=49 bytes=85638032 [OFFLOAD] mark=256 zone=0 use=3\n"
            f"ipv6     10 udp      17 30 src=2001:0db8:0000:0000:0000:0000:0000:0005 dst=2001:0db8:0000:0000:0000:0000:0000:0009 sport=1 dport=53 packets=1 bytes=70 "
            f"src=2001:0db8:0000:0000:0000:0000:0000:0009 dst={V6} sport=53 dport=1 packets=1 bytes=120 mark=0 zone=0 use=2\n")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "conntrack"
            p.write_text(table)
            r = subprocess.run(["sh", "-c", S.LOAD_SCRIPT.replace("/proc/net/nf_conntrack", p.as_posix())], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = S.parse_flows(r.stdout)
        self.assertEqual(sorted((v[0], v[1], v[2]) for v in got.values()),
                         sorted([("192.0.2.1", 23187, 71060), ("192.0.2.2", 5568, 85638032), (V6, 70, 120)]))


IFACES = [{"name": "wan10", "device": "pppoe-wan10", "up": True, "ip": "192.0.2.10/32"}, {"name": "wan2", "device": "pppoe-wan2", "up": True, "ip": "192.0.2.2/32"},
          {"name": "wan2_6", "device": "pppoe-wan2", "up": True, "ip": "", "ip6": "2001:db8::1/64"}, {"name": "isp", "device": "eth0.20", "up": False, "ip": ""},
          {"name": "lan", "device": "br-lan", "up": True, "ip": "10.0.0.1/24"}]


@ddt
class Rows(unittest.TestCase):
    def test_rows(self):
        rows, addrs = S.wan_rows(IFACES, {"wan2", "wan2_6", "wan10", "isp"})
        self.assertEqual([(w["name"], w["device"], w["up"]) for w in rows],
                         [("isp", "eth0.20", False), ("wan2", "pppoe-wan2", True), ("wan10", "pppoe-wan10", True)], "wanN_6 merged, natural order")
        self.assertEqual(addrs, {"192.0.2.2": "wan2", "192.0.2.10": "wan10", V6: "wan2"}, "a WAN's IPv6 address, as the kernel writes it")

    def test_lan_is_not_a_wan(self):
        self.assertNotIn("10.0.0.1", S.wan_rows(IFACES, {"wan2"})[1])

    @data(("192.0.2.5/24", "192.0.2.5"), ("2001:DB8::1/64", V6), ("", None), (None, None), ("nonsense", None))
    @unpack
    def test_exploded(self, addr, want):
        self.assertEqual(S.exploded(addr), want)

    @data((None, ["wan2", "wan10"]), (["wan10"], ["wan10"]), (["wan10", "wan10"], ["wan10"]), ([], []))
    @unpack
    def test_pick(self, names, want):
        rows, _ = S.wan_rows(IFACES, {"wan2", "wan10", "isp"})
        self.assertEqual(S.pick(rows, names), want)

    @data((["lan"], "not a monitored WAN"), (["wan1; reboot"], "not a monitored WAN"), ("wan2", "must be a list"))
    @unpack
    def test_pick_rejects(self, names, msg):
        rows, _ = S.wan_rows(IFACES, {"wan2", "isp"})
        with self.assertRaisesRegex(ValueError, msg):
            S.pick(rows, names)


@ddt
class Script(unittest.TestCase):
    def test_one_by_one(self):
        s = S.test_script(["wan1"], "http://192.0.2.9/f.bin", 1000, 12)
        self.assertIn("mwan3 use wan1 curl -4 -s -o /dev/null -r 0-999 --max-time 12", s)
        self.assertNotIn("wait", s)

    def test_together_runs_in_the_background_and_waits(self):
        s = S.test_script(["wan1", "wan2"], "http://192.0.2.9/f.bin", 1000, 12, together=True)
        self.assertEqual(s.count(" &\n"), 2)
        self.assertTrue(s.endswith("wait\nexit 0\n"))

    def test_url_quoted(self):
        self.assertIn("'http://192.0.2.9/a b;reboot'", S.test_script(["wan1"], "http://192.0.2.9/a b;reboot", 10, 1))

    @data("wan1; reboot", "$(reboot)", "a b", "")
    def test_bad_name(self, name):
        with self.assertRaisesRegex(ValueError, "bad interface"):
            S.test_script([name], "http://192.0.2.9/f", 10, 1)

    def test_output_parses_with_the_noise_mwan3_prints(self):
        import speedwatch
        out = ("Running 'curl -4 -s' with DEVICE=eth0.20 SRCIP=192.0.2.1 FWMARK=0x3f00 FAMILY=ipv4\n"
               "isp 17149834 30000000 206\nwan3 0 0 000\n")
        self.assertEqual(speedwatch.parse(out), {"isp": 137, "wan3": None})


@ddt
class Timeout(unittest.TestCase):
    @data(False, True)
    def test_a_timeout_still_measures(self, together):
        """curl exits 28 at --max-time with its numbers printed: the script must still exit 0 (seen live on a slow link)."""
        stub = 'mwan3() { echo "wan1 4875000 58500000 206"; return 28; }\n'
        r = subprocess.run(["sh", "-c", stub + S.test_script(["wan1"], "http://192.0.2.9/f", 10, 1, together)], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "wan1 4875000 58500000 206"))


class FakeNoc:
    def __init__(self, replies, url="http://192.0.2.9/f.bin"):
        self.replies, self.scripts = dict(replies), []
        self.C = {"speedwatch": {"url": url, "test_timeout_s": 1, "test_bytes": 1000} if url else {}}
        self.ROUTER_HOST = "192.0.2.254"
        self.VAR = Path(tempfile.mkdtemp())
        self.gate = None

    def ssh(self, host, script, timeout=20):
        self.scripts.append(script)
        if self.gate:
            self.gate.wait(5)
        names = [ln.split()[2] for ln in script.splitlines() if ln.startswith("mwan3 use")]
        out = []
        for n in names:
            r = self.replies.get(n)
            if isinstance(r, Exception):
                raise r
            out.append(f"{n} {r} 1000 206" if r is not None else f"{n} 0 0 000")
        return "\n".join(out)

    def append_jsonl(self, path, entries):
        with open(path, "a") as f:
            f.write("".join(json.dumps(x) + "\n" for x in entries))

    def tail_jsonl(self, path, n):
        try:
            return [json.loads(x) for x in reversed(Path(path).read_text().splitlines())][:n]
        except OSError:
            return []

    def wan_ifaces(self):
        return {"wan1", "wan2"}

    def collect_router(self):
        return {"interfaces": [{"name": "wan1", "device": "pppoe-wan1", "up": True, "ip": "192.0.2.1/32"},
                               {"name": "wan2", "device": "pppoe-wan2", "up": True, "ip": "192.0.2.2/32"}]}

    def cached(self, key, ttl, fn, stale=0):
        return fn()


def finish(t):
    for _ in range(200):
        if not t.snapshot()["running"]:
            return t.snapshot()
        time.sleep(0.01)
    raise AssertionError("test run did not finish")


@ddt
class Runner(unittest.TestCase):
    def history(self, noc):
        return [json.loads(x) for x in (noc.VAR / "speedwatch.jsonl").read_text().splitlines()]

    def test_one_by_one(self):
        noc = FakeNoc({"wan1": 41250000, "wan2": 4875000})
        t = S.Tests(noc)
        t.start(["wan1", "wan2"])
        st = finish(t)
        self.assertEqual(st["results"], {"wan1": {"status": "done", "mbps": 330}, "wan2": {"status": "done", "mbps": 39}})
        self.assertIsNone(st["total_mbps"], "no total: they ran one after the other")
        self.assertEqual(len(noc.scripts), 2, "one connection per WAN, so results arrive one at a time")
        h = self.history(noc)
        self.assertEqual((h[0]["mbps"], h[0]["source"]), ({"wan1": 330, "wan2": 39}, "manual"))

    def test_together(self):
        noc = FakeNoc({"wan1": 41250000, "wan2": 4875000})
        t = S.Tests(noc)
        t.start(["wan1", "wan2"], together=True)
        st = finish(t)
        self.assertEqual((st["mode"], st["total_mbps"], len(noc.scripts)), ("together", 369, 1))
        self.assertEqual(self.history(noc)[0]["source"], "together")

    def test_one_fails(self):
        noc = FakeNoc({"wan1": 41250000, "wan2": None})
        t = S.Tests(noc)
        t.start(["wan1", "wan2"])
        st = finish(t)
        self.assertEqual((st["results"]["wan1"]["status"], st["results"]["wan2"]), ("done", {"status": "failed", "mbps": None}))
        self.assertEqual(self.history(noc)[0]["mbps"], {"wan1": 330, "wan2": None})

    def test_ssh_failure_is_reported_and_the_run_ends(self):
        noc = FakeNoc({"wan1": RuntimeError("router gone")})
        t = S.Tests(noc)
        t.start(["wan1", "wan2"])
        st = finish(t)
        self.assertIn("router gone", st["error"])
        self.assertEqual({r["status"] for r in st["results"].values()}, {"failed"}, "nothing left waiting")
        self.assertFalse((noc.VAR / "speedwatch.jsonl").exists(), "nothing measured: nothing recorded")

    def test_results_arrive_live(self):
        noc = FakeNoc({"wan1": 41250000})
        noc.gate = threading.Event()
        t = S.Tests(noc)
        st = t.start(["wan1"])
        self.assertTrue(st["running"])
        for _ in range(200):
            if t.snapshot()["results"]["wan1"]["status"] == "running":
                break
            time.sleep(0.01)
        self.assertEqual(t.snapshot()["results"]["wan1"]["status"], "running")
        with self.assertRaisesRegex(ValueError, "already running"):
            t.start(["wan1"])
        noc.gate.set()
        self.assertEqual(finish(t)["results"]["wan1"]["mbps"], 330)

    @data(({"url": None}, ["wan1"], "speedwatch.url"), ({}, [], "no WAN"))
    @unpack
    def test_wont_start(self, kw, names, msg):
        t = S.Tests(FakeNoc({}, **kw))
        with self.assertRaisesRegex(ValueError, msg):
            t.start(names)
        self.assertFalse(t.snapshot()["running"])


@ddt
class View(unittest.TestCase):
    def view(self, noc=None, table=""):
        noc = noc or FakeNoc({})
        orig = noc.ssh
        noc.ssh = lambda host, script, timeout=20: table if "nf_conntrack" in script else orig(host, script, timeout)
        return S.view(noc, S.Tests(noc), S.Load()), noc

    def test_shape(self):
        v, _ = self.view()
        self.assertEqual([w["name"] for w in v["wans"]], ["wan1", "wan2"])
        self.assertEqual((v["configured"], v["test_mb"], v["slow_mbps"], v["load_error"]), (True, 0, 100, None))
        self.assertIsNone(v["total_down_mbps"], "the first sample has nothing to compare: unknown, not zero")
        self.assertIsNone(v["wans"][0]["down_mbps"])

    def test_load_error_shown_and_tab_still_works(self):
        noc = FakeNoc({})
        noc.ssh = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ssh failed"))
        v = S.view(noc, S.Tests(noc), S.Load())
        self.assertIn("ssh failed", v["load_error"])

    def test_router_unreadable(self):
        noc = FakeNoc({})
        noc.collect_router = lambda: {"error": "no router"}
        with self.assertRaisesRegex(RuntimeError, "no router"):
            S.view(noc, S.Tests(noc), S.Load())

    def test_last_result_ignores_together_and_failures(self):
        noc = FakeNoc({})
        path = noc.VAR / "speedwatch.jsonl"
        noc.append_jsonl(path, [{"ts": "A", "mbps": {"wan1": 330, "wan2": 300}}, {"ts": "B", "mbps": {"wan1": 40, "wan2": None}, "source": "manual"},
                                {"ts": "C", "mbps": {"wan1": 99, "wan2": 99}, "source": "together"}])
        v, _ = self.view(noc)
        self.assertEqual({w["name"]: (w["last_mbps"], w["last_ts"]) for w in v["wans"]}, {"wan1": (40, "B"), "wan2": (300, "A")})
        self.assertEqual([(h["ts"], h["source"]) for h in v["history"]], [("A", "hourly"), ("B", "manual")], "oldest first, no together runs")

    @data((60, 48), (5, 5))
    @unpack
    def test_history_limit(self, n, want):
        self.assertEqual(len(S.history_rows([{"ts": str(i), "mbps": {}} for i in range(n)])), want)


@unittest.skipUnless(os.environ.get("NOC_HOST"), "set NOC_HOST")
class Live(unittest.TestCase):
    """The real router: load from its connection table while it downloads through a WAN, then a test of that WAN."""
    PROBE = r'''
import json, os, subprocess, sys, time, io, contextlib
os.environ["NOC_NO_WORKERS"] = "1"
sys.path.insert(0, "/opt/noc")
with contextlib.redirect_stdout(io.StringIO()):
    import app as noc, speedtab as S
me = sys.modules[__name__]
r = noc.collect_router()
rows, addrs = S.wan_rows(r["interfaces"], noc.wan_ifaces())
wan = next(w["name"] for w in rows if w["up"])
load = S.Load()
sample = lambda: load.update(S.parse_flows(noc.ssh(noc.ROUTER_HOST, S.LOAD_SCRIPT, timeout=8)), addrs, time.time())
sample()
url = (noc.C.get("speedwatch") or {})["url"]
noc.ssh(noc.ROUTER_HOST, f"(mwan3 use {wan} curl -4 -s -o /dev/null --max-time 6 {url} >/dev/null 2>&1 &) ; true", timeout=8)
peak = 0
for _ in range(4):
    time.sleep(1.5)
    peak = max(peak, (sample().get(wan) or {}).get("down_mbps") or 0)
time.sleep(3)
t = S.Tests(noc)
t.start([wan])
for _ in range(60):
    if not t.snapshot()["running"]:
        break
    time.sleep(1)
print(json.dumps({"wan": wan, "rows": [w["name"] for w in rows], "addrs": len(addrs), "peak": peak, "test": t.snapshot(),
                  "hist": noc.tail_jsonl(noc.VAR / "speedwatch.jsonl", 1)}))
'''

    def test_live(self):
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", f"root@{os.environ['NOC_HOST']}", "python3", "-"],
                           input=self.PROBE, capture_output=True, text=True, timeout=180, check=True)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        wan = got["wan"]
        self.assertGreater(got["peak"], 5, f"the load on {wan} must show the router's own download: {got}")
        res = got["test"]["results"][wan]
        self.assertEqual(res["status"], "done", got)
        self.assertGreater(res["mbps"], 0)
        self.assertEqual(got["hist"][0]["mbps"], {wan: res["mbps"]}, "the result joins the history")


if __name__ == "__main__":
    unittest.main()
