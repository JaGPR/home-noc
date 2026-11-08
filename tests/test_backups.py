"""Data-driven tests of the backup watcher in a cluster (app/backups.py): tasks and logs from every node,
and the per-guest "has a recent good backup" check.

Unit tests use a fake Proxmox API with made-up nodes and guests.  Run:  python -m unittest tests/test_backups.py
(The single-node behaviour, messages and missed/stuck backups are covered by tests/smoke_test.py.)

The integration test runs the real watcher on the NOC against the live cluster and is skipped unless NOC_HOST
is set (SSH as root with a key), e.g.  NOC_HOST=192.0.2.20 python -m unittest tests/test_backups.py
It only reads (the NOC's read-only Proxmox token) and sends nothing.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from ddt import data, ddt, unpack

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import backups as B  # noqa: E402

NOW = 1_800_000_000.0
H = 3600


def log(*guests):
    """A vzdump log with these (vmid, kind, name, ok) results."""
    lines = []
    for vmid, kind, name, ok in guests:
        lines += [f"INFO: Starting Backup of VM {vmid} ({kind})", f"INFO: {'VM' if kind == 'qemu' else 'CT'} Name: {name}"]
        lines += [f"INFO: archive file size: 1.00GB", f"INFO: Finished Backup of VM {vmid} (00:00:30)"] if ok else \
                 [f"ERROR: Backup of VM {vmid} failed - no space left"]
    return lines


def task(node, n, end, status="OK"):
    return {"upid": f"UPID:{node}:{n:08X}:0:{int(end):08X}:vzdump::root@pam:", "starttime": end - 120, "endtime": end,
            "status": status, "node": node}


class FakePve:
    """Two nodes: main (guests 100, 102) and laptop (101, 103). tasks/logs per node; `down` nodes fail."""

    def __init__(self):
        self.status = {"main": "online", "laptop": "online"}
        self.tasks = {"main": [], "laptop": []}
        self.logs, self.down, self.calls, self.cluster_ok = {}, set(), [], True
        self.jobs = [{"id": "nightly", "type": "vzdump", "enabled": 1, "schedule": "02:00", "storage": "NAS",
                      "vmid": "100,101,102,103"}]
        self.guests = [{"vmid": 100, "name": "dns", "type": "qemu", "node": "main"}, {"vmid": 102, "name": "vm", "type": "qemu", "node": "main"},
                       {"vmid": 101, "name": "ctl", "type": "lxc", "node": "laptop"}, {"vmid": 103, "name": "ha", "type": "qemu", "node": "laptop"}]

    def __call__(self, path):
        self.calls.append(path)
        if path == "/nodes":
            if not self.cluster_ok:
                raise RuntimeError("403")
            return [{"node": n, "status": s} for n, s in self.status.items()]
        if path == "/cluster/resources?type=vm":
            if not self.cluster_ok:
                raise RuntimeError("403")
            return self.guests
        if path == "/cluster/backup":
            return self.jobs
        node = path.split("/")[2]
        if node in self.down:
            raise RuntimeError(f"{node} unreachable")
        if "/tasks?" in path:
            return self.tasks[node]
        if "/log?" in path:
            upid = urllib.parse.unquote(path.split("/tasks/")[1].split("/log")[0])
            assert upid.split(":")[1] == node, f"log of {upid} asked from {node}"
            return [{"n": i, "t": t} for i, t in enumerate(self.logs[upid])]
        if path.endswith("/qemu"):
            return [{"vmid": g["vmid"], "name": g["name"]} for g in self.guests if g["type"] == "qemu" and g["node"] == node]
        if path.endswith("/lxc"):
            return [{"vmid": g["vmid"], "name": g["name"]} for g in self.guests if g["type"] == "lxc" and g["node"] == node]
        raise AssertionError(path)


class FakeNoc:
    def __init__(self, pve):
        self.pve, self.PVE, self.VAR = pve, {"node": "main"}, Path(tempfile.mkdtemp())


def watcher():
    pve = FakePve()
    return B.Watcher(FakeNoc(pve)), pve


def nightly(pve, when, main=True, laptop=True, fail=()):
    """The 02:00 job ran on each node (main: 100 102, laptop: 101 103) `when`."""
    for node, ids, on in (("main", ((100, "qemu", "dns"), (102, "qemu", "vm")), main),
                          ("laptop", ((101, "lxc", "ctl"), (103, "qemu", "ha")), laptop)):
        if on:
            t = task(node, len(pve.logs) + 1, when)
            pve.tasks[node].insert(0, t)
            pve.logs[t["upid"]] = log(*[(v, k, n, v not in fail) for v, k, n in ids])


@ddt
class Coverage(unittest.TestCase):
    @data(
        # (runs: list of (hours ago, main ran, laptop ran, failed ids), -> guests that are not ok)
        ([(2, True, True, ())], set()),                         # last night, both nodes: all good
        ([(26, True, True, ()), (2, True, False, ())], set()),  # laptop missed last night, but 26 h is within 36 h
        ([(50, True, True, ()), (26, True, False, ()), (2, True, False, ())], {"101", "103"}),  # laptop skipped twice
        ([(2, True, True, (103,))], {"103"}),                   # one guest failed, never good
        ([(30, True, True, ()), (2, True, True, (103,))], set()),  # failed last night, the previous one is recent
        ([], {"100", "101", "102", "103"}),                     # no backups at all
    )
    @unpack
    def test_coverage(self, runs, bad):
        w, pve = watcher()
        for hours, main, laptop, fail in runs:
            nightly(pve, NOW - hours * H, main, laptop, fail)
        got = w.coverage_readings({"backup_max_age_h": 36}, now=NOW)
        self.assertEqual(set(got), {"backup:guest:100", "backup:guest:101", "backup:guest:102", "backup:guest:103"})
        self.assertEqual({k.split(":")[-1] for k, (ok, _) in got.items() if not ok}, bad)

    def test_texts(self):
        w, pve = watcher()
        nightly(pve, NOW - 40 * H, laptop=False)
        nightly(pve, NOW - 2 * H, laptop=False)
        got = w.coverage_readings({"backup_max_age_h": 36}, now=NOW)
        self.assertEqual(got["backup:guest:100"], (True, "Backup of 100 dns: last good backup 2h ago"))
        self.assertEqual(got["backup:guest:103"], (False, "Backup of 103 ha: no good backup found (expected within 36 h)"))
        pve.tasks["laptop"].append(task("laptop", 99, NOW - 40 * H))
        pve.logs[pve.tasks["laptop"][-1]["upid"]] = log((103, "qemu", "ha", True))
        w.parsed.clear()
        self.assertEqual(w.coverage_readings({"backup_max_age_h": 36}, now=NOW)["backup:guest:103"],
                         (False, "Backup of 103 ha: last good backup 40h ago (expected within 36 h)"))

    @data(({"enabled": 0}, set()), ({"vmid": "100,999"}, {"backup:guest:100"}), ({"vmid": None, "all": 1, "exclude": "101,103"},
          {"backup:guest:100", "backup:guest:102"}))
    @unpack
    def test_job_scope(self, change, keys):
        """Disabled jobs don't count; a guest that no longer exists is skipped; all + exclude."""
        w, pve = watcher()
        pve.jobs[0].update(change)
        nightly(pve, NOW - 2 * H)
        self.assertEqual(set(w.coverage_readings({}, now=NOW)), keys)


