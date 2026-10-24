"""Data-driven tests of tracking and removing ISP boxes through the real API.

ISP boxes are tracked devices in the "isp" group; WAN groups name one as the
"ISP box" step of their diagnosis path (router.wan_paths.<group>.isp_device).
Removing a device must also clear those references, and the preview must say so.

Offline: the app runs on the example config with dummy secrets in a temporary
directory, like smoke_test.py.  Run:  python -m unittest tests/test_isp_boxes.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

ROOT = Path(__file__).resolve().parents[1]
ETC = Path(tempfile.mkdtemp())
VAR = Path(tempfile.mkdtemp())
EXAMPLE = json.loads((ROOT / "config" / "config.example.json").read_text())
EXAMPLE["devices"].append({"name": "Second ONT", "target": "192.0.2.3", "group": "isp"})
PATHS = {"isp-a": {"switch_port": 2, "isp_device": "ONT (web port)", "ont": None},
         "isp-b": {"switch_port": 3, "isp_device": "Second ONT", "ont": None},
         "isp-c": {"switch_port": 4, "isp_device": "ONT (web port)", "ont": None}}
EXAMPLE["router"]["wan_paths"] = PATHS
(ETC / "config.json").write_text(json.dumps(EXAMPLE))
(ETC / "secrets.env").write_text(
    "PIHOLE_A_PASS=x\nPIHOLE_B_PASS=x\nOMADA_CLIENT_ID=x\nOMADA_CLIENT_SECRET=x\nPVE_TOKEN=x\n"
    "NOC_ADMIN_USER=admin\nNOC_ADMIN_PASS=test-pass\nNOC_SESSION_KEY=" + "cd" * 32 + "\n")
os.environ["NOC_ETC"], os.environ["NOC_VAR"] = str(ETC), str(VAR)
os.environ["NOC_NO_WORKERS"] = "1"
sys.path.insert(0, str(ROOT / "app"))

from fastapi.testclient import TestClient  # noqa: E402
import app as noc  # noqa: E402

C = TestClient(noc.app)
assert C.post("/login", data={"user": "admin", "password": "test-pass"}, follow_redirects=False).status_code == 303


def reset():
    """Put devices and WAN paths back to the starting state."""
    noc.C["devices"] = json.loads(json.dumps(EXAMPLE["devices"]))
    noc.ROUTER["wan_paths"] = json.loads(json.dumps(PATHS))


def preview(kind, params):
    r = C.post("/api/actions/preview", json={"kind": kind, "params": params})
    return r.status_code, r.json()


def confirm(p):
    return C.post("/api/actions/confirm", json={"id": p["id"]}).json()


def saved():
    return json.loads((ETC / "config.json").read_text())


def tearDownModule():
    shutil.rmtree(ETC, ignore_errors=True)
    shutil.rmtree(VAR, ignore_errors=True)


@ddt
class Remove(unittest.TestCase):
    def setUp(self):
        reset()

    @data(
        # (device, groups that lose their ISP box)
        ("ONT (web port)", ["isp-a", "isp-c"]),
        ("Second ONT", ["isp-b"]),
        ("Router", []),                       # not an ISP box of any group
    )
    @unpack
    def test_preview_names_affected_groups(self, name, groups):
        code, p = preview("device_delete", {"original_name": name})
        self.assertEqual(code, 200, p)
        self.assertEqual(p["summary"], f"Stop tracking {name}")
        note = [d for d in p["detail"] if d.startswith("WAN diagnosis no longer checks it")]
        if groups:
            self.assertEqual(note, [f"WAN diagnosis no longer checks it for: {', '.join(groups)} (ISP box cleared)"])
        else:
            self.assertEqual(note, [])

    @data(("ONT (web port)", {"isp-a": None, "isp-b": "Second ONT", "isp-c": None}),
          ("Second ONT", {"isp-a": "ONT (web port)", "isp-b": None, "isp-c": "ONT (web port)"}),
          ("Router", {"isp-a": "ONT (web port)", "isp-b": "Second ONT", "isp-c": "ONT (web port)"}))
    @unpack
    def test_confirm_clears_only_its_references(self, name, want):
        _, p = preview("device_delete", {"original_name": name})
        self.assertTrue(confirm(p)["ok"])
        cfg = saved()
        self.assertNotIn(name, [d["name"] for d in cfg["devices"]])
        self.assertEqual({g: v["isp_device"] for g, v in cfg["router"]["wan_paths"].items()}, want)
        # the rest of each path is kept
        self.assertEqual([v["switch_port"] for v in cfg["router"]["wan_paths"].values()], [2, 3, 4])

    def test_preview_changes_nothing(self):
        before = (ETC / "config.json").read_text()
        preview("device_delete", {"original_name": "ONT (web port)"})
        self.assertEqual((ETC / "config.json").read_text(), before)

    @data("Nope", "", None)
    def test_unknown_device_refused(self, name):
        code, _ = preview("device_delete", {"original_name": name})
        self.assertEqual(code, 400)


@ddt
class AddAndRename(unittest.TestCase):
    def setUp(self):
        reset()

    @data(
        ({"name": "Cable modem", "group": "isp", "mode": "ip", "ip": "192.0.2.60"}, "192.0.2.60"),
        ({"name": "ONT 3", "group": "isp", "mode": "ip", "ip": "192.0.2.61", "port": 80}, "192.0.2.61:80"),
        ({"name": "Stick", "group": "isp", "mode": "mac", "mac": "00:00:5E:00:53:2A"}, None),
    )
    @unpack
    def test_add_isp_box(self, params, target):
        code, p = preview("device_add", params)
        self.assertEqual(code, 200, p)
        self.assertTrue(confirm(p)["ok"])
        dev = next(d for d in saved()["devices"] if d["name"] == params["name"])
        self.assertEqual(dev["group"], "isp")
        if target:
            self.assertEqual(dev["target"], target)
        else:
            self.assertEqual(dev["mac"], "00:00:5e:00:53:2a")
        # listed for the WAN groups' ISP box choice
        self.assertIn(params["name"], C.get("/api/settings").json()["wan"]["devices"])

    def test_rename_keeps_references(self):
        _, p = preview("device_edit", {"original_name": "Second ONT", "name": "ONT B", "group": "isp", "mode": "ip", "ip": "192.0.2.3"})
        self.assertTrue(confirm(p)["ok"])
        self.assertEqual(saved()["router"]["wan_paths"]["isp-b"]["isp_device"], "ONT B")

    @data(({"name": "x", "group": "nope", "mode": "ip", "ip": "192.0.2.9"},),
          ({"name": "Router", "group": "isp", "mode": "ip", "ip": "192.0.2.9"},),       # name taken
          ({"name": "y", "group": "isp", "mode": "ip", "ip": "not-an-ip"},))
    @unpack
    def test_invalid_refused(self, params):
        self.assertEqual(preview("device_add", params)[0], 400)


if __name__ == "__main__":
    unittest.main()
