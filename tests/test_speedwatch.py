"""Data-driven tests of the per-session PPPoE speed watch (app/speedwatch.py).

Unit tests use made-up interface names and numbers.  Run:  python -m unittest tests/test_speedwatch.py

The integration test runs the real router script and is skipped unless ROUTER_HOST is set
(SSH as root with a key), e.g.  ROUTER_HOST=192.0.2.1 SPEED_URL=http://... python -m unittest tests/test_speedwatch.py
It only downloads (about 40 MB per PPPoE session) and changes nothing on the router.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import speedwatch as S  # noqa: E402

T0 = 1_000_000.0


@ddt
class Parse(unittest.TestCase):
    @data(("wan1 41250000 40000000 206", {"wan1": 330}),
          ("wan1 4875000 40000000 200", {"wan1": 39}),
          ("wan2 down", {"wan2": "down"}),
          ("wan3 0 0 000", {"wan3": None}),             # no connection
          ("wan3 5000 1200 404", {"wan3": None}),       # not the file
          ("wan4 abc 1 206", {"wan4": None}),
          ("garbage\n\nwan5 1 2", {}),
          ("wan1 41250000 40000000 206\nwan2 down\nwan3 4875000 9000000 206",   # a timeout still measures
           {"wan1": 330, "wan2": "down", "wan3": 39}))
    @unpack
    def test_parse(self, text, want):
        self.assertEqual(S.parse(text), want)

    @data(({"a": 39}, {"a": 320}, {"a": 320}),
          ({"a": 320}, {"a": 39}, {"a": 320}),
          ({"a": None}, {"a": 40}, {"a": 40}),
          ({"a": "down"}, {"a": None}, {"a": None}),
          ({"a": 40}, {}, {"a": 40}),
          ({}, {"b": "down"}, {"b": "down"}))
    @unpack
    def test_best(self, a, b, want):
        self.assertEqual(S.best(a, b), want)


@ddt
class Assess(unittest.TestCase):
    @data(({"w1": 330, "w2": 39, "w3": None, "w4": "down"}, {"w2": 39}, {"w1": 330}, ["w3"], False),
          ({"w1": 99, "w2": 100}, {"w1": 99}, {"w2": 100}, [], False),
          ({"w1": 40, "w2": 50}, {"w1": 40, "w2": 50}, {}, [], True),
          ({"w1": None, "w2": "down"}, {}, {}, ["w1"], False),
          ({}, {}, {}, [], False))
    @unpack
    def test_assess(self, speeds, slow, fast, failed, overall):
        self.assertEqual(S.assess(speeds, 100), {"slow": slow, "fast": fast, "failed": failed, "overall": overall})

    @data((0, "0 min"), (59 * 60, "59 min"), (3600, "1 h 0 min"), (3 * 3600 + 125, "3 h 2 min"))
    @unpack
    def test_ago(self, s, want):
        self.assertEqual(S.ago(s), want)


def step(prev, speeds, now=T0):
    return S.changes(prev, S.assess(speeds, 100), now, "Home")


@ddt
class Changes(unittest.TestCase):
    @data(
        # (previous state, readings, alerts expected (substrings, in order), new state)
        ({}, {"w1": 330, "w2": 320}, [], {"slow": {}, "overall": None}),
        ({}, {"w1": 330, "w3": 39, "w2": 310}, ["<b>w3</b> is slow: 39 Mbps (others 310-330 Mbps)"],
         {"slow": {"w3": T0}, "overall": None}),
        ({"slow": {"w3": T0 - 600}}, {"w1": 330, "w3": 38}, [], {"slow": {"w3": T0 - 600}, "overall": None}),
        ({"slow": {"w3": T0 - 7500}}, {"w1": 330, "w3": 320}, ["<b>w3</b> is back to 320 Mbps (slow for 2 h 5 min)"],
         {"slow": {}, "overall": None}),
        ({"slow": {"w3": T0 - 60}}, {"w1": 330, "w3": "down"}, [], {"slow": {"w3": T0 - 60}, "overall": None}),
        ({"slow": {"w3": T0 - 60}}, {"w1": 330, "w3": None}, [], {"slow": {"w3": T0 - 60}, "overall": None}),
        ({}, {"w1": 40, "w2": 60}, ["every PPPoE session is slow (40-60 Mbps)"], {"slow": {}, "overall": T0}),
        ({"slow": {"w3": T0 - 60}, "overall": T0 - 60}, {"w1": 40, "w3": 30}, [],
         {"slow": {"w3": T0 - 60}, "overall": T0 - 60}),
        ({"overall": T0 - 1800}, {"w1": 330, "w2": 320}, ["fast again (320-330 Mbps, all slow for 30 min)"],
         {"slow": {}, "overall": None}),
        ({"overall": T0 - 1800}, {"w1": 330, "w2": 30}, ["fast again (330 Mbps", "<b>w2</b> is slow: 30 Mbps (others 330 Mbps)"],
         {"slow": {"w2": T0}, "overall": None}),
        ({"overall": T0 - 60}, {"w1": "down"}, [], {"slow": {}, "overall": T0 - 60}),
        ({}, {"w<1>": 20, "w2": 300}, ["<b>w&lt;1&gt;</b> is slow"], {"slow": {"w<1>": T0}, "overall": None}),
        (None, {"w1": 20, "w2": 300}, ["<b>w1</b> is slow"], {"slow": {"w1": T0}, "overall": None}),
    )
    @unpack
    def test_changes(self, prev, speeds, alerts, state):
        msgs, new = step(prev, speeds)
        self.assertEqual(len(msgs), len(alerts), msgs)
        for m, want in zip(msgs, alerts):
            self.assertIn(want, m)
            self.assertIn("<b>Home</b>", m)
        self.assertEqual(new, state)

    def test_lifecycle(self):
        """Slow -> still slow (no repeat) -> recovered, one message each way."""
        state, sent = {}, []
        for i, w3 in enumerate([320, 39, 40, 41, 330, 325]):
            msgs, state = step(state, {"w1": 330, "w3": w3}, T0 + i * 3600)
            sent += msgs
        self.assertEqual(len(sent), 2)
        self.assertIn("is slow: 39", sent[0])
        self.assertIn("back to 330 Mbps (slow for 3 h 0 min)", sent[1])


@ddt
class Script(unittest.TestCase):
    @data((40_000_000, 10, "-r 0-39999999 --max-time 10"), (1000, 5, "-r 0-999 --max-time 5"))
    @unpack
    def test_script(self, nbytes, timeout, want):
        s = S.router_script("http://192.0.2.9/f.bin", nbytes, timeout)
        self.assertIn(want, s)
        self.assertIn("--interface pppoe-$i", s)
        self.assertIn("proto='pppoe'", s)

    def test_url_quoted(self):
        self.assertIn("'http://192.0.2.9/a b;rm -rf /'", S.router_script("http://192.0.2.9/a b;rm -rf /", 10, 1))

    @unittest.skipUnless(subprocess.run(["sh", "-c", "true"], capture_output=True).returncode == 0, "no sh")
    def test_script_runs_on_a_stub_router(self):
        """The script itself, with uci / ifstatus / jsonfilter / curl stubbed."""
        stub = r"""
