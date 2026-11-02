"""Data-driven tests of the Proxmox cluster view and its alert checks (app/proxmox.py).

Unit tests use made-up node names and numbers.  Run:  python -m unittest tests/test_proxmox.py

The integration test runs the real collector on the NOC against the live Proxmox API and is skipped unless
NOC_HOST is set (SSH as root with a key), e.g.  NOC_HOST=192.0.2.20 python -m unittest tests/test_proxmox.py
It only reads (the NOC's read-only Proxmox token); it changes nothing.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import proxmox as P  # noqa: E402

GB = 2**30


def status(cpu=0.05, used=4, total=16, up=3600):
    return {"cpu": cpu, "memory": {"used": used * GB, "total": total * GB}, "uptime": up, "pveversion": "pve-manager/9.2.2/x",
            "cpuinfo": {"model": "Example CPU"}}


def member(name, ip, online=1):
    return {"type": "node", "name": name, "ip": ip, "online": online}


CLUSTER = {"type": "cluster", "name": "home", "quorate": 1, "nodes": 2}
STORE = [{"storage": "local", "type": "dir", "used": 25e9, "total": 100e9, "active": 1},
         {"storage": "nas", "type": "nfs", "used": 0, "total": 0, "active": 0}]
GUESTS = [{"vmid": 103, "name": "ha", "type": "qemu", "status": "running", "node": "node-b", "cpu": 0.02, "mem": 1 * GB, "maxmem": 2 * GB},
          {"vmid": 100, "name": "dns", "type": "qemu", "status": "running", "node": "node-a", "cpu": 0.01, "mem": 2 * GB, "maxmem": 8 * GB},
          {"vmid": 101, "name": "ctl", "type": "lxc", "status": "stopped", "node": "node-b", "mem": 0, "maxmem": 3 * GB,
           "disk": 4e9, "maxdisk": 8e9}]


@ddt
class View(unittest.TestCase):
    def test_single_host(self):
        """No cluster: just the configured node, and the old top-level fields unchanged."""
        v = P.view("node-a", status(), GUESTS[1:2], STORE)
        self.assertIsNone(v["cluster"])
        self.assertEqual([(n["name"], n["online"], n["local"]) for n in v["nodes"]], [("node-a", True, True)])
        self.assertEqual((v["node"], v["cpu_pct"], v["mem_pct"], v["uptime"]), ("node-a", 5.0, 25.0, 3600))
        self.assertEqual(v["storage"], [{"name": "local", "type": "dir", "used_pct": 25.0, "total_gb": 100}],
                         "inactive storage is left out")

    def test_two_nodes(self):
        v = P.view("node-a", status(), GUESTS, STORE, [CLUSTER, member("node-b", "192.0.2.21"), member("node-a", "192.0.2.196")],
                   {"node-b": status(cpu=0.5, used=6, total=16)}, {"node-b": STORE[:1]})
        self.assertEqual([n["name"] for n in v["nodes"]], ["node-a", "node-b"], "configured node first")
        b = v["nodes"][1]
        self.assertEqual((b["ip"], b["online"], b["local"], b["cpu_pct"], b["mem_pct"], b["mem_gb"]),
                         ("192.0.2.21", True, False, 50.0, 37.5, 16.0))
        self.assertEqual((b["guests_running"], b["guests"]), (1, 2))
        self.assertEqual(v["cluster"], {"name": "home", "quorate": True, "nodes": 2, "online": 2})
        self.assertEqual([g["vmid"] for g in v["guests"]], [100, 101, 103], "guests sorted by ID")
        self.assertEqual({g["vmid"]: g["node"] for g in v["guests"]}, {100: "node-a", 101: "node-b", 103: "node-b"})

    @data((None, None, "offline in the cluster: no metrics"),
          (0, None, "offline"))
    @unpack
    def test_offline_node(self, online, _, why):
        v = P.view("node-a", status(), GUESTS, STORE, [CLUSTER, member("node-a", "192.0.2.196"), member("node-b", "192.0.2.21", online)])
        b = v["nodes"][1]
        self.assertEqual((b["online"], b["cpu_pct"], b["mem_pct"], b["uptime"], b["storage"]), (False, None, None, None, []), why)
        self.assertEqual(v["cluster"]["online"], 1)

    def test_online_but_unreadable(self):
        """The cluster says online but its status could not be read: still online, metrics unknown."""
        v = P.view("node-a", status(), GUESTS, STORE, [CLUSTER, member("node-a", "192.0.2.196"), member("node-b", "192.0.2.21")])
        self.assertEqual((v["nodes"][1]["online"], v["nodes"][1]["cpu_pct"]), (True, None))

    @data(({"vmid": 1, "name": "x", "type": "qemu", "status": "running"}, None, None),
          ({"vmid": 2, "type": "lxc", "status": "running", "mem": GB, "maxmem": 4 * GB, "disk": 2e9, "maxdisk": 8e9}, 25.0, 25.0),
          ({"vmid": 3, "type": "qemu", "status": "running", "mem": GB, "maxmem": 2 * GB, "disk": 1, "maxdisk": 2}, 50.0, None))
    @unpack
    def test_guest_row(self, g, mem, disk):
        r = P.guest_row(g)
        self.assertEqual((r["mem_pct"], r["disk_pct"]), (mem, disk), "VM disk use is not known to Proxmox; missing maxmem is None")


@ddt
class Checks(unittest.TestCase):
    def view(self, b_online=1, quorate=1):
        return P.view("node-a", status(), GUESTS, STORE,
                      [dict(CLUSTER, quorate=quorate), member("node-a", "192.0.2.196"), member("node-b", "192.0.2.21", b_online)],
                      {"node-b": status()} if b_online else {}, {})

    @data((1, 1, {"pve:node-a": True, "pve:node-b": True, "pve:quorum": True}),
          (0, 1, {"pve:node-a": True, "pve:node-b": False, "pve:quorum": True}),
          (0, 0, {"pve:node-a": True, "pve:node-b": False, "pve:quorum": False}))
    @unpack
    def test_checks(self, b_online, quorate, want):
        c = P.checks(self.view(b_online, quorate))
        self.assertEqual({k: ok for k, (ok, _) in c.items()}, want)

    def test_texts(self):
        c = P.checks(self.view(0, 1))
        self.assertEqual(c["pve:node-b"][1], "Proxmox node node-b (192.0.2.21): offline")
        self.assertEqual(c["pve:quorum"][1], "Proxmox cluster home: quorate, 1/2 nodes online")
        self.assertIn("lost quorum", P.checks(self.view(0, 0))["pve:quorum"][1])

    @data(None, {"error": "x"}, {"nodes": []}, {"cluster": None, "nodes": [{"name": "a", "online": True}]})
    def test_no_cluster_no_checks(self, px):
        self.assertEqual(P.checks(px), {}, "a single host is already watched by its ping check")


@unittest.skipUnless(os.environ.get("NOC_HOST"), "set NOC_HOST")
class Live(unittest.TestCase):
    """The deployed collector against the live cluster: every node Proxmox lists, with its guests."""
    PROBE = r'''
import json, os, sys
os.environ["NOC_NO_WORKERS"] = "1"
sys.path.insert(0, "/opt/noc")
import app as noc, proxmox
px = noc.collect_proxmox()
real = {g["vmid"]: g["node"] for g in noc.pve("/cluster/resources?type=vm")}
print(json.dumps({"px": px, "real": real, "checks": {k: v[0] for k, v in proxmox.checks(px).items()}}))
'''

    def test_live(self):
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", f"root@{os.environ['NOC_HOST']}", "python3", "-"],
                           input=self.PROBE, capture_output=True, text=True, timeout=120, check=True)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        px, real = got["px"], {int(k): v for k, v in got["real"].items()}
        self.assertTrue(px["nodes"] and px["nodes"][0]["local"], "the configured node is listed first")
        self.assertEqual({g["vmid"]: g["node"] for g in px["guests"]}, real, "every guest on its real node")
        for n in px["nodes"]:
            if n["online"]:
                self.assertIsNotNone(n["cpu_pct"], f"{n['name']} is online but has no metrics")
        if px["cluster"]:
            self.assertEqual(set(got["checks"]), {f"pve:{n['name']}" for n in px["nodes"]} | {"pve:quorum"})


if __name__ == "__main__":
    unittest.main()
