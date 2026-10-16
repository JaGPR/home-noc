"""NOC command center: one place to see and control the home network.

Reads: OpenWrt router + AP (SSH/ubus), Pi-hole x2 (API), Omada switch (Open API),
Proxmox (read-only token), ping checks. Writes always go through preview -> confirm
and are appended to an audit log.
"""
import concurrent.futures as cf
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import threading
import time
from pathlib import Path

import requests
import urllib3
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

urllib3.disable_warnings()

BASE = Path(__file__).parent
ETC = Path(os.environ.get("NOC_ETC", "/etc/noc"))
VAR = Path(os.environ.get("NOC_VAR", "/var/lib/noc"))
AUDIT = VAR / "audit.jsonl"
KEY = str(ETC / "id_ed25519")
KNOWN_HOSTS = str(ETC / "known_hosts")


def load_env(path):
    """Secrets only (passwords, tokens). KEY=VALUE per line, # comments."""
    env = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


E = load_env(ETC / "secrets.env")
# Network layout (no secrets): see config/config.example.json in the repo.
C = json.loads((ETC / "config.json").read_text())

SITE_NAME = C.get("site_name", "Home NOC")
# Devices checked for the overview. "target" is an IP (ping) or "host:port" (TCP check,
# for devices that ignore ping from other subnets).
DEVICES = [(d["name"], d["target"], d.get("group", "other")) for d in C.get("devices", [])]

DNS = C.get("dns", {})
DNS_VIP = DNS.get("vip")
VIP_MACS = {p["mac"].lower(): p["name"] for p in DNS.get("piholes", []) if p.get("mac")}

ROUTER = C.get("router", {})
ROUTER_HOST = ROUTER.get("host")
AP_HOST = (C.get("access_point") or {}).get("host")
LAN_IFACE = ROUTER.get("lan_interface", "lan")
TRUNK_DEV = ROUTER.get("trunk_device", "bond")  # VLANs arrive as <trunk>.<vid>, e.g. bond.123
WAN_IFACES = set(ROUTER.get("wan_interfaces", []))
# DDM caches written on the router by luci-app-vsol-ddm / luci-app-hsgq-ddm.
ONT_CACHES = ROUTER.get("ont_caches", [])

SW = C.get("switch", {})
SWITCH_MAC = SW.get("mac", "")
SWITCH_PORTS = int(SW.get("ports", 10))
PROTECTED_PORTS = {int(k): v for k, v in (SW.get("protected_ports") or {}).items()}

NEWNET = C.get("new_network_defaults", {})

# ---------------------------------------------------------------- helpers
_cache: dict = {}
_cache_lock = threading.Lock()


def cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception as e:  # surface collector failures to the UI instead of crashing
        val = {"error": f"{type(e).__name__}: {e}"}
    with _cache_lock:
        _cache[key] = (time.time(), val)
    return val


def invalidate(*keys):
    with _cache_lock:
        for k in keys:
            _cache.pop(k, None)


def ssh(host, script, timeout=20):
    r = subprocess.run(
        ["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "StrictHostKeyChecking=yes",
         f"root@{host}", "sh -s"],
        input=script, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"ssh exit {r.returncode}")
    return r.stdout


def sections(text):
    """Split '@@NAME' delimited output into {NAME: body}."""
    out, name, buf = {}, None, []
    for line in text.splitlines():
        if line.startswith("@@"):
            if name:
                out[name] = "\n".join(buf)
            name, buf = line[2:].strip(), []
        else:
            buf.append(line)
    if name:
        out[name] = "\n".join(buf)
    return out


def jload(s, default=None):
    try:
        return json.loads(s)
    except Exception:
        return default


def audit(request, action, summary, result):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ip": request.client.host if request.client else "",
             "via": request.headers.get("cf-access-authenticated-user-email", ""),
             "action": action, "summary": summary, "result": result}
    with AUDIT.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------- ping / VIP
def ping(ip):
    if ":" in ip:  # "host:port" -> TCP connect check
        host, port = ip.rsplit(":", 1)
        t = time.time()
        try:
            socket.create_connection((host, int(port)), timeout=2).close()
            return {"up": True, "ms": round((time.time() - t) * 1000, 1)}
        except OSError:
            return {"up": False, "ms": None}
    r = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, text=True)
    m = re.search(r"time=([\d.]+)", r.stdout)
    return {"up": r.returncode == 0, "ms": float(m.group(1)) if m else None}


def collect_ping():
    with cf.ThreadPoolExecutor(16) as ex:
        res = dict(zip([d[1] for d in DEVICES], ex.map(ping, [d[1] for d in DEVICES])))
    return [{"name": n, "ip": ip, "group": g, **res[ip]} for n, ip, g in DEVICES]


def vip_owner():
    """Which Pi-hole currently answers ARP for the DNS VIP (keepalived)."""
    if not DNS_VIP:
        return "no VIP configured"
    subprocess.run(["ping", "-c", "1", "-W", "1", DNS_VIP], capture_output=True)
    for line in Path("/proc/net/arp").read_text().splitlines()[1:]:
        parts = line.split()
        if parts[0] == DNS_VIP:
            return VIP_MACS.get(parts[3].lower(), parts[3])
    return "unknown"


# ---------------------------------------------------------------- OpenWrt
# Each marker is preceded by a newline: some sources (DDM caches) don't end with one.
WIFI_SH = r"""
for i in $(ubus list 'hostapd.*' 2>/dev/null | cut -d. -f2); do
  printf '\n@@WIFIINFO %s\n' "$i"; ubus call iwinfo info "{\"device\":\"$i\"}"
  printf '\n@@WIFIASSOC %s\n' "$i"; ubus call iwinfo assoclist "{\"device\":\"$i\"}"
done
"""
ROUTER_SH = r"""
printf '\n@@IF\n'; ubus call network.interface dump
printf '\n@@MWAN\n'; ubus call mwan3 status 2>/dev/null || echo '{}'
printf '\n@@SYS\n'; ubus call system info
printf '\n@@BOARD\n'; ubus call system board
printf '\n@@DEV\n'; ubus call network.device status
printf '\n@@LEASES\n'; cat /tmp/dhcp.leases
printf '\n@@DHCPCFG\n'; uci -q show dhcp | grep -E "^dhcp\.[A-Za-z0-9_]+\.(interface|start|limit|leasetime|ignore|dhcpv4)="
""" + "".join(f"printf '\\n@@ONT{i}\\n'; cat {shlex.quote(o['path'])} 2>/dev/null || echo '{{}}'\n"
              for i, o in enumerate(ONT_CACHES)) + WIFI_SH