uci() { printf "%s\n" "network.wan1.proto='pppoe'" "network.lan.proto='static'" "network.wan2.proto='pppoe'" "network.wan2x.device='y'"; }
ifstatus() { echo "$1"; }
jsonfilter() { read n; [ "$n" = wan2 ] && echo false || echo true; }
curl() { for a; do last="$a"; done; echo "wan1 41250000 40000000 206"; }
"""
        r = subprocess.run(["sh", "-c", stub + S.router_script("http://192.0.2.9/f", 100, 1)],
                           capture_output=True, text=True)
        self.assertEqual(S.parse(r.stdout), {"wan1": 330, "wan2": "down"})


class FakeNoc:
    def __init__(self, outputs, var):
        self.outputs, self.calls, self.sent = list(outputs), 0, []
        self.C = {"speedwatch": {"url": "http://192.0.2.9/f", "retest_s": 0}}
        self.ROUTER_HOST, self.SITE_NAME, self.VAR = "192.0.2.1", "Home", var
        noc = self

        class TG:
            token = staticmethod(lambda: "t")
            chat_id = staticmethod(lambda: 1)
            send = staticmethod(lambda chat, text: noc.sent.append(text))
        self.TG = TG

    def ssh(self, host, script, timeout=20):
        self.calls += 1
        return self.outputs.pop(0)

    def append_jsonl(self, path, entries):
        with open(path, "a") as f:
            f.write("".join(__import__("json").dumps(x) + "\n" for x in entries))


@ddt
class Run(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.var = Path(tempfile.mkdtemp())

    @data(
        # (router outputs, ssh calls, alerts sent)
        (["wan1 41250000 1 206\nwan2 40000000 1 206"], 1, 0),                                # all fast: no retest
        (["wan1 41250000 1 206\nwan2 4875000 1 206", "wan1 41250000 1 206\nwan2 4875000 1 206"], 2, 1),
        (["wan1 41250000 1 206\nwan2 4875000 1 206", "wan1 41250000 1 206\nwan2 40000000 1 206"], 2, 0),  # blip
    )
    @unpack
    def test_run(self, outputs, calls, alerts):
        noc = FakeNoc(outputs, self.var)
        S.run(noc, now=T0)
        self.assertEqual((noc.calls, len(noc.sent)), (calls, alerts))
        self.assertTrue((self.var / "speedwatch.json").exists())
        self.assertEqual(len((self.var / "speedwatch.jsonl").read_text().splitlines()), 1)

    def test_dry_run_saves_and_sends_nothing(self):
        noc = FakeNoc(["wan1 41250000 1 206\nwan2 4875000 1 206"] * 2, self.var)
        msgs = S.run(noc, dry_run=True, now=T0)
        self.assertEqual((len(msgs), noc.sent, list(self.var.iterdir())), (1, [], []))

    def test_no_url(self):
        noc = FakeNoc([], self.var)
        noc.C = {}
        with self.assertRaises(SystemExit):
            S.run(noc)


@unittest.skipUnless(os.environ.get("ROUTER_HOST") and os.environ.get("SPEED_URL"), "set ROUTER_HOST and SPEED_URL")
class Live(unittest.TestCase):
    def test_router(self):
        host = os.environ["ROUTER_HOST"]
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", f"root@{host}", "sh -s"],
                           input=S.router_script(os.environ["SPEED_URL"], 10_000_000, 10).encode(),  # bytes: no CRLF on Windows
                           capture_output=True, timeout=240)
        out = r.stdout.decode()
        speeds = S.parse(out)
        self.assertTrue(speeds, out + r.stderr.decode())
        print(speeds)
        for k, v in speeds.items():
            self.assertTrue(v == "down" or v is None or (isinstance(v, int) and v >= 0), (k, v))
        self.assertTrue(any(isinstance(v, int) and v > 0 for v in speeds.values()), speeds)


if __name__ == "__main__":
    unittest.main()
