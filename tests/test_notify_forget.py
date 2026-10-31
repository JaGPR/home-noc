"""Data-driven tests: the alert watcher forgets checks that no longer exist (app/notify.py, Telegram.forget).

Untracking a device, or removing a WAN, ONT or Pi-hole, must clear its open problem; a family that
failed to read this round (router SSH down, a stale ONT, a Pi-hole not answering) must keep its states.
Run:  python -m unittest tests/test_notify_forget.py

The integration test runs the real watcher once against the live NOC config and is skipped unless NOC_HOST is set
(SSH as root with a key), e.g.  NOC_HOST=192.0.2.20 NOC_GONE="Old device" python -m unittest tests/test_notify_forget.py
It only reads (pings, router and DNS queries); it starts no workers and sends no alerts.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import notify  # noqa: E402


class FakeNoc:
    VAR = Path(tempfile.mkdtemp())
    C = {}


def watcher(states):
    tg = notify.Telegram(FakeNoc())
    tg.states = {k: {"ok": ok, "since": 0, "streak": 0, "text": k} for k, ok in states.items()}
    return tg


DOWN = {"device:Bulb": False, "device:NAS": True, "wan:wan9": False, "wan:ALL": True,
        "ont:Old ONT:link": False, "ont:Old ONT:poll": True, "ont:ONT:link": False, "ont:ONT:poll": True,
        "dns:pi3": False, "block:pi3": False, "block:pi1": False, "dns:pi1": True, "source:router": True}


@ddt
class Forget(unittest.TestCase):
    @data(
        # untracked device: gone from a full device read -> forgotten
        ({"device:NAS": 1}, {"device:": None}, {"device:Bulb"}),
        # device read failed this round (not covered): keep everything
        ({}, {}, set()),
        # router read OK, wan9 removed from the WAN list
        ({"wan:ALL": 1}, {"wan:": None}, {"wan:wan9"}),
        # router SSH down: WAN family not covered, keep the down WAN
        ({"source:router": 1}, {}, set()),
        # ONT still listed but stale: its link check is skipped, keep it; an ONT no longer listed goes
        ({"ont:ONT:poll": 1}, {"ont:": {"ONT"}}, {"ont:Old ONT:link", "ont:Old ONT:poll"}),
        # Pi-hole pi3 removed; pi1 failing its blocking check this round is still listed, keep it
        ({"dns:pi1": 1}, {"dns:": None, "block:": {"pi1"}}, {"dns:pi3", "block:pi3"}),
    )
    @unpack
    def test_forget(self, present, covered, gone):
        tg = watcher(DOWN)
        readings = {k: (DOWN[k], k) for k in DOWN if k in present or not any(k.startswith(p) for p in covered)}
        readings.update({k: (DOWN[k], k) for k in present})
        tg.forget(readings, covered)
        self.assertEqual(set(DOWN) - set(tg.states), gone)

    def test_problem_clears(self):
        tg = watcher({"device:Bulb": False, "device:NAS": True})
        self.assertEqual([p["key"] for p in tg.problems()], ["device:Bulb"])
        tg.forget({"device:NAS": (True, "NAS")}, {"device:": None})
        self.assertEqual(tg.problems(), [], "an untracked device must not stay an open problem")

    def test_side_state_cleared(self):
        tg = watcher({"device:Bulb": False, "wan:wan9": False})
        tg.mgmt_down, tg.diag = {"device:Bulb": 1.0}, {"wan9": {"verdict": "x"}}
        tg.forget({}, {"device:": None, "wan:": None})
        self.assertEqual((tg.mgmt_down, tg.diag), ({}, {}))

    def test_readded_needs_confirmation(self):
        tg = watcher({"device:Bulb": False})
        tg.forget({}, {"device:": None})
        self.assertEqual(tg.step({"device:Bulb": (False, "Bulb")}, first=False), [],
                         "tracked again later, it starts as OK and needs the usual confirmation")


@unittest.skipUnless(os.environ.get("NOC_HOST"), "set NOC_HOST")
class Live(unittest.TestCase):
    """The running NOC must not list a check for a device it no longer tracks."""

    # Runs on the NOC with its real config (no workers started): one real evaluate() of every check,
    # with a stale state planted for a device that isn't tracked, then step() and forget().
    PROBE = r'''
import json, os, sys
os.environ["NOC_NO_WORKERS"] = "1"
sys.path.insert(0, "/opt/noc")
import app as noc
tg = noc.TG
readings, _ = tg.evaluate()
tg.step(readings, first=True)
tg.states["device:Untracked test device"] = {"ok": False, "since": 0, "streak": 0, "text": "x"}
tg.forget(readings, tg.covered)
print(json.dumps({"devices": [d["name"] for d in noc.C.get("devices", [])], "covered": sorted(tg.covered),
                  "states": sorted(tg.states), "problems": [p["key"] for p in tg.problems()]}))
'''

    def test_no_problem_for_untracked(self):
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", f"root@{os.environ['NOC_HOST']}", "python3", "-"],
                           input=self.PROBE, capture_output=True, text=True, timeout=120, check=True)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertIn("device:", got["covered"], "the live device checks must read in full")
        self.assertNotIn("device:Untracked test device", got["states"], "an untracked device must be forgotten")
        tracked = {f"device:{n}" for n in got["devices"]}
        self.assertTrue(tracked & set(got["states"]), "tracked devices must keep their state")
        stray = [k for k in got["problems"] if k.startswith("device:") and k not in tracked]
        self.assertEqual(stray, [], "open problems for devices that are no longer tracked")
        gone = os.environ.get("NOC_GONE")
        if gone:
            self.assertNotIn(gone, got["devices"], f"{gone} is still tracked")


if __name__ == "__main__":
    unittest.main()