AP_SH = "printf '\\n@@SYS\\n'; ubus call system info\nprintf '\\n@@BOARD\\n'; ubus call system board\n" + WIFI_SH


def parse_system(sec):
    info, board = jload(sec.get("SYS"), {}), jload(sec.get("BOARD"), {})
    mem = info.get("memory", {})
    return {"model": board.get("model"), "release": (board.get("release") or {}).get("description"),
            "hostname": board.get("hostname"), "uptime": info.get("uptime"),
            "load": [round(x / 65536, 2) for x in info.get("load", [])],
            "mem_used_pct": round(100 * (1 - mem.get("available", 0) / mem["total"]), 1) if mem.get("total") else None}


def parse_wifi(sec, names):
    radios = []
    for key, body in sec.items():
        if not key.startswith("WIFIINFO "):
            continue
        iface = key.split(" ", 1)[1]
        info = jload(body, {})
        clients = []
        for c in (jload(sec.get(f"WIFIASSOC {iface}"), {}) or {}).get("results", []):
            mac = c.get("mac", "").lower()
            clients.append({"mac": mac, "name": names.get(mac, {}).get("name", ""), "ip": names.get(mac, {}).get("ip", ""),
                            "signal": c.get("signal"), "rx_mbps": round((c.get("rx") or {}).get("rate", 0) / 1000),
                            "tx_mbps": round((c.get("tx") or {}).get("rate", 0) / 1000), "inactive_ms": c.get("inactive")})
        radios.append({"iface": iface, "ssid": info.get("ssid"), "channel": info.get("channel"),
                       "band": "5 GHz" if (info.get("frequency") or 0) > 4000 else "2.4 GHz",
                       "htmode": info.get("htmode"), "txpower": info.get("txpower"), "clients": clients})
    return radios


def parse_ont(raw, name):
    """One luci-app-vsol-ddm / luci-app-hsgq-ddm cache file -> compact summary."""
    vs = jload(raw, {}) or {}
    ddm, onu, dev = vs.get("ddm", {}), vs.get("onu", {}), vs.get("device", {})
    return {"name": name, "connected": vs.get("connected"), "rx_dbm": ddm.get("rx_power_dbm"),
            "tx_dbm": ddm.get("tx_power_dbm"), "temp_c": ddm.get("temperature_c"),
            "state": onu.get("registered_status"), "uptime": dev.get("uptime"), "firmware": dev.get("firmware"),
            "model": dev.get("model"), "fec_uncorrectable": ddm.get("fec_uncorrectable_codewords"),
            "alarms": {k[6:]: v for k, v in onu.items() if k.startswith("alarm_")}, "ts": vs.get("timestamp"),
            "age_s": int(time.time() - vs["timestamp"]) if vs.get("timestamp") else None}


def collect_router():
    if not ROUTER_HOST:
        raise RuntimeError("router.host is not set in config.json")
    sec = sections(ssh(ROUTER_HOST, ROUTER_SH))
    leases = []
    for line in sec.get("LEASES", "").splitlines():
        p = line.split()
        if len(p) >= 4:
            leases.append({"expires": int(p[0]), "mac": p[1].lower(), "ip": p[2], "name": "" if p[3] == "*" else p[3]})
    names = {l["mac"]: l for l in leases}

    ifaces = []
    for i in (jload(sec.get("IF"), {}) or {}).get("interface", []):
        if i.get("interface") == "loopback":
            continue
        addr = (i.get("ipv4-address") or [{}])[0]
        gw = next((r.get("nexthop") for r in i.get("route", []) if r.get("target") == "0.0.0.0"), None)
        ifaces.append({"name": i.get("interface"), "up": i.get("up"), "proto": i.get("proto"),
                       "device": i.get("l3_device") or i.get("device"), "uptime": i.get("uptime"),
                       "ip": f"{addr.get('address')}/{addr.get('mask')}" if addr.get("address") else "",
                       "gateway": gw, "dns": i.get("dns-server", []), "metric": i.get("metric"),
                       "restartable": i.get("interface") in WAN_IFACES})

    mwan = {}
    for name, m in ((jload(sec.get("MWAN"), {}) or {}).get("interfaces") or {}).items():
        t = (m.get("track_ip") or [{}])[0]
        mwan[name] = {"status": m.get("status"), "online": m.get("online"), "score": m.get("score"),
                      "latency": t.get("latency"), "loss": t.get("packetloss")}

    ports = []
    devs = jload(sec.get("DEV"), {}) or {}
    for name in ROUTER.get("ports", ["wan", "lan1", "lan2", "lan3", "lan4", "br-lan"]):
        d = devs.get(name)
        if d:
            st = d.get("statistics", {})
            ports.append({"name": name, "up": d.get("up"), "carrier": d.get("carrier"), "speed": d.get("speed"),
                          "rx_bytes": st.get("rx_bytes"), "tx_bytes": st.get("tx_bytes"),
                          "rx_errors": st.get("rx_errors"), "tx_errors": st.get("tx_errors")})

    onts = [parse_ont(sec.get(f"ONT{i}"), o.get("name", f"ONT {i + 1}")) for i, o in enumerate(ONT_CACHES)]

    # Router DHCP pools: {interface: {start, limit, leasetime, ignore}}
    pools, by_sec = {}, {}
    for line in sec.get("DHCPCFG", "").splitlines():
        m = re.match(r"dhcp\.(\w+)\.(\w+)='?(.*?)'?$", line)
        if m:
            by_sec.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    for s in by_sec.values():
        if s.get("interface"):
            pools[s["interface"]] = s

    return {"system": parse_system(sec), "interfaces": ifaces, "mwan": mwan, "ports": ports, "dhcp_pools": pools,
            "leases": sorted(leases, key=lambda l: tuple(int(x) for x in l["ip"].split("."))),
            "wifi": parse_wifi(sec, names), "onts": onts, "_names": names}


def collect_ap():
    if not AP_HOST:
        return {"system": {}, "wifi": [], "note": "no access point configured"}
    router = cached("router", 15, collect_router)
    names = router.get("_names", {}) if isinstance(router, dict) else {}
    sec = sections(ssh(AP_HOST, AP_SH))
    return {"system": parse_system(sec), "wifi": parse_wifi(sec, names)}


# ---------------------------------------------------------------- Pi-hole
class PiHole:
    def __init__(self, name, url, password):
        self.name, self.url, self.password, self.sid = name, url, password, None
        self.lock = threading.Lock()

    def _auth(self):
        r = requests.post(f"{self.url}/api/auth", json={"password": self.password}, timeout=6)
        r.raise_for_status()
        self.sid = r.json()["session"]["sid"]

    def call(self, method, path, **kw):
        with self.lock:
            for _ in range(2):
                if not self.sid:
                    self._auth()
                r = requests.request(method, f"{self.url}{path}", headers={"sid": self.sid}, timeout=8, **kw)
                if r.status_code == 401:
                    self.sid = None
                    continue
                r.raise_for_status()
                return r.json()
            raise RuntimeError("Pi-hole auth failed")


