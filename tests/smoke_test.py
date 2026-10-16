"""Offline smoke test: boots the app with the example config and dummy secrets.

No network access is needed: it only exercises auth, metadata and an action preview.
Run:  python tests/smoke_test.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
etc = Path(tempfile.mkdtemp())
var = Path(tempfile.mkdtemp())
shutil.copy(ROOT / "config" / "config.example.json", etc / "config.json")
(etc / "secrets.env").write_text(
    "PIHOLE_A_PASS=x\nPIHOLE_B_PASS=x\nOMADA_CLIENT_ID=x\nOMADA_CLIENT_SECRET=x\nPVE_TOKEN=x\n"
    "NOC_ADMIN_USER=admin\nNOC_ADMIN_PASS=test-pass\nNOC_SESSION_KEY=" + "ab" * 32 + "\n")
os.environ["NOC_ETC"], os.environ["NOC_VAR"] = str(etc), str(var)
sys.path.insert(0, str(ROOT / "app"))

from fastapi.testclient import TestClient  # noqa: E402
import app as noc  # noqa: E402

c = TestClient(noc.app)

assert c.get("/healthz").json() == {"ok": True}
assert c.get("/api/overview").status_code == 401, "API must require login"
assert c.get("/", follow_redirects=False).status_code in (302, 303, 307), "UI must redirect to login"

r = c.post("/login", data={"user": "admin", "password": "wrong"}, follow_redirects=False)
assert "bad=1" in r.headers["location"], "wrong password must be rejected"
r = c.post("/login", data={"user": "admin", "password": "test-pass"}, follow_redirects=False)
assert r.status_code == 303 and "noc" in r.cookies, "login must set a session cookie"

meta = c.get("/api/meta").json()
assert meta["site_name"] == "Home NOC" and meta["dns_vip"] == "192.0.2.10", meta
assert meta["new_network"]["subnet_template"] == "10.0.{n}.1/24", meta

# Preview only: builds the plan without touching any Pi-hole.
p = c.post("/api/actions/preview", json={"kind": "dns_blocking", "params": {"enable": False, "minutes": 5}}).json()
assert p["summary"].startswith("Pause ad-blocking for 5 min"), p
assert len(p["detail"]) == 2 and p["confirm_word"] is None, p
bad = c.post("/api/actions/preview", json={"kind": "wan_restart", "params": {"iface": "lan"}})
assert bad.status_code == 400, "only configured WAN interfaces may be restarted"
assert c.post("/api/actions/confirm", json={"id": "nope"}).status_code == 400, "unknown preview id must fail"

assert noc.PROTECTED_PORTS == {1: "Uplink to router"} and noc.SWITCH_PORTS == 10
assert noc.VIP_MACS["00:00:5e:00:53:01"] == "Pi-hole A"
print("smoke test: all checks passed")
