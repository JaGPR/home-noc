"""Offline smoke test: boots the app with the example config and dummy secrets.

No network access is needed: it only exercises auth, metadata and an action preview.
Run:  python tests/smoke_test.py
"""
import json
import re
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

# telegram.mgmt_grace: an ISP box's management drop is held unless it lasts, its WANs are down or the fibre is bad.
real_paths, real_grace = noc.ROUTER.get("wan_paths"), noc.C.setdefault("telegram", {}).get("mgmt_grace")
noc.ROUTER["wan_paths"] = {"isp-b": {"isp_device": "Box", "ont": "ONT"}}
noc.C["telegram"]["mgmt_grace"] = {"Box": 300}
try:
    def held(wan_ok=True, link=None, age=0):
        r = {"device:Box": (False, "Box down"), "wan:wan2": (wan_ok, "wan2"), "ont:ONT:poll": (False, "no reading")}
        if link is not None:
            r["ont:ONT:link"] = (link, "ONT")
        tg.mgmt_down = {"device:Box": time.time() - age}
        tg.apply_grace(r)
        return r["device:Box"][0], r["ont:ONT:poll"][0]
    assert held() == (True, True), "a short management blip is held, poller staleness with it"
    assert held(age=301) == (False, False), "longer than the grace: alert"
    assert held(wan_ok=False) == (False, False), "its internet is down too: alert at once"
    assert held(link=False) == (False, False), "fibre not OK: alert at once"
    r = {"device:Box": (True, "Box")}
    tg.apply_grace(r)
    assert "device:Box" not in tg.mgmt_down, "recovery resets the timer"
finally:
    noc.ROUTER["wan_paths"] = real_paths
    noc.C["telegram"].pop("mgmt_grace", None) if real_grace is None else noc.C["telegram"].update(mgmt_grace=real_grace)
    tg.mgmt_down = {}
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

# Page views may get a recent stale value (refreshed in the background); everything else is strict.
noc._cache["t1"] = (time.time() - 100, {"v": 1})
assert noc.cached("t1", 10, lambda: {"v": 2}, stale=300) == {"v": 1}, "page view gets the stale value at once"
for _ in range(50):
    if noc._cache["t1"][1] == {"v": 2}:
        break
    time.sleep(0.05)
assert noc._cache["t1"][1] == {"v": 2}, "and it is refreshed in the background"
noc._cache["t2"] = (time.time() - 100, {"v": 1})
assert noc.cached("t2", 10, lambda: {"v": 2}) == {"v": 2}, "watcher/actions always get fresh data"
noc._cache["t3"] = (time.time() - 100, {"error": "x"})
assert noc.cached("t3", 10, lambda: {"v": 3}, stale=300) == {"v": 3}, "a cached error is never served stale"

# Link quality, measured through each WAN (mwan3's own numbers are 0 without quality checks).
real_ssh = noc.ssh
noc.ssh = lambda host, script, timeout=20: (
    "wan|8.8.8.8|3 packets transmitted, 3 packets received, 0% packet loss round-trip min/avg/max = 14.7/16.04/18.6 ms \n"
    "wan2|1.1.1.1|3 packets transmitted, 0 packets received, 100% packet loss \n")
try:
    q = noc.collect_wan_quality()
finally:
    noc.ssh = real_ssh
assert q["wan"] == {"loss": 0, "latency": 16.0, "target": "8.8.8.8", "measured": True}, q
assert q["wan2"]["loss"] == 100 and q["wan2"]["latency"] is None, q

# OpenWrt editing (offline): canned `uci -X show` output stands in for the router.
import openwrt  # noqa: E402

