"""Data-driven tests of the NOC side of the Proxmox host fans (app/hardware.py).

The host side is tests/test_noc_hw.py.  Run:  python -m unittest tests/test_hardware.py
"""
import json
import sys
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import hardware as HW  # noqa: E402

STATUS = {"temps": {"cpu": 40.0, "gpu": 38.0, "board": 35.0}, "hot_c": 85, "wanted": {}, "guard": {"running": True},
          "fans": [{"target": "board", "channel": 1, "name": "CPU fan", "rpm": 962, "pct": 36, "auto": True, "min_pct": 30},
                   {"target": "board", "channel": 2, "name": "Fan 2", "rpm": 896, "pct": 36, "auto": True, "min_pct": 25},
                   {"target": "gpu", "channel": 1, "name": "GPU fan", "rpm": 95, "pct": 0, "auto": True, "min_pct": 20}]}


class FakeNoc:
    def __init__(self, reply=STATUS, host="192.0.2.6"):
        self.C = {"hardware": {"host": host}} if host else {}
        self.reply, self.commands = reply, []

    def ssh(self, host, script, timeout=20, command="sh -s"):
        self.commands.append((host, command))
        return json.dumps(self.reply)

    def cached(self, key, ttl, fn):
        return fn()


@ddt
class Plan(unittest.TestCase):
    @data(("board", "quiet", None, "Case and CPU fans: Quiet curve"), ("gpu", "manual", 60, "GPU fan: Fixed speed at 60 %"),
          ("gpu", "auto", None, "GPU fan: Auto (board / GPU firmware)"), ("board", "full", None, "Case and CPU fans: Full speed"))
    @unpack
    def test_summary_and_command(self, target, mode, pct, summary):
        noc = FakeNoc()
        s, detail, word, run, refresh = HW.plan(noc, "hw_fan", {"target": target, "mode": mode, "pct": pct})
        self.assertEqual((s, word, refresh), (summary, None, ["hardware"]))
        self.assertTrue(detail[0].startswith("Now: "))
        run()
        want = f"noc-hw set {target} {mode}" + (f" {pct}" if mode == "manual" else "")
        self.assertEqual(noc.commands[-1], ("192.0.2.6", want))

    @data(({"target": "cpu", "mode": "auto"}, "target"), ({"target": "gpu", "mode": "turbo"}, "mode"),
          ({"target": "gpu", "mode": "manual"}, "fan %"), ({"target": "gpu", "mode": "manual", "pct": 101}, "0-100"),
          ({"target": "gpu", "mode": "manual", "pct": "x; reboot"}, "fan %"), ({"target": "board; id", "mode": "auto"}, "target"))
    @unpack
    def test_rejects(self, params, msg):
        noc = FakeNoc()
        with self.assertRaisesRegex(ValueError, msg):
            HW.plan(noc, "hw_fan", params)
        self.assertFalse([c for c in noc.commands if "set" in c[1]], "nothing may be sent for a rejected action")

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            HW.plan(FakeNoc(), "hw_reboot", {})

    @data(("board", 10, "minimum: the host runs these fans at 30 %"), ("gpu", 5, "the host runs these fans at 20 %"))
    @unpack
    def test_floor_is_explained(self, target, pct, text):
        _, detail, *_ = HW.plan(FakeNoc(), "hw_fan", {"target": target, "mode": "manual", "pct": pct})
        self.assertTrue(any(text in d for d in detail), detail)

    @data(("auto", False), ("quiet", True), ("manual", True))
    @unpack
    def test_guard_explained_unless_auto(self, mode, mentioned):
        _, detail, *_ = HW.plan(FakeNoc(), "hw_fan", {"target": "board", "mode": mode, "pct": 50})
        self.assertEqual(any("100 % above 85" in d for d in detail), mentioned)

    def test_now_lists_only_the_target(self):
        _, detail, *_ = HW.plan(FakeNoc(), "hw_fan", {"target": "gpu", "mode": "auto"})
        self.assertEqual(detail[0], "Now: GPU fan 95 rpm (auto)")


@ddt
class Call(unittest.TestCase):
    def test_no_host(self):
        with self.assertRaisesRegex(RuntimeError, "hardware.host"):
            HW.collect(FakeNoc(host=None))

    def test_host_error_raised(self):
        with self.assertRaisesRegex(RuntimeError, "fan % must be"):
            HW.call(FakeNoc(reply={"error": "fan % must be 0-100"}), "set", "gpu", "manual", 500)

    @data((("status",), "noc-hw status"), (("set", "gpu", "manual", 5), "noc-hw set gpu manual 5"),
          (("set", "a b", "x'y"), "noc-hw set 'a b' 'x'\"'\"'y'"))
    @unpack
    def test_arguments_quoted(self, args, command):
        noc = FakeNoc()
        HW.call(noc, *args)
        self.assertEqual(noc.commands[-1][1], command)


if __name__ == "__main__":
    unittest.main()
