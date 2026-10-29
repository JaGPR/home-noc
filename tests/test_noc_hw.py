"""Data-driven tests of noc-hw, the Proxmox host fan program (deploy/proxmox/noc-hw).

Unit tests run it against a fake sysfs in a temp directory.  Run:  python -m unittest tests/test_noc_hw.py

The integration test drives the real host and is skipped unless PVE_HOST is set (SSH as root
with a key that may run noc-hw), e.g.  PVE_HOST=192.0.2.6 python -m unittest tests/test_noc_hw.py
It sets the GPU fan to 60 % and the board fans to full for a few seconds, then back to auto.
With PVE_LIGHTS=1 it also sets the RAM and GPU lighting to static and puts back what was there.
"""
import importlib.machinery
import importlib.util
import json
import os
import shlex
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
                     ("RUN", self.root / "run/noc-hw"), ("HWMON", mon), ("OPENRGB", self.root / "opt/openrgb/AppRun")):
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
        # one header with its own mode: in the plan beside board; hot raises it too; auto stays firmware
        ({"board": {"mode": "quiet"}, "fan2": {"mode": "manual", "pct": 70}}, 50, 45, False,
         {"board": 28, "gpu": None, "fan2": 70}, False),
        ({"fan2": {"mode": "manual", "pct": 40}}, 90, 45, False, {"board": None, "gpu": None, "fan2": 100}, True),
        ({"board": {"mode": "full"}, "fan1": {"mode": "auto"}}, 50, 45, False, {"board": 100, "gpu": None, "fan1": None}, False),
        ({"fan2": {"mode": "balanced"}}, None, 45, False, {"board": None, "gpu": None, "fan2": None}, False),
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

    @data(
        # (wanted, -> (pwm1_enable, pwm1), (pwm2_enable, pwm2))
        ({"board": {"mode": "manual", "pct": 50}, "fan2": {"mode": "manual", "pct": 80}}, (1, 128), (1, 204)),
        ({"fan2": {"mode": "manual", "pct": 60}}, (5, 96), (1, 153)),               # only header 2 leaves firmware
        ({"board": {"mode": "full"}, "fan1": {"mode": "auto"}}, (5, 96), (1, 255)),  # header 1 back to firmware
        ({"fan2": {"mode": "manual", "pct": 5}}, (5, 96), (1, 64)),                 # its own 25 % floor
    )
    @unpack
    def test_one_fan_own_mode(self, wanted, fan1, fan2):
        for target, w in wanted.items():
            H.set_mode(target, w["mode"], w.get("pct"), cfg=CFG)
        H.tick(CFG, False)
        got = [(self.h.get(self.h.nct, f"pwm{c}_enable"), self.h.get(self.h.nct, f"pwm{c}")) for c in (1, 2)]
        self.assertEqual(got, [fan1, fan2])
        self.assertEqual(self.h.get(self.h.nct, "pwm3_enable"), 5, "a header outside the config is never touched")

    def test_group_follows_board_again(self):
        H.set_mode("board", "full", cfg=CFG)
        H.set_mode("fan2", "manual", 40, cfg=CFG)
        H.tick(CFG, False)
        self.assertEqual(self.h.get(self.h.nct, "pwm2"), 102)
        self.assertEqual(H.set_mode("fan2", "group", cfg=CFG), {"board": {"mode": "full"}})
        H.tick(CFG, False)
        self.assertEqual(self.h.get(self.h.nct, "pwm2"), 255)

    @data(("fan3", "auto", None, "not a controlled"), ("fan9", "full", None, "not a controlled"),
          ("fan2", "turbo", None, "or group"), ("board", "group", None, "mode must be"), ("gpu", "group", None, "mode must be"),
          ("fan2", "manual", "x", "fan %"), ("fan", "auto", None, "target must be"), ("fan2;id", "auto", None, "target must be"))
    @unpack
    def test_one_fan_rejects(self, target, mode, pct, msg):
        with self.assertRaisesRegex(ValueError, msg):
            H.set_mode(target, mode, pct, cfg=CFG)
        self.assertEqual(H.load_wanted(), {})

    def test_status_lists_fan_ids(self):
        self.assertEqual([f.get("id") for f in H.status(CFG)["fans"]], ["fan1", "fan2", None])

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
          ("/usr/local/sbin/noc-hw set gpu auto", 0), ("noc-hw guard", 2), ("noc-hw ssh", 2), ("/bin/noc-hw status", 2),
          ("set fan2 manual 60", 0), ("set fan2 group", 0), ("set fan3 full", 1))
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


