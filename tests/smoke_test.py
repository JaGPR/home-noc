"""Offline smoke test: boots the app with the example config and dummy secrets.

No network access is needed: it only exercises auth, metadata and an action preview.
Run:  python tests/smoke_test.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
etc = Path(tempfile.mkdtemp())
var = Path(tempfile.mkdtemp())
shutil.copy(ROOT / "config" / "config.example.json", etc / "config.json")
(etc / "secrets.env").write_text(
    "PIHOLE_A_PASS=x\nPIHOLE_B_PASS=x\nOMADA_CLIENT_ID=x\nOMADA_CLIENT_SECRET=x\nPVE_TOKEN=x\n"
    "NOC_ADMIN_USER=admin\nNOC_ADMIN_PASS=test-pass\nNOC_SESSION_KEY=" + "ab" * 32 + "\n")
os.environ["NOC_ETC"], os.environ["NOC_VAR"] = str(etc), str(var)
os.environ["NOC_NO_WORKERS"] = "1"
sys.path.insert(0, str(ROOT / "app"))

from fastapi.testclient import TestClient  # noqa: E402
import app as noc  # noqa: E402
import notify  # noqa: E402

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
assert noc.PROTECTED_PORTS == {1: "Uplink to router"} and noc.SWITCH_PORTS == 10
assert noc.VIP_MACS["00:00:5e:00:53:01"] == "Pi-hole A"

# Preview only: builds the plan without touching any Pi-hole.
p = c.post("/api/actions/preview", json={"kind": "dns_blocking", "params": {"enable": False, "minutes": 5}}).json()
assert p["summary"].startswith("Pause ad-blocking for 5 min"), p
assert len(p["detail"]) == 2 and p["confirm_word"] is None, p
bad = c.post("/api/actions/preview", json={"kind": "wan_restart", "params": {"iface": "lan"}})
assert bad.status_code == 400, "only configured WAN interfaces may be restarted"
assert c.post("/api/actions/confirm", json={"id": "nope"}).status_code == 400, "unknown preview id must fail"

# Devices tab: previews validate input and never touch config.json until confirmed.
before = (etc / "config.json").read_text()
p = c.post("/api/actions/preview", json={"kind": "device_add", "params": {
    "name": "Printer", "group": "client", "mode": "ip", "ip": "192.0.2.50", "port": 80}}).json()
assert p["summary"] == "Add device Printer" and "192.0.2.50:80 (TCP)" in p["detail"][0], p
dup = c.post("/api/actions/preview", json={"kind": "device_add", "params": {
    "name": "Router", "group": "core", "mode": "ip", "ip": "192.0.2.9"}})
assert dup.status_code == 400, "duplicate device names must be refused"
badmac = c.post("/api/actions/preview", json={"kind": "device_add", "params": {
    "name": "X", "group": "iot", "mode": "mac", "mac": "zz:11"}})
assert badmac.status_code == 400, "invalid MAC must be refused"
ok = c.post("/api/actions/confirm", json={"id": p["id"]}).json()
assert ok["ok"] and "Printer" in (etc / "config.json").read_text(), "confirmed add must persist"
assert (etc / "config.json.bak").read_text() == before, "previous config must be kept as .bak"

# WAN groups: router is offline in this test, so candidates fall back to the configured WANs.
noc._cache["router"] = (time.time() + 1e6, {"error": "offline test"})
post = lambda kind, params: c.post("/api/actions/preview", json={"kind": kind, "params": params})
p = post("wan_group_restart", {"group": "isp-a"}).json()
assert p["summary"] == "Restart all isp-a links (2)" and "wan, wan2" in p["detail"][0], p
assert post("wan_group_restart", {"group": "nope"}).status_code == 400, "unknown group must be refused"
assert post("wan_groups_set", {"groups": {"isp-a": ["wan"], "isp-b": ["wan"]}}).status_code == 400, "an interface in two groups"
assert post("wan_groups_set", {"groups": {"Bad Name!": ["wan"]}}).status_code == 400, "invalid group name"
assert post("wan_groups_set", {"groups": {"isp-a": ["eth9"]}}).status_code == 400, "unknown interface"
p = post("wan_groups_set", {"groups": {"isp-a": ["wan"], "isp-b": ["wan2"]}}).json()
assert c.post("/api/actions/confirm", json={"id": p["id"]}).json()["ok"]
assert noc.wan_groups() == {"isp-a": ["wan"], "isp-b": ["wan2"]} and noc.wan_ifaces() == {"wan", "wan2"}
assert '"isp-b"' in (etc / "config.json").read_text(), "groups must persist"

# Telegram: works without a token, never pairs without one, and never leaks the token.
a = c.get("/api/alerts").json()
assert a["token_set"] is False and a["paired"] is False and a["groups"]["isp-b"] == ["wan2"], a
assert c.post("/api/alerts/pair").status_code == 400, "pairing needs a bot token"
assert not noc.TG.started, "workers must not start in tests"
tg = noc.TG
assert tg.step({"x": (True, "x")}, first=True) == []
assert tg.step({"x": (False, "x down")}, first=False) == [], "one bad reading must not alert"
ch = tg.step({"x": (False, "x down")}, first=False)
assert len(ch) == 1 and ch[0]["ok"] is False, ch
assert tg.step({"x": (True, "x up")}, False) == [] and tg.step({"x": (True, "x up")}, False)[0]["ok"] is True
assert tg.step({"new": (False, "new dev")}, False) == [], "checks added later still need confirmation"
assert "ALL INTERNET" in tg.format_changes([{"key": "wan:ALL", "ok": False, "text": "Internet: 0/2", "lasted": 5}], None)
out = {}
tg.ont_checks({"name": "ONT", "connected": True, "age_s": 10, "rx_dbm": -28.1, "tx_dbm": 2.0, "temp_c": 40,
               "state_code": "O5", "alarms": {"los": "Clear (Normal)"},
               "limits": {"rx_pwr_low_warn": -26, "rx_pwr_high_warn": -9, "tx_pwr_low_warn": 0.5,
                          "tx_pwr_high_warn": 3.5, "temp_low_warn": 0, "temp_high_warn": 65}}, out)
assert out["ont:ONT:rx"][0] is False and out["ont:ONT:tx"][0] and out["ont:ONT:link"][0], out
tg.token_cache = (time.time() + 1e6, "123:SECRET")
real_post = notify.requests.post
notify.requests.post = lambda *a, **k: (_ for _ in ()).throw(OSError("https://api.telegram.org/bot123:SECRET/getMe failed"))
try:
    tg.api("getMe")
    raise AssertionError("api() must raise")
except RuntimeError as ex:
    assert "SECRET" not in str(ex), "bot token leaked into an error message"
finally:
    notify.requests.post, tg.token_cache = real_post, (0.0, "")

# Every bot read command must answer (not raise) with the router up and down.
now = time.time() + 1e6
noc._cache["ping"] = (now, [{"name": "NAS", "ip": "192.0.2.5", "up": False, "ms": None},
                            {"name": "Router", "ip": "192.0.2.1", "up": True, "ms": 0.4}])
noc._cache["dns"] = (now, {"vip": "192.0.2.10", "vip_owner": "Pi-hole A", "nodes": [
    {"name": "Pi-hole A", "ok": True, "blocking": "enabled", "queries": 1000, "percent": 12.5},
    {"name": "Pi-hole B", "ok": False, "error": "timeout"}]})
ont = {"name": "ONT", "connected": True, "age_s": 10, "rx_dbm": -21.1, "tx_dbm": 2.5, "temp_c": 45, "state": "O5",
       "state_code": "O5", "alarms": {}, "limits": {"rx_pwr_low_warn": -26, "rx_pwr_high_warn": -9}}
routers = [{"error": "offline"}, {"interfaces": [{"name": "wan", "up": True, "ip": "198.51.100.2/24"},
                                                 {"name": "wan2", "up": False, "ip": ""}],
                                  "mwan": {"wan": {"status": "online", "latency": 5}, "wan2": {"status": "offline"}},
                                  "onts": [ont]}]
for r in routers:
    noc._cache["router"] = (now, r)
    for cmd in (tg.cmd_status, tg.cmd_wan, tg.cmd_dns, tg.cmd_optics, tg.cmd_devices, tg.cmd_problems):
        out = cmd("")
        assert isinstance(out, str) and out, (cmd.__name__, out)
    readings, vip = tg.evaluate()
    assert vip == "Pi-hole A" and readings["device:NAS"][0] is False, readings
assert "wan2 [isp-b]: offline" in readings["wan:wan2"][1] and readings["wan:ALL"][0] is True, readings
assert "NAS" in tg.cmd_status("") and "Pi-hole B" in tg.cmd_dns("")

# WAN diagnosis: feed canned router output and check each fault is named as the cause.
import diagnose  # noqa: E402

noc.ROUTER["wan_paths"] = {"isp-b": {"switch_port": 2, "isp_device": "Router", "ont": "ONT"}}
noc._cache["switch"] = (now, {"networks": [{"id": "n15", "vlan": 15, "name": "isp-b"}],
                              "lags": [{"lag": 1, "disabled": False, "members_up": 2, "untag_ids": [], "tag_ids": ["n15"]}],
                              "ports": [{"port": 2, "disabled": False, "link": True, "speed": "1G", "sfp": True,
                                         "untag_ids": [], "tag_ids": ["n15"], "native_id": "n1"}]})
good_ont = {**ont, "alarms": {"los": "Clear (Normal)"}, "limits": {"rx_pwr_low_alarm": -27}}
noc._cache["router"] = (now, {"interfaces": [], "mwan": {}, "onts": [good_ont]})


def router_out(status, cfg="disabled=\nproto=dhcp\ndevice=bond.15\nmwan=1\ntrack=8.8.8.8",
               links="DEV bond.15 up 1\nDEV bond up 1\nSLAVE lan1 up 1\nSLAVE lan3 up 1", log="", ping=""):
    return f"\n@@STATUS\n{json.dumps(status)}\n@@CFG\n{cfg}\n@@LINKS\n{links}\n@@LOG\n{log}\n@@PING\n{ping}\n"


UP = {"up": True, "autostart": True, "proto": "dhcp", "l3_device": "bond.15",
      "ipv4-address": [{"address": "198.51.100.9", "mask": 24}]}
DOWN = {"up": False, "autostart": True, "proto": "dhcp", "l3_device": "bond.15"}
cases = [
    ("healthy", router_out(UP, ping="gw ok 198.51.100.1\ntrack ok 8.8.8.8"), None),
    ("disabled", router_out(DOWN, cfg="disabled=1\nproto=dhcp\ndevice=bond.15\nmwan=1\ntrack=8.8.8.8"), "Router config"),
    ("stopped", router_out({**DOWN, "autostart": False}), "Router config"),
    ("lag down", router_out(DOWN, links="DEV bond.15 lowerlayerdown 0\nDEV bond down 0\nSLAVE lan1 down 0\nSLAVE lan3 down 0"), "Router link"),
    ("dhcp silent", router_out(DOWN, log="x udhcpc: started, v1\nx udhcpc: broadcasting discover"), "ISP session"),
    ("no internet", router_out(UP, ping="gw ok 198.51.100.1\ntrack fail 8.8.8.8\nmwan offline"), "Beyond the ISP"),
    ("gw ignores ping", router_out(UP, ping="gw fail 198.51.100.1\ntrack ok 8.8.8.8\nmwan online"), None),
    ("ping flaky, mwan ok", router_out(UP, ping="gw fail 198.51.100.1\ntrack fail 8.8.8.8\nmwan online"), None),
    ("isp dark", router_out(UP, ping="gw fail 198.51.100.1\ntrack fail 8.8.8.8\nmwan offline"), "Beyond the ISP"),
]
real_ssh = noc.ssh
try:
    for name, text, layer in cases:
        noc.ssh = lambda host, script, timeout=20, _t=text: _t
        d = diagnose.diagnose(noc, "wan2")
        assert d["layer"] == layer, (name, d["verdict"], d["steps"])
    # switch port without link, then LOS on the fibre, then a rejected PPPoE login
    noc.ssh = lambda host, script, timeout=20: router_out(DOWN)
    sw = noc._cache["switch"][1]
    sw["ports"][0]["link"] = False
    d = diagnose.diagnose(noc, "wan2")
    assert d["layer"] == "Switch" and "no link on switch port 2" in d["verdict"], d["verdict"]
    assert any(s.get("consequence") for s in d["steps"]), "layers after the cause are marked as consequences"
    sw["ports"][0]["link"] = True
    noc._cache["router"] = (now, {"interfaces": [], "mwan": {}, "onts": [{**good_ont, "alarms": {"los": "Detected"}}]})
    d = diagnose.diagnose(noc, "wan2")
    assert d["layer"] == "Fibre" and "LOS" in d["verdict"], d["verdict"]
    noc._cache["router"] = (now, {"interfaces": [], "mwan": {}, "onts": [good_ont]})
    noc.ssh = lambda host, script, timeout=20: router_out(
        {"up": False, "autostart": True, "proto": "pppoe", "errors": [{"code": "AUTH_TOPEER_FAILED"}]},
        cfg="disabled=\nproto=pppoe\ndevice=wanmac0\nmwan=1\ntrack=8.8.8.8", links="DEV wanmac0 up 1\nDEV wan up 1")
    d = diagnose.diagnose(noc, "wan2")
    assert d["layer"] == "ISP session" and "rejected the PPPoE login" in d["verdict"], d["verdict"]
    assert "Cause" in tg.format_diag(d) and "rejected" in tg.format_causes({"wan2": d})[0]
    noc.ssh = lambda host, script, timeout=20: (_ for _ in ()).throw(RuntimeError("ssh timeout"))
    assert diagnose.diagnose(noc, "wan2")["layer"] == "Router"
    try:
        diagnose.diagnose(noc, "wan2; reboot")
        raise AssertionError("shell metacharacters must be refused")
    except ValueError:
        pass
finally:
    noc.ssh = real_ssh

# Settings tab: everything editable, secrets write-only and never echoed.
noc._cache["router"] = (now, {"error": "offline test"})
st = c.get("/api/settings").json()
assert {"general", "dns", "connections", "monitoring", "onts", "protected_ports", "wan"} <= set(st), st.keys()
assert st["dns"]["piholes"][0]["password_set"] is True and "password" not in st["dns"]["piholes"][0]
assert "test-pass" not in json.dumps(st) and "x" != st["connections"].get("pve_token_set"), "secrets must not be returned"
pv = lambda kind, params: c.post("/api/actions/preview", json={"kind": kind, "params": params})
piholes = [{"name": x["name"], "url": x["url"], "mac": x["mac"], "original_name": x["name"]} for x in st["dns"]["piholes"]]
r = pv("settings_dns", {"vip": "192.0.2.10", "piholes": piholes + [{"name": "Pi-hole C", "url": "http://192.0.2.13"}]})
assert r.status_code == 400 and "app password" in r.json()["error"], "a new Pi-hole needs its password"
assert pv("settings_dns", {"vip": "300.1.1.1", "piholes": piholes}).status_code == 400, "bad VIP"
assert pv("settings_dns", {"vip": "192.0.2.10", "piholes": piholes + [{"name": "X", "url": "ftp://x", "password": "p"}]}).status_code == 400
r = pv("settings_dns", {"vip": "192.0.2.20", "piholes": piholes + [{"name": "Pi-hole C", "url": "http://192.0.2.13:8080",
                                                                    "mac": "00:00:5E:00:53:03", "password": "S3cret-C"}]}).json()
txt = json.dumps(r, ensure_ascii=False)
assert "S3cret-C" not in txt and "Add Pi-hole C" in txt and "login test failed (no answer" in txt and "app password: will be set" in txt and "VIP: 192.0.2.10 → 192.0.2.20" in txt, r
assert c.post("/api/actions/confirm", json={"id": r["id"]}).json()["ok"]
assert "PIHOLE_C_PASS=S3cret-C" in (etc / "secrets.env").read_text(), "password must land in secrets.env"
assert noc.DNS_VIP == "192.0.2.20" and len(noc.PIHOLES) == 3 and noc.VIP_MACS["00:00:5e:00:53:03"] == "Pi-hole C", "applied live"
assert "S3cret-C" not in (var / "audit.jsonl").read_text() and "S3cret-C" not in (etc / "config.json").read_text()
st = c.get("/api/settings").json()
assert [x["password_set"] for x in st["dns"]["piholes"]][-1] is True and "S3cret-C" not in json.dumps(st)

assert pv("settings_monitoring", {"check_every_s": 5, "confirm_checks": 2, "ont_stale_s": 180}).status_code == 400
r = pv("settings_monitoring", {"check_every_s": 60, "confirm_checks": 3, "ont_stale_s": 300}).json()
assert c.post("/api/actions/confirm", json={"id": r["id"]}).json()["ok"] and tg.cfg()["confirm_checks"] == 3

noc.ROUTER["wan_paths"] = {"isp-b": {"ont": "Fibre ONT"}}
r = pv("settings_onts", {"onts": [{"name": "Main ONT", "path": "/tmp/vsol_ddm_cache.json", "original_name": "Fibre ONT"}]}).json()
assert "Rename Fibre ONT → Main ONT" in r["detail"], r
assert c.post("/api/actions/confirm", json={"id": r["id"]}).json()["ok"]
assert noc.ONT_CACHES[0]["name"] == "Main ONT" and noc.ROUTER["wan_paths"]["isp-b"]["ont"] == "Main ONT", "renames follow"
assert "@@ONT0" in noc.router_sh() and "vsol_ddm_cache.json" in noc.router_sh()
assert pv("settings_onts", {"onts": [{"name": "Bad", "path": "relative.json"}]}).status_code == 400

r = pv("settings_protected", {"ports": {"1": "Uplink to router", "7": "NAS"}}).json()
assert c.post("/api/actions/confirm", json={"id": r["id"]}).json()["ok"] and noc.PROTECTED_PORTS == {1: "Uplink to router", 7: "NAS"}
assert pv("settings_protected", {"ports": {"99": "x"}}).status_code == 400

r = pv("settings_general", {"site_name": "Lab NOC"}).json()
assert c.post("/api/actions/confirm", json={"id": r["id"]}).json()["ok"] and noc.SITE_NAME == "Lab NOC"

con = c.get("/api/settings").json()["connections"]
base = {k: v for k, v in con.items() if not k.endswith("_set")}
r = pv("settings_connections", {**base, "omada_url": "https://192.0.2.40:8043", "pve_token": "noc@pve!ro=abc"}).json()
assert "Omada URL" in " ".join(r["detail"]) and "Proxmox token: will be replaced" in r["detail"] and "abc" not in json.dumps(r), r
assert c.post("/api/actions/confirm", json={"id": r["id"]}).json()["ok"]
assert noc.OMADA.url == "https://192.0.2.40:8043" and noc.E["PVE_TOKEN"] == "noc@pve!ro=abc", "applied live"
assert pv("settings_connections", {**base, "pve_token": "not-a-token"}).status_code == 400
bad = pv("settings_connections", {**base, "router_host": "192.0.2.254"})
assert bad.status_code == 400 and "host key" in bad.json()["error"], "a new router address must present an SSH host key"
r = pv("wan_groups_set", {"groups": {"isp-a": ["wan"], "isp-b": ["wan2"]},
                          "paths": {"isp-b": {"switch_port": 2, "isp_device": "Router", "ont": "Main ONT"}}}).json()
assert any("isp-b path" in x for x in r["detail"]), r
assert pv("wan_groups_set", {"groups": {"isp-a": ["wan"]}, "paths": {"isp-a": {"ont": "Nope"}}}).status_code == 400

print("smoke test: all checks passed")