UCI = """network.cfg01=device
network.cfg01.name='bond'
network.cfg01.type='bonding'
network.cfg01.ports='lan1' 'lan3'
network.cfg02=device
network.cfg02.type='8021q'
network.cfg02.ifname='bond'
network.cfg02.vid='20'
network.cfg02.name='bond.20'
network.cfg03=device
network.cfg03.type='8021q'
network.cfg03.ifname='bond'
network.cfg03.vid='30'
network.cfg03.name='bond.30'
network.lan=interface
network.lan.device='br-lan'
network.lan.proto='static'
network.lan.ipaddr='192.0.2.1/24'
network.ispb=interface
network.ispb.device='bond.20'
network.ispb.proto='pppoe'
network.ispb.username='user1'
network.ispb.password='TopSecret'
firewall.cfg10=zone
firewall.cfg10.name='lan'
firewall.cfg10.network='lan'
firewall.cfg11=zone
firewall.cfg11.name='wan'
firewall.cfg11.network='ispb'
dhcp.lan=dhcp
dhcp.lan.interface='lan'
dhcp.lan.start='100'
dhcp.lan.limit='150'
mwan3.ispb=interface
mwan3.ispb.enabled='1'
mwan3.ispb.track_ip='8.8.8.8'
mwan3.m_ispb=member
mwan3.m_ispb.interface='ispb'
mwan3.m_ispb.metric='1'
mwan3.m_ispb.weight='1'
mwan3.balanced=policy
mwan3.balanced.use_member='m_ispb'
mwan3.rule_all=rule
mwan3.rule_all.use_policy='balanced'
mwan3.rule_all.dest_ip='0.0.0.0/0'
"""
noc.ssh = lambda host, script, timeout=20: UCI + "\n@@MWANSTATUS\n" + '{"interfaces": {"ispb": {"status": "online"}}}'
try:
    cfg = openwrt.read(noc)
    assert "TopSecret" not in json.dumps(cfg), "PPPoE password must never be read back"
    ispb = next(i for i in cfg["interfaces"] if i["name"] == "ispb")
    assert ispb["password_set"] and ispb["zone"] == "wan" and ispb["mwan"], ispb
    assert next(d for d in cfg["devices"] if d["name"] == "bond.20")["used_by"] == ["interface ispb"]
    assert next(d for d in cfg["devices"] if d["name"] == "bond.30")["used_by"] == []
    pv = lambda kind, params: c.post("/api/actions/preview", json={"kind": kind, "params": params})
    # the wizard: new VLAN device + static LAN interface + DHCP server in the lan zone
    r = pv("ow_setup", {"device": {"mode": "new", "type": "8021q", "base": "bond", "vid": 40, "ipv6": False},
                        "interface": {"name": "guest", "proto": "static", "ipaddr": "10.0.40.1/24", "ipv6": False, "auto": True},
                        "zone": "lan", "dhcp_server": {"enabled": True, "start": 100, "limit": 50, "leasetime": "12h"}}).json()
    txt = " | ".join(r["detail"])
    assert "New device bond.40: VLAN 40 (802.1q) on bond" in txt and "Firewall zone: none → lan" in txt and "DHCP server: on" in txt, r
    # a WAN with mwan3 member + policy, PPPoE password never echoed
    r = pv("ow_setup", {"device": {"mode": "new", "type": "8021q", "base": "bond", "vid": 50},
                        "interface": {"name": "ispc", "proto": "pppoe", "username": "u2", "password": "Pw-123", "metric": 5},
                        "zone": "wan", "mwan": {"enabled": True, "track_ip": ["8.8.8.8", "1.1.1.1"], "reliability": 1,
                                                "metric": 2, "weight": 3, "policies": ["balanced"]}})
    assert r.status_code == 200 and "Pw-123" not in r.text and "member m_ispc (metric 2, weight 3), in policies balanced" in r.text, r.text
    b = openwrt.Batch(); b.set("network.x.y", "it's")  # noqa: E702
except ValueError as ex:
    assert "quotes" in str(ex), ex
else:
    raise AssertionError("values with quotes must be refused")