# OpenRGB --list-detailed as this host prints it (DRAM and GPU report their mode, the Aura USB board always Direct)
LD = """0: ENE DRAM
  Type:           DRAM
  Description:    ENE SMBus Device
  Location:       I2C: SMBus I801 adapter at 0000:00:1f.4 (/dev/i2c-0), address 0x71
  Modes: {ram0}
  Zones: DRAM
  LEDs: 'DRAM LED 1' 'DRAM LED 2' 'DRAM LED 3' 'DRAM LED 4' 'DRAM LED 5' 'DRAM LED 6' 'DRAM LED 7' 'DRAM LED 8'

1: ENE DRAM
  Type:           DRAM
  Location:       I2C: SMBus I801 adapter at 0000:00:1f.4 (/dev/i2c-0), address 0x73
  Modes: {ram1}
  Zones: DRAM
  LEDs: 'DRAM LED 1' 'DRAM LED 2'

2: Sapphire Radeon RX 570/580/590 Nitro+
  Type:           GPU
  Location:       I2C: AMDGPU i2c bit bus OEM 0x97 (/dev/i2c-4), address 0x55
  Modes: 'Sapphire Blue' 'Rainbow Wave' 'PCB Temperature' 'Fan Speed' {gpu}
  Zones: 'GPU Zone'
  LEDs: 'GPU LED'

3: Example B660 board
  Type:           Motherboard
  Location:       HID: /dev/hidraw0
  Modes: [Direct] Off Static Breathing Flashing 'Spectrum Cycle' Rainbow 'Chase Fade' Chase 'Red Pulse'
  Zones: 'Aura Mainboard' 'Addressable RGB Header 1'
  LEDs: 'Aura Mainboard, LED 1' 'Aura Mainboard, LED 2'

4: Some keyboard
  Type:           Keyboard
  Modes: [Static]
"""


def mark(modes, active):
    """Modes as OpenRGB prints them: 'Two words' quoted, the active one in brackets, e.g. ['PCB Temperature']."""
    q = lambda m: f"'{m}'" if " " in m else m
    return " ".join(f"[{q(m)}]" if m == active else q(m) for m in modes)


def ram_modes(active):
    return mark(("Direct", "Off", "Static", "Breathing", "Flashing", "Spectrum Cycle", "Rainbow", "Chase Fade", "Chase",
                 "Random Flicker"), active)


class FakeOpenRGB:
    """Stands in for run_openrgb: applies --device/--mode to its state; stuck= a mode the RAM ignores."""

    def __init__(self, stuck=None):
        self.modes = {0: "Rainbow", 1: "Rainbow", 2: "Off", 3: "Direct"}
        self.calls, self.stuck = [], stuck

    def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        dev = None
        for a, b in zip(args, args[1:]):
            if a == "--device":
                dev = int(b)
            elif a == "--mode" and dev != 3 and b != self.stuck:   # the board never reports its mode
                self.modes[dev] = b
        return LD.format(ram0=ram_modes(self.modes[0]), ram1=ram_modes(self.modes[1]), gpu=mark(("Static", "Off"), self.modes[2]))