@ddt
class Cluster(unittest.TestCase):
    def test_tasks_from_every_node_and_logs_from_their_own(self):
        w, pve = watcher()
        nightly(pve, NOW - 2 * H)
        self.assertEqual({t["node"] for t in w.tasks()}, {"main", "laptop"})
        self.assertEqual(set(w.latest()), {"100", "101", "102", "103"})   # FakePve asserts each log came from its node

    @data(("laptop", True), ("main", False))
    @unpack
    def test_unreachable_node(self, node, works):
        """Another node that doesn't answer is skipped; the configured node must answer."""
        w, pve = watcher()
        nightly(pve, NOW - 2 * H)
        pve.down.add(node)
        if works:
            self.assertEqual({t["node"] for t in w.tasks()}, {"main"})
        else:
            with self.assertRaisesRegex(RuntimeError, "main unreachable"):
                w.tasks()

    @data("unknown", "offline")
    def test_configured_node_always_asked(self, status):
        """Proxmox calls a node "unknown" while its pvestatd is down; its backups still count."""
        w, pve = watcher()
        nightly(pve, NOW - 2 * H)
        pve.status["main"] = status
        self.assertEqual(w.nodes(), ["main", "laptop"])
        self.assertEqual(set(w.coverage_readings({"backup_max_age_h": 36}, now=NOW)),
                         {"backup:guest:100", "backup:guest:101", "backup:guest:102", "backup:guest:103"})
        self.assertTrue(all(ok for ok, _ in w.coverage_readings({"backup_max_age_h": 36}, now=NOW).values()))

    @data(
        # (node, what happens to it between two polls): no messages on the poll after it is back
        ("laptop", "offline"),   # laptop not read for a while: its old runs are not sent again
        ("laptop", "down"),      # laptop online but not answering
        ("main", "unknown"),     # the configured node with its pvestatd down
    )
    @unpack
    def test_node_back_does_not_resend_old_runs(self, node, how):
        w, pve = watcher()
        cfg = {"backup_long_h": 6, "backup_success": True}
        for h in (50, 26, 2):
            nightly(pve, B.time.time() - h * H)
        self.assertEqual(len(w.poll(cfg, "Home")), 2)       # first poll: the 2 h old runs once, older ones not
        if how == "down":
            pve.down.add(node)
        else:
            pve.status[node] = how
        w.last_poll = 0
        w.poll(cfg, "Home")
        pve.down.clear()
        pve.status[node] = "online"
        w.last_poll = 0
        self.assertEqual(w.poll(cfg, "Home"), [])

    def test_new_run_on_a_node_that_was_away_is_sent(self):
        w, pve = watcher()
        cfg = {"backup_long_h": 6, "backup_success": True}
        nightly(pve, B.time.time() - 26 * H)
        w.poll(cfg, "Home")
        pve.status["laptop"] = "offline"
        w.last_poll = 0
        w.poll(cfg, "Home")
        pve.status["laptop"] = "online"
        nightly(pve, B.time.time() - 60, main=False)
        w.last_poll = 0
        got = w.poll(cfg, "Home")
        self.assertEqual(len(got), 1)
        self.assertIn("101 ctl", got[0])

    def test_offline_node_not_asked(self):
        w, pve = watcher()
        pve.status["laptop"] = "offline"
        w.tasks()
        self.assertFalse([c for c in pve.calls if c.startswith("/nodes/laptop/")])

    def test_without_cluster_access(self):
        """A token that can't read /nodes or /cluster/resources: the configured node only, as before."""
        w, pve = watcher()
        pve.cluster_ok = False
        nightly(pve, NOW - 2 * H)
        self.assertEqual(w.nodes(), ["main"])
        self.assertEqual(w.guest_names(), {"100": "dns", "102": "vm"})

    @data(("UPID:laptop:0001:0:0:vzdump::root@pam:", "laptop"), ("UPID:main:x:y", "main"), ("garbage", "main"), ("UPID::a:b", "main"))
    @unpack
    def test_task_node(self, upid, node):
        self.assertEqual(watcher()[0].task_node(upid), node)

    def test_overview_lists_every_node(self):
        w, pve = watcher()
        nightly(pve, NOW - 2 * H, laptop=False)
        out = w.overview("Home")
        self.assertIn("103 ha · no recent backup", out)
        self.assertIn("100 dns ·", out)