# Each Pi-hole's app password lives in secrets.env under the name given by "password_env".
PIHOLES = [PiHole(p["name"], p["url"].rstrip("/"), E.get(p.get("password_env", ""), ""))
           for p in DNS.get("piholes", [])]


def collect_pihole_one(p):
    try:
        s = p.call("GET", "/api/stats/summary")
        b = p.call("GET", "/api/dns/blocking")
        top = p.call("GET", "/api/stats/top_domains", params={"blocked": "true", "count": 8})
        clients = p.call("GET", "/api/stats/top_clients", params={"count": 8})
        q = s.get("queries", {})
        return {"name": p.name, "ok": True, "blocking": b.get("blocking"), "timer": b.get("timer"),
                "queries": q.get("total"), "blocked": q.get("blocked"),
                "percent": round(q.get("percent_blocked") or 0, 1),
                "gravity": (s.get("gravity") or {}).get("domains_being_blocked"),
                "clients": (s.get("clients") or {}).get("active"),
                "top_blocked": top.get("domains", []), "top_clients": clients.get("clients", [])}
    except Exception as e:
        return {"name": p.name, "ok": False, "error": str(e)}


def collect_dns():
    if not PIHOLES:
        return {"vip": DNS_VIP, "vip_owner": "no Pi-holes configured", "nodes": []}
    with cf.ThreadPoolExecutor(len(PIHOLES)) as ex:
        nodes = list(ex.map(collect_pihole_one, PIHOLES))
    return {"vip": DNS_VIP, "vip_owner": vip_owner(), "nodes": nodes}


# ---------------------------------------------------------------- Omada
OMC = C.get("omada", {})


class Omada:
    def __init__(self):
        self.token, self.exp = None, 0
        self.url = OMC.get("url", "").rstrip("/")
        self.cid, self.site = OMC.get("omadac_id", ""), OMC.get("site_id", "")
        self.root = f"{self.url}/openapi/v1/{self.cid}/sites/{self.site}"
        self.lock = threading.Lock()

    def _tok(self):
        if not (self.url and self.cid and self.site):
            raise RuntimeError("omada.url / omadac_id / site_id are not set in config.json")
        with self.lock:
            if self.token and time.time() < self.exp - 60:
                return self.token
            r = requests.post(f"{self.url}/openapi/authorize/token", params={"grant_type": "client_credentials"},
                              json={"omadacId": self.cid, "client_id": E["OMADA_CLIENT_ID"],
                                    "client_secret": E["OMADA_CLIENT_SECRET"]}, verify=False, timeout=8)
            res = r.json()["result"]
            self.token, self.exp = res["accessToken"], time.time() + int(res.get("expiresIn", 3600))
            return self.token

    def call(self, method, path, **kw):
        r = requests.request(method, f"{self.root}{path}", headers={"Authorization": f"AccessToken={self._tok()}"},
                             verify=False, timeout=12, **kw)
        d = r.json()
        if d.get("errorCode") != 0:
            raise RuntimeError(f"Omada {d.get('errorCode')}: {d.get('msg')}")
        return d.get("result")


OMADA = Omada()


def port_details(lag_ids=()):
    """Per-port (and per-LAG) VLAN membership, which the overview endpoint doesn't include.
    LAG members carry the LAG's VLANs; their own port fields are stale."""
    plist = [{"unit": 1, "slot": 0, "port": i} for i in range(1, SWITCH_PORTS + 1)]
    res = OMADA.call("POST", "/switches/ports/select",
                     json={"selectAll": False, "switchList": [{"mac": SWITCH_MAC, "portList": plist, "lagList": list(lag_ids)}]})
    ports = {p["port"]: p for sw in (res or []) for p in sw.get("ports", [])}
    lags = {}
    for sw in res or []:
        for lag in sw.get("lags") or []:
            lags[lag.get("lagId")] = lag
            for member in lag.get("ports") or []:
                ports[member] = {**lag, "_lag": lag.get("lagId")}
    ports["_lags"] = lags
    return ports


def vlan_sets(rec):
    """(native, untagged incl. native, tagged) as read from a port/LAG record."""
    native = rec.get("nativeNetworkId")
    untag = list(rec.get("untagNetworkIds") or [])
    if native and native not in untag:
        untag.insert(0, native)
    return native, untag, [t for t in (rec.get("tagNetworkIds") or []) if t not in untag]


def vlan_body(native, untag, tag):
    # Omada rejects the native network inside the untagged/tagged lists on write (it adds it back on read).
    return {"nativeNetworkId": native, "networkTagsSetting": 2,
            "untagNetworkIds": [i for i in untag if i != native], "tagNetworkIds": [i for i in tag if i != native]}


NETMETA = Path("/var/lib/noc/networks.json")  # NOC-side DHCP plans per Omada network id


def load_meta():
    try:
        return json.loads(NETMETA.read_text())
    except Exception:
        return {}


def save_meta(meta):
    NETMETA.parent.mkdir(parents=True, exist_ok=True)
    tmp = NETMETA.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(NETMETA)