try:
    assert pv("ow_device_delete", {"id": "cfg02"}).json()["error"].startswith("bond.20 is in use"), "in-use devices stay"
    assert pv("ow_device_delete", {"id": "cfg03"}).status_code == 200
    assert "rule_all" in pv("ow_mwan_policy_delete", {"name": "balanced"}).json()["error"], "policies used by rules stay"
    assert pv("ow_iface_delete", {"name": "lan"}).status_code == 400, "LAN can't be deleted"
    r = pv("ow_iface_delete", {"name": "ispb"}).json()
    assert r["confirm_word"] == "ispb" and any("member m_ispb" in x for x in r["detail"]) and any("policy balanced" in x for x in r["detail"]), r
    assert pv("ow_iface_edit", {"interface": {"name": "ispb", "proto": "pppoe", "device": "bond.20", "username": "user1", "password": ""}}).status_code == 400, \
        "an edit that changes nothing is refused"
    r = pv("ow_iface_edit", {"interface": {"name": "ispb", "proto": "pppoe", "device": "bond.20", "username": "user2", "password": ""}}).json()
    assert "username: user1 → user2" in r["detail"], r
    r = pv("ow_mwan_rule", {"name": "tv", "proto": "tcp", "src_ip": "192.0.2.50/32", "dest_port": "443", "use_policy": "balanced"}).json()
    assert any("tcp from 192.0.2.50/32" in x for x in r["detail"]), r
    assert pv("ow_mwan_rule", {"name": "bad", "proto": "all", "dest_port": "443", "use_policy": "balanced"}).status_code == 400, "ports need tcp/udp"
    assert pv("ow_setup", {"interface": {"name": "x1", "proto": "static", "ipaddr": "10.0.0.1", "device": "bond.30"}}).status_code == 400, "prefix required"
    # several MAC VLANs on one device, each with a PPPoE interface, its own gateway metric, mwan3 member
    mv = lambda **kw: pv("ow_macvlan_bulk", {"base": "bond.30", "mode": "vepa", "mac": "random", "interfaces": True, "proto": "pppoe",
                                            "username": "isp-user", "password": "Mv-Secret", "zone": "wan",
                                            "rows": [{"dev_name": "b30mac0", "ifname": "isp7", "metric": 7}, {"dev_name": "b30mac1", "ifname": "isp8", "metric": 8}],
                                            "mwan": {"enabled": True, "track_ip": ["8.8.8.8"], "metric": 1, "weight": 1, "policies": ["balanced"]}, **kw})
    r = mv()
    assert r.status_code == 200 and "Mv-Secret" not in r.text, r.text
    txt = " | ".join(r.json()["detail"])
    assert "2 MAC VLAN device(s) on bond.30 (vepa mode), a fixed random MAC each" in txt and "New device b30mac0: MAC VLAN on bond.30" in txt, txt
    assert "interface isp7: pppoe on b30mac0, metric 7" in txt and "interface isp8: pppoe on b30mac1, metric 8" in txt, txt
    assert "member m_isp8 (metric 1, weight 1), in policies balanced" in txt and "All in firewall zone wan" in txt, txt
    assert re.search(r"MAC address: hardware default → [0-9a-f]{2}(:[0-9a-f]{2}){5}", txt), "a fixed random MAC is set"
    assert mv(rows=[{"dev_name": "b30mac0", "ifname": "ispb"}]).status_code == 400, "existing interface name refused"
    assert mv(rows=[]).status_code == 400 and mv(base="nosuch").status_code == 400
    assert mv(proto="static", rows=[{"dev_name": "b30mac0", "ifname": "st1", "ipaddr": "10.9.0.2"}]).status_code == 400, "static needs a prefix"
    r = mv(proto="static", zone="lan", mwan=None, rows=[{"dev_name": "b30mac0", "ifname": "st1", "ipaddr": "10.9.0.2/24", "gateway": "10.9.0.1", "metric": 30}]).json()
    assert any("st1: static on b30mac0, metric 30, 10.9.0.2/24 via 10.9.0.1" in x for x in r["detail"]), r
    # mwan3: add an interface with a member in a policy, and remove one
    r = pv("ow_mwan_iface", {"name": "lan", "track_ip": ["1.1.1.1"], "reliability": 1,
                             "member": {"name": "m_lan", "metric": 3, "weight": 2, "policies": ["balanced"]}}).json()
    txt = " | ".join(r["detail"])
    assert "New mwan3 interface lan" in txt and "New member m_lan: metric 3, weight 2, in policies balanced" in txt and "not 'wan'" in txt, r
    assert pv("ow_mwan_iface", {"name": "nosuch", "track_ip": ["1.1.1.1"]}).status_code == 400, "must be a router interface"
    assert pv("ow_mwan_iface", {"name": "lan", "track_ip": ["1.1.1.1"], "member": {"name": "m_ispb"}}).status_code == 400, "member name taken"
    r = pv("ow_mwan_iface_delete", {"name": "ispb"}).json()
    txt = " | ".join(r["detail"])
    assert r["confirm_word"] == "ispb" and "Delete its member m_ispb" in txt and "left with no members" in txt, r
finally:
    noc.ssh = real_ssh

# "New network" = Omada VLAN + LAG tag + router setup as ONE action; a failing router step undoes the switch side.
assert str(noc.NETMETA).startswith(str(var)), "network plans must live in NOC_VAR, not a hard-coded path"
calls = []
FAKE_SW = {"networks": [{"id": "n1", "name": "Default", "vlan": 1, "editable": False, "igmp": False}],
           "lags": [{"lag": 1, "name": "LAG 1", "ports": [4, 5], "native_id": "n1", "untag_ids": ["n1"], "tag_ids": []}],
           "ports": [], "profiles": []}