@unittest.skipUnless(os.environ.get("NOC_HOST"), "set NOC_HOST")
class Live(unittest.TestCase):
    """The deployed watcher against the live cluster: every guest of an enabled job is checked, and the
    guests that ran on each node are found in that node's tasks."""
    PROBE = r'''
import json, os, sys, io, contextlib
os.environ["NOC_NO_WORKERS"] = "1"
sys.path.insert(0, "/opt/noc")
with contextlib.redirect_stdout(io.StringIO()):
    import app as noc, notify
w = noc.TG.backups
cov = w.coverage_readings({**notify.DEFAULTS, **(noc.C.get("telegram") or {})})
nodes = {t["upid"].split(":")[1] for t in w.tasks()}
res = {str(g["vmid"]): g["node"] for g in noc.pve("/cluster/resources?type=vm")}
print(json.dumps({"coverage": cov, "task_nodes": sorted(nodes), "guests": res, "online": w.nodes()}))
'''

    def test_live(self):
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", f"root@{os.environ['NOC_HOST']}", "python3", "-"],
                           input=self.PROBE, capture_output=True, text=True, timeout=180, check=True)
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(got["coverage"], "no guest checked")
        for k, (ok, text) in got["coverage"].items():
            self.assertTrue(ok, f"{k}: {text}")
        self.assertTrue(set(got["online"]) <= set(got["task_nodes"]) | set(got["online"]))
        laptop_guests = {v for v, n in got["guests"].items() if n != got["online"][0]}
        if laptop_guests:
            self.assertGreater(len(got["task_nodes"]), 1, "the other node's backup tasks are read too")


if __name__ == "__main__":
    unittest.main()