def describe_dhcp(vlan, meta):
    """Where a VLAN's DHCP comes from: NOC plan if recorded, else inferred read-only from the router."""
    if meta:
        d = meta
        if d["device"] == "openwrt":
            pool = f"{d['range_start']} – {d['range_end']}, lease {d['lease_h']} h, DNS {d['dns']}" if d.get("enabled") else "DHCP off"
            return {"device": "OpenWrt router", "detail": f"{d['gateway']}/{d['prefix']} · {pool}",
                    "status": "planned", "note": "router not configured (OpenWrt changes not enabled)"}
        if d["device"] == "upstream":
            return {"device": d.get("provider") or "upstream device", "detail": "", "status": "recorded", "note": ""}
        return {"device": "none (static IPs)", "detail": "", "status": "recorded", "note": ""}
    r = cached("router", 15, collect_router)
    if not isinstance(r, dict) or "interfaces" not in r:
        return {"device": "unknown", "detail": "", "status": "inferred", "note": "router data unavailable"}
    if vlan == 1:
        lan = next((i for i in r["interfaces"] if i["name"] == LAN_IFACE), {})
        pool = (r.get("dhcp_pools") or {}).get(LAN_IFACE, {})
        if lan.get("ip") and pool and pool.get("ignore") != "1":
            net = ipaddress.ip_interface(lan["ip"]).network
            start = int(pool.get("start", 100))
            first, last = net.network_address + start, net.network_address + start + int(pool.get("limit", 150)) - 1
            return {"device": "OpenWrt router", "detail": f"{lan['ip']} · {first} – {last}, lease {pool.get('leasetime', '12h')}",
                    "status": "live", "note": "router LAN"}
    pppoe = [i["name"] for i in r["interfaces"] if i.get("proto") == "pppoe"]
    if pppoe:
        # PPPoE sessions ride VLAN-tagged macvlans on the WAN port (wan.<vlan>macN) - read the config to match.
        raw = cached("pppoe_vlans", 300, lambda: ssh(ROUTER_HOST, "uci -q show network | grep -E \"\\.(device|ifname|vid)=\""))
        if isinstance(raw, str) and re.search(rf"\.vid='{vlan}'", raw) and re.search(rf"device='wan\.{vlan}(mac\d+)?'", raw):
            return {"device": "none (PPPoE)", "detail": f"ISP assigns addresses over PPPoE on wan.{vlan} ({', '.join(pppoe)})",
                    "status": "live", "note": ""}
    for i in r["interfaces"]:
        if i.get("device") == f"{TRUNK_DEV}.{vlan}":
            if i["proto"] == "dhcp":
                return {"device": "upstream device", "detail": f"router is a DHCP client here ({i['name']}: {i['ip'] or 'no lease'})",
                        "status": "live", "note": ""}
            if i["proto"] == "static":
                return {"device": "none (static)", "detail": f"router {i['name']}: {i['ip']}", "status": "live", "note": ""}
    return {"device": "not set", "detail": "no router interface on this VLAN", "status": "unknown", "note": ""}


def router_subnets():
    r = cached("router", 15, collect_router)
    out = []
    for i in (r.get("interfaces", []) if isinstance(r, dict) else []):
        if i.get("ip") and i.get("proto") in ("static", "dhcp"):
            try:
                out.append((i["name"], ipaddress.ip_interface(i["ip"]).network))
            except ValueError:
                pass
    return out


def clean_dhcp(d, nets_meta_excluding):
    """Validate the 'where is DHCP' answer. Returns the normalized plan dict."""
    dev = (d or {}).get("device", "none")
    if dev == "none":
        return {"device": "none"}
    if dev == "upstream":
        prov = (d.get("provider") or "").strip()
        if not 1 <= len(prov) <= 64 or not re.fullmatch(r"[\w .\-()/]+", prov):
            raise ValueError("say which device provides DHCP (1-64 chars)")
        return {"device": "upstream", "provider": prov}
    if dev in ("omada_gateway", "switch"):
        raise ValueError("that DHCP server device isn't available on this network")
    if dev != "openwrt":
        raise ValueError("unknown DHCP server device")
    try:
        gw = ipaddress.ip_interface(f"{d['gateway']}/{int(d['prefix'])}")
    except (KeyError, ValueError):
        raise ValueError("gateway/subnet must look like 10.0.20.1 / 24")
    net = gw.network
    if not 16 <= net.prefixlen <= 30 or not net.is_private:
        raise ValueError("use a private subnet between /16 and /30")
    if gw.ip in (net.network_address, net.broadcast_address):
        raise ValueError("gateway can't be the network or broadcast address")
    for name, other in router_subnets() + nets_meta_excluding:
        if net.overlaps(other):
            raise ValueError(f"{net} overlaps {other} ({name})")
    plan = {"device": "openwrt", "gateway": str(gw.ip), "prefix": net.prefixlen, "enabled": bool(d.get("enabled"))}
    if plan["enabled"]:
        try:
            a, b = ipaddress.ip_address(d["range_start"]), ipaddress.ip_address(d["range_end"])
        except (KeyError, ValueError):
            raise ValueError("DHCP range must be two IP addresses")
        hosts = (net.network_address, net.broadcast_address)
        if a not in net or b not in net or a in hosts or b in hosts or a > b:
            raise ValueError(f"DHCP range must be inside {net}, start ≤ end")
        if a <= gw.ip <= b:
            raise ValueError("DHCP range must not include the gateway address")
        lease = int(d.get("lease_h") or 12)
        if not 1 <= lease <= 168:
            raise ValueError("lease time 1-168 hours")
        dns_default = NEWNET.get("dns") or DNS_VIP
        if not (d.get("dns") or dns_default):
            raise ValueError("give a DNS server for the DHCP pool")
        dns = str(ipaddress.ip_address(d.get("dns") or dns_default))
        plan.update({"range_start": str(a), "range_end": str(b), "lease_h": lease, "dns": dns})
    return plan


def planned_subnets(meta, exclude_id=None):
    return [(f"planned {k}", ipaddress.ip_interface(f"{v['gateway']}/{v['prefix']}").network)
            for k, v in meta.items() if k != exclude_id and v.get("device") == "openwrt"]


def router_vlan_users():
    """VLAN id -> router interface names riding it over the trunk (e.g. 1015 -> ['wan_b'])."""
    r = cached("router", 15, collect_router)
    users = {}
    for i in r.get("interfaces", []) if isinstance(r, dict) else []:
        m = re.fullmatch(rf"{re.escape(TRUNK_DEV)}\.(\d+)", i.get("device") or "")
        if m:
            users.setdefault(int(m.group(1)), []).append(i["name"])
    return users