@ddt
class Lights(unittest.TestCase):
    def setUp(self):
        self.h = FakeHost()
        H.OPENRGB.parent.mkdir(parents=True)
        H.OPENRGB.write_text("")
        self.rgb, self.real = FakeOpenRGB(), H.run_openrgb
        H.run_openrgb = self.rgb

    def tearDown(self):
        H.run_openrgb = self.real
        self.h.close()

    def test_parse(self):
        devs = H.parse_devices(LD.format(ram0=ram_modes("Rainbow"), ram1=ram_modes("Spectrum Cycle"), gpu="Static Off"))
        self.assertEqual([(d["index"], d["type"], d["mode"], d["leds"]) for d in devs],
                         [(0, "DRAM", "Rainbow", 8), (1, "DRAM", "Spectrum Cycle", 2), (2, "GPU", None, 1),
                          (3, "Motherboard", "Direct", 2), (4, "Keyboard", "Static", 0)])
        self.assertIn("Spectrum Cycle", devs[0]["modes"])
        self.assertEqual(devs[3]["zones"], ["Aura Mainboard", "Addressable RGB Header 1"])
        self.assertEqual(devs[0]["location"][:4], "I2C:")

    def test_groups(self):
        g = H.lights()["groups"]
        self.assertEqual(sorted(g), ["board", "gpu", "ram"], "a keyboard is not a lighting group here")
        self.assertEqual((len(g["ram"]["devices"]), g["ram"]["mode"]), (2, "Rainbow"))
        self.assertNotIn("Direct", g["ram"]["modes"])
        self.assertEqual(g["gpu"]["modes"], ["Sapphire Blue", "Rainbow Wave", "PCB Temperature", "Fan Speed", "Static", "Off"])

    def test_mixed_mode_group(self):
        self.rgb.modes[1] = "Spectrum Cycle"
        self.assertIsNone(H.lights()["groups"]["ram"]["mode"])

    def test_status_never_runs_openrgb(self):
        H.status(CFG)
        self.assertEqual(self.rgb.calls, [])
        H.lights()
        self.assertEqual(len(self.rgb.calls), 1)
        H.lights(), H.status(CFG)
        self.assertEqual(len(self.rgb.calls), 1, "detection is cached")
        H.lights(refresh=True)
        self.assertEqual(len(self.rgb.calls), 2)

    def test_no_openrgb(self):
        H.OPENRGB.unlink()
        self.assertEqual(H.lights(), {"available": False, "groups": {}})

    @data(("ram", "static", "00aaff", True, ["--device", "0", "--mode", "Static", "--color", "00AAFF",
                                            "--device", "1", "--mode", "Static", "--color", "00AAFF"]),
          ("gpu", "Off", None, True, ["--device", "2", "--mode", "Off"]),
          ("board", "Breathing", "FF0000", None, ["--device", "3", "--mode", "Breathing", "--color", "FF0000"]),
          ("ram", "spectrum cycle", None, True, ["--device", "0", "--mode", "Spectrum Cycle", "--device", "1", "--mode", "Spectrum Cycle"]))
    @unpack
    def test_set_light(self, group, mode, color, verified, args):
        r = H.set_light(group, mode, color)
        self.assertEqual(r["verified"], verified)
        self.assertEqual(self.rgb.calls[1], args)   # calls[0] is the first detection
        self.assertEqual(r["lights"]["groups"][group]["wanted"]["mode"].lower(), mode.lower())

    def test_every_change_reapplies_all_wanted(self):
        H.set_light("gpu", "Static", "FF0000")
        H.set_light("ram", "Off")
        self.assertEqual(self.rgb.calls[-2], ["--device", "0", "--mode", "Off", "--device", "1", "--mode", "Off",
                                              "--device", "2", "--mode", "Static", "--color", "FF0000"])
        self.assertEqual(self.rgb.calls[-1], ["--list-detailed"], "read back after applying")

    @data(("fans", "Static", None, "no fans lighting"), ("ram", "Direct", None, "ram modes"),
          ("gpu", "Rainbow", None, "gpu modes"), ("ram", "Static", "red", "6 hex"), ("ram", "Static", "FF00001", "6 hex"),
          ("ram", "Static; reboot", None, "ram modes"))
    @unpack
    def test_set_light_rejects(self, group, mode, color, msg):
        with self.assertRaisesRegex(ValueError, msg):
            H.set_light(group, mode, color)
        self.assertFalse(any("--mode" in c for c in self.rgb.calls), "nothing applied")
        self.assertEqual(H.load_lights_wanted(), {})

    def test_not_read_back_is_an_error(self):
        self.rgb.stuck = "Breathing"
        with self.assertRaisesRegex(ValueError, "did not read back"):
            H.set_light("ram", "Breathing")

    def test_lights_apply_at_boot(self):
        H.set_light("ram", "Static", "112233")
        self.rgb.modes.update({0: "Rainbow", 1: "Rainbow"})   # power cycle: the RAM went back to its own mode
        stale = json.loads((H.RUN / "lights.json").read_text())   # a device list from before: indices may have moved
        for d in stale["devices"]:
            d["index"] += 7
        (H.RUN / "lights.json").write_text(json.dumps(stale))
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(H.main(["lights-apply"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"verified": {"ram": True}})
        self.assertEqual(self.rgb.calls[-3], ["--list-detailed"], "detects again before applying")
        self.assertEqual(self.rgb.calls[-2][:2], ["--device", "0"], "applies to the devices as detected now")
        self.assertEqual(self.rgb.modes[0], "Static")

    @data(("lights", 0), ("lights refresh", 0), ("light gpu Off", 0), ("light ram Static 00FF00", 0),
          ("light ram Turbo", 1), ("light ram", 2), ("lights now", 2), ("noc-hw light gpu 'PCB Temperature'", 0),
          ("lights-apply", 0), ("light gpu Off; reboot", 1))
    @unpack
    def test_forced_command(self, original, code):
        import contextlib
        import io
        os.environ["SSH_ORIGINAL_COMMAND"] = original
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(H.main(["ssh"]), code)
        finally:
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

    def setUp(self):
        self.saved = self.run_hw("status")["wanted"]   # the owner's settings: put back after each test

    def tearDown(self):
        now = self.run_hw("status")["wanted"]
        for target in sorted(set(now) | set(self.saved) | {"board", "gpu"}):
            w = self.saved.get(target) or ({"mode": "group"} if target.startswith("fan") else {"mode": "auto"})
            if now.get(target) != self.saved.get(target):
                self.run_hw(f"set {target} {w['mode']}" + (f" {w['pct']}" if w["mode"] == "manual" else ""))
        self.assertEqual(self.run_hw("status")["wanted"], self.saved)

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

    def test_one_fan(self):
        s = self.run_hw("status")
        board = [f for f in s["fans"] if f["target"] == "board"]
        slow = [f for f in board if f["pct"] < 80]   # one the firmware already runs near 100 % can't speed up
        self.assertTrue(slow, board)
        one, rest = slow[0], [f for f in board if f is not slow[0]]
        try:
            self.run_hw("set board auto")
            self.run_hw(f"set {one['id']} full")
            s = self.wait_for(lambda s: not next(f for f in s["fans"] if f.get("id") == one["id"])["auto"], "fan did not go manual")
            self.assertTrue(all(f["auto"] for f in s["fans"] if f["target"] == "board" and f["id"] != one["id"]),
                            "the other headers stay with the firmware")
            time.sleep(4)
            now = next(f for f in self.run_hw("status")["fans"] if f.get("id") == one["id"])
            self.assertGreater(now["rpm"], one["rpm"] * 1.1, f"{one['name']} did not speed up: {one['rpm']} -> {now['rpm']}")
        finally:
            self.run_hw(f"set {one['id']} group")
        self.assertNotIn(one["id"], self.run_hw("status")["wanted"])
        self.wait_for(lambda s: all(f["auto"] for f in s["fans"] if f["target"] == "board"), "fans did not return to auto")
        self.assertTrue(rest)

    @unittest.skipUnless(os.environ.get("PVE_LIGHTS"), "set PVE_LIGHTS=1")
    def test_lights(self):
        g = self.run_hw("lights refresh")["groups"]
        self.assertTrue({"ram", "gpu"} <= set(g), g)
        before = {k: g[k]["mode"] for k in ("ram", "gpu")}
        try:
            for group, color in (("ram", "00AAFF"), ("gpu", "FF4000")):
                r = self.run_hw(f"light {group} Static {color}")
                self.assertIs(r["verified"], True, r)
                self.assertEqual(r["lights"]["groups"][group]["mode"], "Static")
        finally:
            for group, mode in before.items():
                self.run_hw(f"light {group} {shlex.quote(mode)}")
        g = self.run_hw("lights")["groups"]
        self.assertEqual({k: g[k]["mode"] for k in before}, before)


if __name__ == "__main__":
    unittest.main()
