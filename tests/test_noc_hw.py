"""Data-driven tests of noc-hw, the Proxmox host fan program (deploy/proxmox/noc-hw).

Unit tests run it against a fake sysfs in a temp directory.  Run:  python -m unittest tests/test_noc_hw.py

The integration test drives the real host and is skipped unless PVE_HOST is set (SSH as root
with a key that may run noc-hw), e.g.  PVE_HOST=192.0.2.6 python -m unittest tests/test_noc_hw.py
It sets the GPU fan to 60 % and the board fans to full for a few seconds, then back to auto.
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

SRC = Path(__file__).resolve().parents[1] / "deploy" / "proxmox" / "noc-hw"
_loader = importlib.machinery.SourceFileLoader("noc_hw", str(SRC))
_spec = importlib.util.spec_from_loader("noc_hw", _loader)
H = importlib.util.module_from_spec(_spec)
_loader.exec_module(H)

CFG = {"board_chip": "nct6798", "channels": {"1": {"name": "CPU fan", "min_pct": 30}, "2": {"name": "Fan 2", "min_pct": 25}},
       "hot_c": 85, "interval_s": 3, "gpu_min_pct": 20}


class FakeHost:
    """A sysfs with coretemp, an nct6798 (pwm1, pwm2, pwm3 with no fan) and an amdgpu."""

    def __init__(self, cpu=50.0, gpu=45.0):
        self.root = Path(tempfile.mkdtemp())
        mon = self.root / "sys/class/hwmon"
        self.core, self.nct, self.gpu = mon / "hwmon5", mon / "hwmon2", mon / "hwmon8"
        for d, name in ((self.core, "coretemp"), (self.nct, "nct6798"), (self.gpu, "amdgpu")):
            d.mkdir(parents=True)
            (d / "name").write_text(name + "\n")
        (self.core / "temp1_label").write_text("Package id 0\n")
        (self.core / "temp2_label").write_text("Core 0\n")
        (self.core / "temp2_input").write_text("99000\n")
        self.temp(cpu=cpu, gpu=gpu)
        (self.nct / "temp1_input").write_text("35000\n")
        for ch, rpm in ((1, 1100), (2, 900), (3, 0)):
            (self.nct / f"pwm{ch}").write_text("96\n")
            (self.nct / f"pwm{ch}_enable").write_text("5\n")
            (self.nct / f"fan{ch}_input").write_text(f"{rpm}\n")
        (self.gpu / "pwm1").write_text("0\n")
        (self.gpu / "pwm1_enable").write_text("2\n")
        (self.gpu / "fan1_input").write_text("95\n")
        (self.root / "etc").mkdir()
        (self.root / "etc/noc-hw.json").write_text(json.dumps(CFG))
        for k, v in (("ROOT", self.root), ("CONF", self.root / "etc/noc-hw.json"), ("VAR", self.root / "var/lib/noc-hw"),
                     ("RUN", self.root / "run/noc-hw"), ("HWMON", mon)):
            setattr(H, k, v)

    def temp(self, cpu=None, gpu=None):
        if cpu is not None:
            (self.core / "temp1_input").write_text(f"{int(cpu * 1000)}\n")
        if gpu is not None:
            (self.gpu / "temp1_input").write_text(f"{int(gpu * 1000)}\n")

    def get(self, d, f):
        return int((d / f).read_text())

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


@ddt
class Policy(unittest.TestCase):
    @data(("quiet", 20, 25), ("quiet", 40, 25), ("quiet", 60, 38), ("quiet", 82, 100), ("quiet", 95, 100),
          ("balanced", 65, 60), ("balanced", 70, 70), ("full", 30, 100))
    @unpack
    def test_curve(self, mode, t, pct):
        self.assertEqual(H.curve(H.CURVES[mode], t), pct)

    @data((None, 50, None), ({"mode": "auto"}, 50, None), ({"mode": "manual", "pct": 40}, None, 40),
          ({"mode": "quiet"}, None, None), ({"mode": "balanced"}, 65, 60))
    @unpack
    def test_wanted_pct(self, want, t, pct):
        self.assertEqual(H.wanted_pct(want, t), pct)

    @data(
        # (wanted, cpu, gpu, hot before, -> plan, hot after)
        ({"board": {"mode": "quiet"}, "gpu": {"mode": "auto"}}, 50, 45, False, {"board": 28, "gpu": None}, False),
        ({"board": {"mode": "quiet"}}, 86, 45, False, {"board": 100, "gpu": None}, True),
        ({"gpu": {"mode": "manual", "pct": 30}}, 50, 90, False, {"board": None, "gpu": 100}, True),
        ({"board": {"mode": "manual", "pct": 30}}, 80, 45, True, {"board": 100, "gpu": None}, True),   # hysteresis
        ({"board": {"mode": "manual", "pct": 30}}, 74, 45, True, {"board": 30, "gpu": None}, False),
        ({}, 99, 99, False, {"board": None, "gpu": None}, True),   # auto stays with the firmware even when hot
        ({"board": {"mode": "balanced"}}, None, 45, False, {"board": None, "gpu": None}, False),  # no CPU temp: firmware
    )
    @unpack
    def test_decide(self, wanted, cpu, gpu, hot_was, plan, hot):
        self.assertEqual(H.decide(wanted, {"cpu": cpu, "gpu": gpu, "board": 35}, hot_was, 85), (plan, hot))


@ddt
class Sysfs(unittest.TestCase):
    def setUp(self):
        self.h = FakeHost()

    def tearDown(self):
        self.h.close()

    def test_temps(self):
        self.assertEqual(H.temps(CFG), {"cpu": 50.0, "gpu": 45.0, "board": 35.0})

    @data(("board", 50, 128, 30), ("board", 10, 76, 30), ("gpu", 10, 51, 20), ("gpu", 100, 255, 20))
    @unpack
    def test_manual_floor(self, target, pct, pwm, floor):
        H.apply(CFG, {target: pct})
        if target == "board":
            self.assertEqual(self.h.get(self.h.nct, "pwm1_enable"), 1)
            self.assertEqual(self.h.get(self.h.nct, "pwm1"), pwm)
            self.assertEqual(self.h.get(self.h.nct, "pwm3_enable"), 5, "a header outside the config is never touched")
        else:
            self.assertEqual((self.h.get(self.h.gpu, "pwm1_enable"), self.h.get(self.h.gpu, "pwm1")), (1, pwm))

    def test_restore_returns_firmware_mode(self):
        (self.h.nct / "pwm2_enable").write_text("2\n")       # header 2 was on Thermal Cruise
        H.apply(CFG, {"board": 60, "gpu": 60})
        H.apply(CFG, {"board": 70, "gpu": 70})               # already manual: the original is kept
        H.restore(CFG)
        self.assertEqual([self.h.get(self.h.nct, f"pwm{c}_enable") for c in (1, 2)], [5, 2])
        self.assertEqual(self.h.get(self.h.gpu, "pwm1_enable"), 2)

    def test_restore_without_record_uses_firmware_default(self):
        (self.h.nct / "pwm1_enable").write_text("1\n")
        H.restore(CFG)
        self.assertEqual(self.h.get(self.h.nct, "pwm1_enable"), 5)

    @data(("cpu", 0, 100), ("gpu", 0, 100), ("x", "auto", None), ("board", "turbo", None),
          ("board", "manual", None), ("board", "manual", "101"), ("gpu", "manual", "abc"))
    @unpack
    def test_set_rejects(self, target, mode, pct):
        with self.assertRaises(ValueError):
            H.set_mode(target, mode, pct)

    def test_tick_follows_wanted_then_hot_then_back(self):
        H.set_mode("board", "quiet")
        H.set_mode("gpu", "manual", 50)
        hot = H.tick(CFG, False)
        self.assertEqual((hot, self.h.get(self.h.nct, "pwm1")), (False, 76))
        self.assertEqual(self.h.get(self.h.gpu, "pwm1"), 128)
        self.h.temp(gpu=88)
        hot = H.tick(CFG, hot)
        self.assertEqual((hot, self.h.get(self.h.nct, "pwm1"), self.h.get(self.h.gpu, "pwm1")), (True, 255, 255))
        H.set_mode("board", "auto")
        H.set_mode("gpu", "auto")
        H.tick(CFG, hot)
        self.assertEqual((self.h.get(self.h.nct, "pwm1_enable"), self.h.get(self.h.gpu, "pwm1_enable")), (5, 2))

    def test_tick_error_hands_fans_back(self):
        H.set_mode("board", "full")
        H.tick(CFG, False)
        self.assertEqual(self.h.get(self.h.nct, "pwm1_enable"), 1)
        real = H.temps
        H.temps = lambda cfg: 1 / 0
        try:
            H.tick(CFG, False)
        finally:
            H.temps = real
        self.assertEqual(self.h.get(self.h.nct, "pwm1_enable"), 5)
        self.assertIn("ZeroDivisionError", json.loads((H.RUN / "guard.json").read_text())["error"])

    def test_status(self):
        H.set_mode("gpu", "manual", 40)
        H.tick(CFG, False)
        s = H.status(CFG)
        self.assertEqual([(f["target"], f["channel"], f["name"]) for f in s["fans"]],
                         [("board", 1, "CPU fan"), ("board", 2, "Fan 2"), ("gpu", 1, "GPU fan")])
        self.assertEqual(s["fans"][2]["pct"], 40)
        self.assertFalse(s["fans"][2]["auto"])
        self.assertTrue(s["fans"][0]["auto"])
        self.assertTrue(s["guard"]["running"])
        self.assertEqual(s["wanted"], {"gpu": {"mode": "manual", "pct": 40}})

    @data(("status", 0), ("set gpu manual 55", 0), ("restore", 0), ("guard", 2), ("ssh", 2), ("rm -rf /", 2),
          ("set board turbo", 1), ("", 2), ("status; reboot", 2), ("noc-hw status", 0),
          ("/usr/local/sbin/noc-hw set gpu auto", 0), ("noc-hw guard", 2), ("noc-hw ssh", 2), ("/bin/noc-hw status", 2))
    @unpack
    def test_forced_command(self, original, code):
        import contextlib
        import io

        def no_guard():
            raise AssertionError("the NOC's key started the guard loop")
        os.environ["SSH_ORIGINAL_COMMAND"] = original
        real, H.guard = H.guard, no_guard
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(H.main(["ssh"]), code)
        finally:
            H.guard = real
            os.environ.pop("SSH_ORIGINAL_COMMAND")


@unittest.skipUnless(os.environ.get("PVE_HOST"), "set PVE_HOST")
class Live(unittest.TestCase):
    """The real host through the NOC's restricted key: set, wait for the guard, check, back to auto."""

    def run_hw(self, cmd):
        # PVE_SSH_KEY / PVE_KNOWN_HOSTS: run it as the NOC does, e.g. from the NOC with its restricted key
        opts = [x for k, o in (("PVE_SSH_KEY", "-i"), ("PVE_KNOWN_HOSTS", "-oUserKnownHostsFile="))
                if os.environ.get(k) for x in ([o, os.environ[k]] if o == "-i" else [o + os.environ[k]])]
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", *opts, f"root@{os.environ['PVE_HOST']}", "noc-hw " + cmd],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def wait_for(self, cond, what):
        for _ in range(12):
            s = self.run_hw("status")
            if cond(s):
                return s
            time.sleep(1)
        self.fail(f"{what}: {json.dumps(s)}")

    def test_set_and_restore(self):
        s = self.run_hw("status")
        self.assertTrue(s["guard"]["running"], s["guard"])
        self.assertIsNotNone(s["temps"]["cpu"])
        board = [f for f in s["fans"] if f["target"] == "board"]
        self.assertTrue(board and all(f["rpm"] and f["rpm"] > 0 for f in board), board)
        before = {f["channel"]: f["rpm"] for f in board}
        try:
            self.run_hw("set gpu manual 60")
            self.run_hw("set board full")
            s = self.wait_for(lambda s: all(not f["auto"] for f in s["fans"]), "fans did not go manual")
            gpu = next(f for f in s["fans"] if f["target"] == "gpu")
            self.assertEqual(gpu["pct"], 60)
            self.assertTrue(all(f["pct"] == 100 for f in s["fans"] if f["target"] == "board"))
            time.sleep(4)
            s = self.run_hw("status")
            faster = [f for f in s["fans"] if f["target"] == "board" and f["rpm"] > before[f["channel"]] * 1.1]
            self.assertTrue(faster, f"no board fan sped up: before {before}, now {s['fans']}")
        finally:
            self.run_hw("set gpu auto")
            self.run_hw("set board auto")
        self.wait_for(lambda s: all(f["auto"] for f in s["fans"]), "fans did not return to auto")


if __name__ == "__main__":
    unittest.main()