def collect_switch():
    nets = OMADA.call("GET", "/lan-networks", params={"page": 1, "pageSize": 100}).get("data", [])
    profiles = OMADA.call("GET", "/lan-profiles", params={"page": 1, "pageSize": 100}).get("data", [])
    rows = OMADA.call("GET", "/switches/ports/overview", params={"page": 1, "pageSize": 100}).get("data", [])
    detail = port_details({(r.get("lagSetting") or {}).get("lagId") for r in rows} - {None})
    devices = OMADA.call("GET", "/devices", params={"page": 1, "pageSize": 100})
    devices = devices.get("data", devices) if isinstance(devices, dict) else devices
    net_name = {n["id"]: f"{n['name']} (VLAN {n.get('vlan')})" for n in nets}
    prof_by_id = {p["id"]: p for p in profiles}
    speed = {0: "auto", 1: "10M", 2: "100M", 3: "1G", 4: "2.5G", 5: "10G"}
    lag_type = {1: "static", 2: "LACP", 3: "LACP active", 4: "LACP passive"}
    lags = []
    for lid, l in sorted(detail.pop("_lags", {}).items()):
        native, untag, tag = vlan_sets(l)
        members = [x for x in rows if x["port"] in (l.get("ports") or [])]
        lags.append({"lag": lid, "name": l.get("name") or f"LAG {lid}", "ports": l.get("ports") or [],
                     "type": lag_type.get(l.get("lagType"), l.get("lagType")), "disabled": l.get("disable"),
                     "members_up": sum(1 for x in members if (x.get("portStatus") or {}).get("linkStatus") == 1),
                     "native_id": native, "native": net_name.get(native, native or "none"),
                     "untag_ids": untag, "tag_ids": tag,
                     "untagged": [net_name.get(i, i) for i in untag], "tagged": [net_name.get(i, i) for i in tag],
                     "protected": "the only link between switch and router"})
    ports = []
    for r in rows:
        ps = r.get("portStatus", {})
        lag = r.get("lagSetting") or {}
        d = detail.get(r["port"], {})
        override = d.get("profileOverrideEnable", r.get("profileOverrideEnable"))
        # On this controller the port's own VLAN fields are what's in effect, even with profile
        # override off (verified against live traffic on an SG2210P with controller 6.2).
        src = d or prof_by_id.get(r.get("profileId"), {})
        native_id, untag, tag = vlan_sets(src)
        ports.append({"port": r["port"], "name": r.get("portName"), "switch": r.get("switchMac"),
                      "link": ps.get("linkStatus") == 1, "speed": speed.get(ps.get("linkSpeed"), ps.get("linkSpeed")),
                      "poe_w": ps.get("poePower"), "rx": ps.get("rx"), "tx": ps.get("tx"),
                      "profile_id": r.get("profileId"), "profile": r.get("profileName"),
                      "override": override,
                      "vlan_source": f"LAG {d['_lag']} settings" if d.get("_lag") else "port settings" if d
                      else f"profile '{r.get('profileName')}' (port detail unavailable)",
                      "native_id": native_id, "native": net_name.get(native_id, native_id or "none"),
                      "untag_ids": untag, "tag_ids": tag,
                      "untagged": [net_name.get(i, i) for i in untag], "tagged": [net_name.get(i, i) for i in tag],
                      "tags_setting": src.get("networkTagsSetting", r.get("networkTagsSetting")),
                      "disabled": r.get("disable"), "lag": lag.get("lagId"), "sfp": r.get("type") == 3,
                      "protected": PROTECTED_PORTS.get(r["port"])})
    def used_by(nid):
        out = [f"LAG {l['lag']}" + (" (native)" if l["native_id"] == nid else " (untagged)" if nid in l["untag_ids"] else " (tagged)")
               for l in lags if nid in l["untag_ids"] + l["tag_ids"]]
        out += [f"port {p['port']}" + (" (native)" if p["native_id"] == nid else " (untagged)" if nid in p["untag_ids"] else " (tagged)")
                for p in ports if not p["lag"] and nid in p["untag_ids"] + p["tag_ids"]]
        return out
    meta = load_meta()
    has_omada_gw = any(str(d.get("type", "")).lower() == "gateway" for d in (devices or []))
    return {"ports": ports, "lags": lags,
            "dhcp_devices": {"omada_gateway": has_omada_gw, "switch": False},
            "networks": [{"id": n["id"], "name": n["name"], "vlan": n.get("vlan"),
                          "kind": "VLAN only (switch)" if n.get("purpose") == 0 else "interface (built-in)",
                          "editable": n.get("purpose") == 0, "igmp": n.get("igmpSnoopEnable"),
                          "dhcp": describe_dhcp(n.get("vlan"), meta.get(n["id"])), "dhcp_plan": meta.get(n["id"]),
                          "used_by": used_by(n["id"])} for n in nets],
            "profiles": [{"id": p["id"], "name": p["name"],
                          "untagged": [net_name.get(i, i) for i in (p.get("untagNetworkIds") or [])],
                          "tagged": [net_name.get(i, i) for i in (p.get("tagNetworkIds") or [])],
                          "native": net_name.get(p.get("nativeNetworkId"), "none")} for p in profiles],
            "devices": [{"name": d.get("name"), "model": d.get("model"), "ip": d.get("ip"), "status": d.get("status"),
                         "firmware": d.get("firmwareVersion"), "uptime": d.get("uptime"), "cpu": d.get("cpuUtil"),
                         "mem": d.get("memUtil")} for d in (devices or [])]}


# ---------------------------------------------------------------- Proxmox
PVE = C.get("proxmox", {})


def pve(path):
    if not PVE.get("url"):
        raise RuntimeError("proxmox.url is not set in config.json")
    r = requests.get(f"{PVE['url'].rstrip('/')}/api2/json{path}",
                     headers={"Authorization": f"PVEAPIToken={E['PVE_TOKEN']}"}, verify=False, timeout=10)
    r.raise_for_status()
    return r.json()["data"]


def collect_proxmox():
    node = PVE.get("node")
    if not node:
        raise RuntimeError("proxmox.node is not set in config.json")
    st = pve(f"/nodes/{node}/status")
    guests = [{"vmid": g["vmid"], "name": g.get("name"), "type": g["type"], "status": g["status"],
               "cpu_pct": round(100 * g.get("cpu", 0), 1), "mem_pct": round(100 * g["mem"] / g["maxmem"], 1) if g.get("maxmem") else None,
               "disk_pct": round(100 * g["disk"] / g["maxdisk"], 1) if g.get("maxdisk") and g["type"] == "lxc" else None,
               "uptime": g.get("uptime")} for g in pve("/cluster/resources?type=vm")]
    storage = [{"name": s["storage"], "type": s.get("type"), "used_pct": round(100 * s["used"] / s["total"], 1) if s.get("total") else None,
                "total_gb": round(s.get("total", 0) / 1e9)} for s in pve(f"/nodes/{node}/storage") if s.get("active")]
    return {"node": node, "cpu_pct": round(100 * st["cpu"], 1), "mem_pct": round(100 * st["memory"]["used"] / st["memory"]["total"], 1),
            "uptime": st.get("uptime"), "version": st.get("pveversion"), "guests": sorted(guests, key=lambda g: g["vmid"]),
            "storage": storage}


# ---------------------------------------------------------------- actions (preview -> confirm)
PENDING: dict = {}