real_cs, real_call, real_pd = noc.collect_switch, noc.OMADA.call, noc.port_details


def fake_call(method, path, **kw):
    calls.append((method, path))
    if method == "GET" and path == "/lan-networks":
        return {"data": [{"id": "nNEW", "name": "Guest", "vlan": 60}]}
    return {}


noc.collect_switch, noc.OMADA.call = (lambda: FAKE_SW), fake_call
noc.port_details = lambda lag_ids=(): {"_lags": {1: {"nativeNetworkId": "n1", "untagNetworkIds": ["n1"], "tagNetworkIds": ["nNEW"]}}}
noc.ssh = lambda host, script, timeout=20: UCI + "\n@@MWANSTATUS\n{}"   # the router "refuses" every change
try:
    params = {"name": "Guest", "vlan": 60, "igmp": False, "carry_lag": 1,
              "dhcp": {"device": "openwrt", "gateway": "10.0.60.1", "prefix": 24, "enabled": True,
                       "range_start": "10.0.60.100", "range_end": "10.0.60.199", "lease_h": 12, "dns": "192.0.2.10"},
              "router": {"device": {"mode": "new", "type": "8021q", "base": "bond", "vid": 60},
                         "interface": {"name": "Guest", "proto": "static", "ipaddr": "10.0.60.1/24", "ipv6": False, "auto": True},
                         "zone": "lan", "dhcp_server": {"enabled": True, "start": 100, "limit": 100, "leasetime": "12h", "dns": "192.0.2.10"}}}
    r = c.post("/api/actions/preview", json={"kind": "net_create", "params": params}).json()
    txt = " | ".join(r["detail"])
    assert r["summary"] == "Create network Guest (VLAN 60) on the switch and the router", r
    assert "carry it tagged on LAG 1" in txt and "New device bond.60" in txt and "hands out DNS 192.0.2.10" in txt and "removed again" in txt, txt
    assert calls == [], "a preview must not touch the controller"
    out = c.post("/api/actions/confirm", json={"id": r["id"]})
    assert out.status_code == 500 and "removed again" in out.json()["error"], out.text
    assert ("POST", "/lan-networks") in calls and ("DELETE", "/lan-networks/nNEW") in calls, calls
    lag_patches = [x for x in calls if x == ("PATCH", f"/switches/{noc.SWITCH_MAC}/lags/1")]
    assert len(lag_patches) == 2, "LAG tagged, then untagged again on failure"
    assert "nNEW" not in noc.load_meta(), "the DHCP plan of the removed network is dropped"
    # switch-only (no router steps) still works as before
    calls.clear()
    r = c.post("/api/actions/preview", json={"kind": "net_create", "params": {**params, "router": None, "carry_lag": None}}).json()
    assert r["summary"] == "Create network Guest (VLAN 60)" and "Not on any port yet" in " ".join(r["detail"]), r
finally:
    noc.collect_switch, noc.OMADA.call, noc.port_details, noc.ssh = real_cs, real_call, real_pd, real_ssh

# Deleting a VLAN: router device / interface / mwan3 are removed only when ticked, router first.
calls.clear()
DEL_SW = {"networks": [{"id": "n20", "name": "ISPB", "vlan": 20, "editable": True, "igmp": False}],
          "lags": [], "ports": [], "profiles": []}
noc.collect_switch, noc.OMADA.call = (lambda: DEL_SW), fake_call
noc.ssh = lambda host, script, timeout=20: UCI + "\n@@MWANSTATUS\n{}"   # router refuses changes (no "APPLIED")
try:
    pvd = lambda router=None: c.post("/api/actions/preview", json={"kind": "net_delete", "params": {"id": "n20", **({"router": router} if router else {})}}).json()
    r = pvd()
    assert r["summary"] == "Delete network ISPB" and "kept on the router" not in " ".join(r["detail"]), r  # no users known offline
    r = pvd({"mwan": True})
    txt = " | ".join(r["detail"])
    assert "stop tracking ispb" in txt and "delete member m_ispb" in txt and "Delete interface" not in txt and "Delete device" not in txt, txt
    r = pvd({"interface": True})
    txt = " | ".join(r["detail"])
    assert "Delete interface ispb" in txt and "stop tracking ispb" in txt and "Delete device" not in txt, "interface pulls in mwan3: " + txt
    r = pvd({"device": True})
    txt = " | ".join(r["detail"])
    assert all(x in txt for x in ("Delete device bond.20", "Delete interface ispb", "stop tracking ispb")), txt
    assert r["summary"] == "Delete network ISPB and its router setup" and r["confirm_word"] == "vlan 20", r
    out = c.post("/api/actions/confirm", json={"id": r["id"], "confirm_text": "vlan 20"})
    assert out.status_code == 500 and not any(x[0] == "DELETE" for x in calls), "router failed first, so the switch VLAN must stay"
