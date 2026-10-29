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

STATUS = {"temps": {"cpu": 40.0, "gpu": 38.0, "board": 35.0}, "hot_c": 85, "wanted": {"board": {"mode": "quiet"}}, "guard": {"running": True},
          "fans": [{"target": "board", "channel": 1, "id": "fan1", "name": "CPU fan", "rpm": 962, "pct": 36, "auto": True, "min_pct": 30},
                   {"target": "board", "channel": 2, "id": "fan2", "name": "Fan 2", "rpm": 896, "pct": 36, "auto": True, "min_pct": 25},
                   {"target": "gpu", "channel": 1, "name": "GPU fan", "rpm": 95, "pct": 0, "auto": True, "min_pct": 20}],
          "lights": {"available": True, "groups": {
              "ram": {"modes": ["Off", "Static", "Breathing", "Spectrum Cycle", "Rainbow"], "mode": "Rainbow", "wanted": None,
                      "devices": [{"name": "ENE DRAM", "mode": "Rainbow"}, {"name": "ENE DRAM", "mode": "Rainbow"}]},
              "board": {"modes": ["Off", "Static"], "mode": "Direct", "wanted": None,
                        "devices": [{"name": "Example board", "mode": "Direct"}]}}}}


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

    @data(("fan2", "manual", 70, "Fan 2: Fixed speed at 70 %", "noc-hw set fan2 manual 70", "Only this fan"),
          ("fan1", "full", None, "CPU fan: Full speed", "noc-hw set fan1 full", "Only this fan"),
          ("fan2", "group", None, "Fan 2: follow Case and CPU fans", "noc-hw set fan2 group", "(Quiet curve)"))
    @unpack
    def test_one_fan(self, target, mode, pct, summary, command, note):
        noc = FakeNoc()
        s, detail, _, run, _ = HW.plan(noc, "hw_fan", {"target": target, "mode": mode, "pct": pct})
        self.assertEqual(s, summary)
        self.assertEqual(detail[0], f"Now: {summary.split(':')[0]} " + ("962" if target == "fan1" else "896") + " rpm (auto)")
        self.assertTrue(any(note in d for d in detail), detail)
        self.assertEqual(any("100 % above" in d for d in detail), mode != "group")
        run()
        self.assertEqual(noc.commands[-1][1], command)

    @data(({"target": "fan3", "mode": "full"}, "not a fan the host controls"), ({"target": "board", "mode": "group"}, "mode must be"),
          ({"target": "gpu", "mode": "group"}, "mode must be"), ({"target": "fan2", "mode": "turbo"}, "or group"),
          ({"target": "fan2 x", "mode": "auto"}, "target"), ({"target": 2, "mode": "auto"}, "target"))
    @unpack
    def test_one_fan_rejects(self, params, msg):
        noc = FakeNoc()
        with self.assertRaisesRegex(ValueError, msg):
            HW.plan(noc, "hw_fan", params)
        self.assertFalse([c for c in noc.commands if " set " in c[1]])

    def test_now_lists_only_the_target(self):
        _, detail, *_ = HW.plan(FakeNoc(), "hw_fan", {"target": "gpu", "mode": "auto"})
        self.assertEqual(detail[0], "Now: GPU fan 95 rpm (auto)")


@ddt
class Light(unittest.TestCase):
    @data(({"group": "ram", "mode": "Static", "color": "#00aaff"}, "RAM lighting: Static #00AAFF", "noc-hw light ram Static 00AAFF"),
          ({"group": "ram", "mode": "Spectrum Cycle"}, "RAM lighting: Spectrum Cycle", "noc-hw light ram 'Spectrum Cycle'"),
          ({"group": "ram", "mode": "Off", "color": "FF0000"}, "RAM lighting: Off", "noc-hw light ram Off"),
          ({"group": "board", "mode": "Static", "color": ""}, "Motherboard lighting: Static", "noc-hw light board Static"))
    @unpack
    def test_summary_and_command(self, params, summary, command):
        noc = FakeNoc()
        s, detail, word, run, refresh = HW.plan(noc, "hw_light", params)
        self.assertEqual((s, word, refresh), (summary, None, ["hardware"]))
        run()
        self.assertEqual(noc.commands[-1], ("192.0.2.6", command))

    @data(({"group": "fans", "mode": "Off"}, "group must be"), ({"group": "gpu", "mode": "Off"}, "no gpu lighting"),
          ({"group": "ram", "mode": "Direct"}, "RAM lighting modes"), ({"group": "ram", "mode": "Off; reboot"}, "modes"),
          ({"group": "ram", "mode": "Static", "color": "red"}, "6 hex"), ({"group": "ram", "mode": "Static", "color": "FF00001"}, "6 hex"))
    @unpack
    def test_rejects(self, params, msg):
        noc = FakeNoc()
        with self.assertRaisesRegex(ValueError, msg):
            HW.plan(noc, "hw_light", params)
        self.assertFalse([c for c in noc.commands if "light" in c[1]], "nothing may be sent for a rejected action")

    @data(("ram", "Now: Rainbow on 2 device(s): ENE DRAM", "SMBus", False), ("board", "Now: Direct on 1 device(s)", None, True))
    @unpack
    def test_detail(self, group, now, extra, unreadable):
        _, detail, *_ = HW.plan(FakeNoc(), "hw_light", {"group": group, "mode": "Static"})
        self.assertTrue(detail[0].startswith(now), detail)
        if extra:
            self.assertTrue(any(extra in d for d in detail), detail)
        self.assertEqual(any("can't be read back" in d for d in detail), unreadable)

    def test_light_waits_longer(self):
        seen = []
        noc = FakeNoc()
        noc.ssh = lambda host, script, timeout=20, command="": seen.append(timeout) or json.dumps(
            {"verified": True} if " light " in command else STATUS)
        HW.plan(noc, "hw_light", {"group": "ram", "mode": "Off"})[3]()
        self.assertGreaterEqual(seen[-1], 60, "OpenRGB detects and reads back: give it time")


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