def plan_vlans(cur, v, nets):
    """Validate a requested VLAN set against current state; return (native, untag, tag, human diff)."""
    native = v.get("native_id")
    untag, tag = list(dict.fromkeys(v.get("untag_ids") or [])), list(dict.fromkeys(v.get("tag_ids") or []))
    for i in [native, *untag, *tag]:
        if i and i not in nets:
            raise ValueError(f"unknown network {i}")
    if not native:
        raise ValueError("pick a native (untagged) VLAN")
    if native not in untag:
        untag.insert(0, native)
    tag = [t for t in tag if t != native]
    if set(untag) & set(tag):
        raise ValueError("a VLAN can't be both tagged and untagged")
    label = lambda i: f"{nets[i]['name']} (VLAN {nets[i]['vlan']})" if i in nets else i
    detail = []
    if native != cur["native_id"]:
        detail.append(f"Native VLAN: {cur['native']} -> {label(native)}")
    for name, old, new in (("Untagged", cur["untag_ids"], untag), ("Tagged", cur["tag_ids"], tag)):
        add, rem = [i for i in new if i not in old], [i for i in old if i not in new]
        if add:
            detail.append(f"{name} + " + ", ".join(map(label, add)))
        if rem:
            detail.append(f"{name} − " + ", ".join(map(label, rem)))
    if not detail:
        raise ValueError("no VLAN changes")
    detail.append("Result → untagged: " + ", ".join(map(label, untag)) + " | tagged: " + (", ".join(map(label, tag)) or "none"))
    return native, untag, tag, detail


def verify_vlans(read, native, untag, tag, what):
    """Read the port/LAG back from the switch and prove it matches what was previewed."""
    time.sleep(2)
    got_native, got_untag, got_tag = vlan_sets(read() or {})
    if (got_native, set(got_untag), set(got_tag)) != (native, set(untag), set(tag)):
        raise RuntimeError(f"applied, but the switch reports native={got_native} untagged={sorted(got_untag)} "
                           f"tagged={sorted(got_tag)} - check {what} in Omada")
    return {"verified": True}