finally:
    noc.collect_switch, noc.OMADA.call, noc.ssh = real_cs, real_call, real_ssh

# NOC password: change it (signs old sessions out), then reset it with a Telegram code.
import auth  # noqa: E402
import passwd  # noqa: E402

assert auth.verify("x" * 12, {"NOC_ADMIN_PASS_HASH": auth.hash_password("x" * 12, 1000)}) and not auth.verify("", {})
pwpost = lambda path, body: c.post(path, json=body)
old_cookie = c.cookies.get("noc")
assert pwpost("/api/account/password", {"current": "wrong", "new": "brand-new-pass-1"}).status_code == 400, "current password checked"
assert "10 characters" in pwpost("/api/account/password", {"current": "test-pass", "new": "short"}).json()["error"]
r = pwpost("/api/account/password", {"current": "test-pass", "new": "brand-new-pass-1"})
assert r.status_code == 200 and "noc" in r.cookies, r.text
sec = (etc / "secrets.env").read_text()
assert "NOC_ADMIN_PASS_HASH=pbkdf2_sha256$" in sec and "\nNOC_ADMIN_PASS=\n" in sec and "brand-new-pass-1" not in sec, "only a hash is stored"
assert c.get("/api/audit").status_code == 200, "the changer stays signed in"
assert TestClient(noc.app, cookies={"noc": old_cookie}).get("/api/audit").status_code == 401, "old sessions are signed out"
anon = TestClient(noc.app)
assert anon.post("/login", data={"user": "admin", "password": "test-pass"}, follow_redirects=False).headers["location"].endswith("bad=1")
assert anon.post("/login", data={"user": "admin", "password": "brand-new-pass-1"}, follow_redirects=False).headers["location"] == "/"

r = anon.post("/login/forgot")
assert r.status_code == 400 and r.json().get("console"), "no Telegram: point to noc-passwd"
sent = []
real_tok, real_chat, real_send = noc.TG.token, noc.TG.chat_id, noc.TG.send
noc.TG.token, noc.TG.chat_id, noc.TG.send = (lambda: "tok"), (lambda: 42), (lambda chat, text, **kw: sent.append(text))
try:
    assert anon.post("/login/forgot").json()["sent"] and len(sent) == 1, sent
    assert anon.post("/login/forgot").status_code == 429, "one code a minute"
    code = re.search(r"<code>(\d{4}-\d{4})</code>", sent[0]).group(1)
    wrong = "0000-0000" if code != "0000-0000" else "1111-1111"
    assert "tries left" in anon.post("/login/reset", json={"code": wrong, "new": "reset-pass-123"}).json()["error"]
    assert anon.post("/login/reset", json={"code": code, "new": "short"}).status_code == 400, "weak password refused"
    r = anon.post("/login/reset", json={"code": code, "new": "reset-pass-123"})
    assert r.status_code == 200 and "noc" in r.cookies and anon.get("/api/audit").status_code == 200, r.text
    assert anon.post("/login/reset", json={"code": code, "new": "other-pass-123"}).status_code == 400, "codes are single use"
    assert c.get("/api/audit").status_code == 401, "the reset signs everyone else out"
    noc.RESET.update(sent=0.0)
    anon.post("/login/forgot")
    for _ in range(noc.RESET_TRIES):
        r = anon.post("/login/reset", json={"code": wrong, "new": "reset-pass-123"})
    assert "too many" in r.json()["error"] and noc.RESET["hash"] is None, "the code dies after 5 wrong tries"
finally:
    noc.TG.token, noc.TG.chat_id, noc.TG.send = real_tok, real_chat, real_send
assert TestClient(noc.app).post("/login", data={"user": "admin", "password": "reset-pass-123"}, follow_redirects=False).headers["location"] == "/"
# the console tool writes the same format
passwd.write_env(etc / "secrets.env", auth.updates("console-pass-99"))
assert auth.verify("console-pass-99", noc.load_env(etc / "secrets.env"))

print("smoke test: all checks passed")