def plan_action(kind, p):
    """Validate an action and describe exactly what it will do. Returns (summary, detail, confirm_word, fn)."""
    if kind == "dns_blocking":
        enable = bool(p.get("enable"))
        minutes = int(p.get("minutes") or 0)
        if not enable and not 0 <= minutes <= 240:
            raise ValueError("minutes must be 0-240")
        body = {"blocking": enable, "timer": None if enable or minutes == 0 else minutes * 60}
        what = "Resume ad-blocking" if enable else (f"Pause ad-blocking for {minutes} min" if minutes else "Disable ad-blocking (no timer)")

        def run():
            return {p.name: p.call("POST", "/api/dns/blocking", json=body).get("blocking") for p in PIHOLES}
        if not PIHOLES:
            raise ValueError("no Pi-holes configured")
        return (what + (" on all Pi-holes" if len(PIHOLES) > 1 else ""),
                [f"{p.name} ({p.url})" for p in PIHOLES], None, run, ["dns"])

    if kind == "switch_port":
        sw = cached("switch", 20, collect_switch)
        port = int(p["port"])
        cur = next((x for x in sw["ports"] if x["port"] == port), None)
        if not cur:
            raise ValueError("unknown port")
        if cur["lag"]:
            raise ValueError(f"Port {port} is a member of LAG {cur['lag']} - edit the LAG's VLANs instead (LAGs card)")
        nets = {n["id"]: n for n in sw["networks"]}
        profs = {x["id"]: x for x in sw["profiles"]}
        detail, path, body = [], None, None
        if p.get("profile_id"):
            if p["profile_id"] not in profs:
                raise ValueError("unknown profile")
            detail.append(f"Profile: {cur['profile']} -> {profs[p['profile_id']]['name']}")
            path, body, method = f"/switches/{SWITCH_MAC}/ports/{port}/profile", {"profileId": p["profile_id"]}, "PUT"
        elif "vlans" in p:
            native, untag, tag, vd = plan_vlans(cur, p["vlans"], nets)
            detail += vd
            path, method = f"/switches/{SWITCH_MAC}/ports/{port}", "PATCH"
            body = vlan_body(native, untag, tag)  # only VLAN fields; profile/override left untouched
        elif "disable" in p:
            detail.append(f"Port state: {'disabled' if cur['disabled'] else 'enabled'} -> {'disabled' if p['disable'] else 'enabled'}")
            path, method, body = f"/switches/{SWITCH_MAC}/ports/{port}/status", "PUT", {"status": 0 if p["disable"] else 1}
        else:
            raise ValueError("nothing to change")
        word = f"port {port}" if cur["protected"] else None
        if cur["protected"]:
            detail.append(f"WARNING: port {port} is {cur['protected']} - this can cut that device off")

        def run():
            res = OMADA.call(method, path, json=body)
            if "vlans" in p:
                return verify_vlans(lambda: port_details().get(port, {}), native, untag, tag, f"port {port}")
            return res
        return f"Switch port {port} ({cur['name']})", detail, word, run, ["switch"]

    if kind == "switch_lag":
        sw = cached("switch", 20, collect_switch)
        lid = int(p["lag"])
        cur = next((x for x in sw.get("lags", []) if x["lag"] == lid), None)
        if not cur:
            raise ValueError("unknown LAG")
        nets = {n["id"]: n for n in sw["networks"]}
        v = p.get("vlans") or {}
        if v.get("native_id") and v["native_id"] != cur["native_id"]:
            raise ValueError(f"Changing LAG {lid}'s native VLAN is blocked here: it carries the switch's own "
                             "management, so the switch would drop off Omada and you couldn't undo it remotely. "
                             "Do it in Omada with someone next to the switch.")
        native, untag, tag, detail = plan_vlans(cur, {**v, "native_id": cur["native_id"]}, nets)
        # Name every router connection a removal would cut, so the preview says what actually breaks.
        users = router_vlan_users()
        for i in [i for i in cur["untag_ids"] + cur["tag_ids"] if i not in untag + tag]:
            vid = nets.get(i, {}).get("vlan")
            if users.get(vid):
                detail.append(f"⚠ Removing VLAN {vid} cuts router interface(s): {', '.join(users[vid])}")
        detail.append(f"WARNING: LAG {lid} (ports {'+'.join(map(str, cur['ports']))}) is {cur['protected']}")

        def run():
            OMADA.call("PATCH", f"/switches/{SWITCH_MAC}/lags/{lid}", json=vlan_body(native, untag, tag))
            return verify_vlans(lambda: port_details([lid])["_lags"].get(lid, {}), native, untag, tag, f"LAG {lid}")
        return f"LAG {lid} VLANs", detail, f"lag {lid}", run, ["switch"]

    if kind in ("net_create", "net_edit", "net_delete"):
        sw = cached("switch", 20, collect_switch)
        nets = {n["id"]: n for n in sw["networks"]}
        by_vlan = {n["vlan"]: n for n in sw["networks"]}
        users = router_vlan_users()

        def clean_name(s):
            s = (s or "").strip()
            if not 1 <= len(s) <= 128 or not re.fullmatch(r"[\w .\-]+", s):
                raise ValueError("name: 1-128 letters, digits, space, _ . -")
            return s

        def clean_vlan(v):
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError("VLAN ID must be a number")
            if not 2 <= v <= 4090:
                raise ValueError("VLAN ID must be 2-4090 (1 is Default)")
            return v

        meta = load_meta()

        def dhcp_lines(plan):
            if plan["device"] == "openwrt":
                lines = [f"DHCP server device: OpenWrt router · gateway {plan['gateway']}/{plan['prefix']}"]
                lines.append(f"DHCP pool {plan['range_start']} – {plan['range_end']}, lease {plan['lease_h']} h, DNS {plan['dns']}"
                             if plan["enabled"] else "DHCP server: off (static addresses on this subnet)")
                lines.append("Recorded as a plan only: the router is NOT changed (OpenWrt changes not enabled)")
                return lines
            if plan["device"] == "upstream":
                return [f"DHCP server device: {plan['provider']} (recorded)"]
            return ["DHCP server device: none (static IPs only)"]

        if kind == "net_create":
            name, vlan = clean_name(p.get("name")), clean_vlan(p.get("vlan"))
            if vlan in by_vlan:
                raise ValueError(f"VLAN {vlan} already exists as '{by_vlan[vlan]['name']}'")
            if name.lower() in {n["name"].lower() for n in sw["networks"]}:
                raise ValueError(f"a network named '{name}' already exists")
            plan = clean_dhcp(p.get("dhcp"), planned_subnets(meta))
            igmp = bool(p.get("igmp"))
            # Mirror how the existing switch-only VLANs are defined so the controller treats it the same way.
            template = OMADA.call("GET", f"/lan-networks/{next(n['id'] for n in sw['networks'] if n['editable'])}") \
                if any(n["editable"] for n in sw["networks"]) else {}
            body = {"name": name, "purpose": 0, "vlan": vlan, "application": 1, "igmpSnoopEnable": igmp,
                    "mldSnoopEnable": False, "dhcpL2RelayEnable": False, "allLan": True,
                    "interfaceIds": template.get("interfaceIds", [])}
            detail = [f"Omada: new switch VLAN '{name}' with ID {vlan}" + (" (IGMP snooping on)" if igmp else ""),
                      *dhcp_lines(plan),
                      "Not on any port yet: add it with Edit VLANs on the ports/LAG that should carry it."]

            def run():
                OMADA.call("POST", "/lan-networks", json=body)
                time.sleep(1)
                got = [n for n in OMADA.call("GET", "/lan-networks", params={"page": 1, "pageSize": 100}).get("data", [])
                       if n.get("vlan") == vlan and n.get("name") == name]
                if not got:
                    raise RuntimeError("controller accepted the request but the network isn't listed - check Omada")
                m = load_meta()
                m[got[0]["id"]] = plan
                save_meta(m)
                return {"verified": True, "id": got[0]["id"]}
            return f"Create network {name} (VLAN {vlan})", detail, None, run, ["switch"]

        cur = nets.get(p.get("id"))
        if not cur:
            raise ValueError("unknown network")
        if not cur["editable"]:
            raise ValueError(f"'{cur['name']}' is Omada's built-in network (the switch's management VLAN) - read-only here")

        if kind == "net_delete":
            if cur["used_by"]:
                raise ValueError(f"'{cur['name']}' is still carried by {', '.join(cur['used_by'])} - remove it there first")
            detail = [f"Delete network '{cur['name']}' (VLAN {cur['vlan']})", "No port or LAG carries it."]
            if users.get(cur["vlan"]):
                detail.append(f"Router interface(s) still configured on {TRUNK_DEV}.{cur['vlan']}: {', '.join(users[cur['vlan']])}")

            def run():
                OMADA.call("DELETE", f"/lan-networks/{cur['id']}")
                time.sleep(1)
                if any(n.get("id") == cur["id"] for n in OMADA.call("GET", "/lan-networks", params={"page": 1, "pageSize": 100}).get("data", [])):
                    raise RuntimeError("controller still lists the network - check Omada")
                m = load_meta()
                if m.pop(cur["id"], None) is not None:
                    save_meta(m)
                return {"verified": True}
            return f"Delete network {cur['name']}", detail, f"vlan {cur['vlan']}", run, ["switch"]

        # net_edit
        name = clean_name(p.get("name") or cur["name"])
        vlan = clean_vlan(p.get("vlan") or cur["vlan"])
        igmp = bool(p.get("igmp")) if "igmp" in p else bool(cur["igmp"])
        if vlan != cur["vlan"] and vlan in by_vlan:
            raise ValueError(f"VLAN {vlan} already exists as '{by_vlan[vlan]['name']}'")
        if name != cur["name"] and name.lower() in {n["name"].lower() for n in sw["networks"]}:
            raise ValueError(f"a network named '{name}' already exists")
        detail, word = [], None
        if name != cur["name"]:
            detail.append(f"Name: {cur['name']} -> {name}")
        if igmp != bool(cur["igmp"]):
            detail.append(f"IGMP snooping: {'on' if cur['igmp'] else 'off'} -> {'on' if igmp else 'off'}")
        if vlan != cur["vlan"]:
            detail.append(f"VLAN ID: {cur['vlan']} -> {vlan}")
            if cur["used_by"]:
                detail.append("⚠ Every port/LAG carrying it switches to the new ID: " + ", ".join(cur["used_by"]))
                word = f"vlan {cur['vlan']}"
            if users.get(cur["vlan"]):
                detail.append(f"⚠ Router still expects VLAN {cur['vlan']} on {TRUNK_DEV}.{cur['vlan']} ({', '.join(users[cur['vlan']])}) "
                              f"- that connection breaks until the router is changed to {TRUNK_DEV}.{vlan}")
                word = f"vlan {cur['vlan']}"
        omada_changed = bool(detail)  # so far detail only holds Omada-side changes
        new_plan = clean_dhcp(p["dhcp"], planned_subnets(meta, cur["id"])) if "dhcp" in p else meta.get(cur["id"])
        dhcp_changed = "dhcp" in p and new_plan != meta.get(cur["id"])
        if dhcp_changed:
            detail += dhcp_lines(new_plan)
        if not detail:
            raise ValueError("nothing changed")
        if not omada_changed:  # only the NOC-side DHCP plan changed: no Omada write needed
            def run_meta():
                m = load_meta()
                m[cur["id"]] = new_plan
                save_meta(m)
                return {"saved": True}
            return f"Edit network {cur['name']}", detail, word, run_meta, ["switch"]
        full = OMADA.call("GET", f"/lan-networks/{cur['id']}")
        allowed = ("name purpose isolation interfaceIds vlanType vlans vlan application gatewaySubnet dhcpSettingsVO domain "
                   "igmpSnoopEnable mldSnoopEnable dhcpL2RelayEnable dhcpGuard dhcpv6Guard lanNeworkIpv6Config allLan").split()
        body = {k: v for k, v in full.items() if k in allowed}
        body.update({"name": name, "vlan": vlan, "igmpSnoopEnable": igmp})

        def run():
            OMADA.call("PATCH", f"/lan-networks/{cur['id']}", json=body)
            time.sleep(1)
            got = OMADA.call("GET", f"/lan-networks/{cur['id']}")
            if (got.get("name"), got.get("vlan"), bool(got.get("igmpSnoopEnable"))) != (name, vlan, igmp):
                raise RuntimeError(f"controller reports name={got.get('name')} vlan={got.get('vlan')} - check Omada")
            if dhcp_changed:
                m = load_meta()
                m[cur["id"]] = new_plan
                save_meta(m)
            return {"verified": True}
        return f"Edit network {cur['name']}", detail, word, run, ["switch"]

    if kind == "wan_restart":
        iface = p.get("iface")
        if iface not in WAN_IFACES:
            raise ValueError("not a restartable WAN")

        def run():
            ssh(ROUTER_HOST, f"ifup {iface}")
            return "restarted"
        return f"Restart WAN interface {iface}", [f"Router runs: ifup {iface}", "That connection drops for ~5-30 s; mwan3 moves traffic to the others"], None, run, ["router"]

    raise ValueError("unknown action")


# ---------------------------------------------------------------- app + auth
app = FastAPI(title="NOC", docs_url=None, redoc_url=None)
SESSION_KEY = E["NOC_SESSION_KEY"].encode()
SESSION_HOURS = 12


def make_session(user):
    exp = int(time.time()) + SESSION_HOURS * 3600
    sig = hmac.new(SESSION_KEY, f"{user}:{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{user}:{exp}:{sig}"


def valid_session(tok):
    try:
        user, exp, sig = tok.split(":")
        good = hmac.new(SESSION_KEY, f"{user}:{exp}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except Exception:
        return False


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path in ("/login", "/healthz") or valid_session(request.cookies.get("noc", "")):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "login required"}, status_code=401)
    return RedirectResponse("/login")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/meta")
def api_meta():
    """Non-secret UI settings from config.json."""
    return {"site_name": SITE_NAME, "dns_vip": DNS_VIP,
            "new_network": {"subnet_template": NEWNET.get("subnet_template", "10.0.{n}.1/24"),
                            "range_start": NEWNET.get("range_start", 100), "range_end": NEWNET.get("range_end", 200),
                            "lease_h": NEWNET.get("lease_h", 12), "dns": NEWNET.get("dns") or DNS_VIP or ""}}


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return (BASE / "static" / "login.html").read_text()


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    ok = hmac.compare_digest(str(form.get("user", "")), E["NOC_ADMIN_USER"]) and \
        hmac.compare_digest(str(form.get("password", "")), E["NOC_ADMIN_PASS"])
    if not ok:
        time.sleep(1)
        return RedirectResponse("/login?bad=1", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("noc", make_session(E["NOC_ADMIN_USER"]), httponly=True, samesite="strict",
                    max_age=SESSION_HOURS * 3600, secure=request.headers.get("x-forwarded-proto") == "https")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login")
    resp.delete_cookie("noc")
    return resp


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/overview")
def api_overview():
    with cf.ThreadPoolExecutor(5) as ex:
        f = {k: ex.submit(cached, k, ttl, fn) for k, ttl, fn in [
            ("ping", 10, collect_ping), ("router", 15, collect_router), ("dns", 15, collect_dns),
            ("proxmox", 20, collect_proxmox), ("switch", 20, collect_switch)]}
        r = {k: v.result() for k, v in f.items()}
    router = r["router"] if "error" not in r["router"] else {}
    return {"devices": r["ping"],
            "wans": [{"name": i["name"], "up": i["up"], "ip": i["ip"], "uptime": i["uptime"],
                      **(router.get("mwan", {}).get(i["name"], {}))}
                     for i in router.get("interfaces", []) if i["name"] in WAN_IFACES],
            "onts": router.get("onts", []), "router_system": router.get("system"),
            "dns": r["dns"], "proxmox": r["proxmox"],
            "switch_ports_up": sum(1 for p in r["switch"].get("ports", []) if p["link"]) if "ports" in r["switch"] else None,
            "errors": {k: v["error"] for k, v in r.items() if isinstance(v, dict) and "error" in v}}


@app.get("/api/router")
def api_router():
    d = cached("router", 15, collect_router)
    return {k: v for k, v in d.items() if k != "_names"}


@app.get("/api/ap")
def api_ap():
    return cached("ap", 15, collect_ap)


@app.get("/api/dns")
def api_dns():
    return cached("dns", 10, collect_dns)


@app.get("/api/switch")
def api_switch():
    return cached("switch", 20, collect_switch)


@app.get("/api/proxmox")
def api_proxmox():
    return cached("proxmox", 20, collect_proxmox)


@app.get("/api/audit")
def api_audit():
    if not AUDIT.exists():
        return []
    return [json.loads(l) for l in AUDIT.read_text().splitlines()[-100:]][::-1]


@app.post("/api/actions/preview")
async def preview(request: Request):
    body = await request.json()
    try:
        summary, detail, word, fn, refresh = plan_action(body.get("kind"), body.get("params") or {})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    now = time.time()
    for k in [k for k, v in PENDING.items() if v["exp"] < now]:
        PENDING.pop(k)
    aid = secrets.token_urlsafe(12)
    PENDING[aid] = {"kind": body.get("kind"), "summary": summary, "detail": detail, "word": word,
                    "fn": fn, "refresh": refresh, "exp": now + 120}
    return {"id": aid, "summary": summary, "detail": detail, "confirm_word": word, "expires_in": 120}


@app.post("/api/actions/confirm")
async def confirm(request: Request):
    body = await request.json()
    a = PENDING.pop(body.get("id", ""), None)
    if not a or a["exp"] < time.time():
        return JSONResponse({"error": "preview expired - preview again"}, status_code=400)
    if a["word"] and (body.get("confirm_text") or "").strip().lower() != a["word"]:
        audit(request, a["kind"], a["summary"], "rejected: confirmation text mismatch")
        return JSONResponse({"error": f"type '{a['word']}' to confirm"}, status_code=400)
    try:
        result = a["fn"]()
        invalidate(*a["refresh"])
        audit(request, a["kind"], a["summary"] + " | " + "; ".join(a["detail"]), {"ok": True, "result": result})
        return {"ok": True, "result": result}
    except Exception as e:
        audit(request, a["kind"], a["summary"], {"ok": False, "error": str(e)})
        return JSONResponse({"error": str(e)}, status_code=500)
