"""NOC command center: one place to see and control the home network.

Reads: OpenWrt router + AP (SSH/ubus), Pi-hole x2 (API), Omada switch (Open API),
Proxmox (read-only token), ping checks. Writes always go through preview -> confirm
and are appended to an audit log.
"""
import asyncio
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
import sys
import threading
import time
from pathlib import Path

import requests
import urllib3
from fastapi import FastAPI, Request

import auth
import notify
import openwrt
import settings
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

urllib3.disable_warnings()

BASE = Path(__file__).parent
ETC = Path(os.environ.get("NOC_ETC", "/etc/noc"))
VAR = Path(os.environ.get("NOC_VAR", "/var/lib/noc"))
AUDIT = VAR / "audit.jsonl"
KEY = str(ETC / "id_ed25519")
KNOWN_HOSTS = str(ETC / "known_hosts")
SSH_MUX = Path(os.environ.get("NOC_SSH_MUX", "/run/noc-ssh"))  # reused SSH connections (ControlMaster)


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

# Device health checks live in C["devices"] and are editable from the Devices tab.
# Everything else is editable from the Settings tab; derive_settings() and connect_services()
# turn C into the values below and are re-run after every Settings change.


def derive_settings():
    global SITE_NAME, DNS, DNS_VIP, DNS_VIP6, VIP_MACS, ROUTER, ROUTER_HOST, AP_HOST, LAN_IFACE, TRUNK_DEV
    global ONT_CACHES, SW, SWITCH_MAC, SWITCH_PORTS, PROTECTED_PORTS, NEWNET
    SITE_NAME = C.get("site_name", "Home NOC")
    DNS = C.setdefault("dns", {})
    DNS_VIP = DNS.get("vip")
    DNS_VIP6 = DNS.get("vip6")  # optional IPv6 DNS VIP (keepalived, same instance as the IPv4 one)
    VIP_MACS = {p["mac"].lower(): p["name"] for p in DNS.get("piholes", []) if p.get("mac")}
    ROUTER = C.setdefault("router", {})
    ROUTER_HOST = ROUTER.get("host")
    AP_HOST = (C.get("access_point") or {}).get("host")
    LAN_IFACE = ROUTER.get("lan_interface", "lan")
    TRUNK_DEV = ROUTER.get("trunk_device", "bond")  # VLANs arrive as <trunk>.<vid>, e.g. bond.123
    # DDM caches written on the router by luci-app-vsol-ddm / luci-app-hsgq-ddm.
    ONT_CACHES = ROUTER.get("ont_caches", [])
    SW = C.setdefault("switch", {})
    SWITCH_MAC = SW.get("mac", "")
    SWITCH_PORTS = int(SW.get("ports", 10))
    PROTECTED_PORTS = {int(k): v for k, v in (SW.get("protected_ports") or {}).items()}
    NEWNET = C.get("new_network_defaults", {})


derive_settings()
VIEW_STALE = 300  # page views may show data up to 5 min old while it refreshes in the background


def wan_groups():
    """{group name: [interfaces]}, e.g. {"isp-a": ["wan1", ...]}. Editable from the Settings tab."""
    return ROUTER.get("wan_groups") or {}


def wan_ifaces():
    """Monitored, restartable WANs: router.wan_interfaces plus every member of a WAN group."""
    s = set(ROUTER.get("wan_interfaces", []))
    for members in wan_groups().values():
        s |= set(members)
    return s


def wan_group_of(iface):
    return next((g for g, m in wan_groups().items() if iface in m), None)


def wan_candidates():
    """Router interfaces that may be put in a WAN group: everything except LAN and loopback.
    Falls back to the configured WANs when the router can't be reached."""
    r = cached("router", 15, collect_router)
    names = [i["name"] for i in r.get("interfaces", [])] if isinstance(r, dict) else []
    return [n for n in names if n not in (LAN_IFACE, "loopback")] or sorted(wan_ifaces())


# ---------------------------------------------------------------- helpers
_cache: dict = {}
_cache_lock = threading.Lock()


_refreshing: set = set()


def _store(key, fn):
    try:
        val = fn()
    except Exception as e:  # surface collector failures to the UI instead of crashing
        val = {"error": f"{type(e).__name__}: {e}"}
    with _cache_lock:
        _cache[key] = (time.time(), val)
    return val


def cached(key, ttl, fn, stale=0):
    """Value of fn(), reused for `ttl` seconds.

    With `stale` > 0 (page views only), an expired but recent good value is returned at once
    and refreshed in the background, so tabs open instantly. The watcher and every action
    use the default (stale=0) and always get data no older than `ttl`."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        usable = hit and now - hit[0] < stale and not (isinstance(hit[1], dict) and "error" in hit[1])
        if usable:
            if key not in _refreshing:
                _refreshing.add(key)

                def refresh():
                    try:
                        _store(key, fn)
                    finally:
                        _refreshing.discard(key)
                threading.Thread(target=refresh, daemon=True).start()
            return hit[1]
    return _store(key, fn)


def invalidate(*keys):
    with _cache_lock:
        for k in keys:
            _cache.pop(k, None)


def ssh(host, script, timeout=20):
    """Run a shell script on a host. One SSH connection per host is kept open for 5 minutes
    and reused (about 6 ms per call instead of a 130 ms handshake, and no login per call in
    the host's log). If the shared connection can't be used, ssh falls back to a normal one."""
    try:
        SSH_MUX.mkdir(mode=0o700, parents=True, exist_ok=True)
        mux = ["-o", "ControlMaster=auto", "-o", f"ControlPath={SSH_MUX}/%C", "-o", "ControlPersist=300",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]
    except OSError:
        mux = []
    r = subprocess.run(
        ["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "StrictHostKeyChecking=yes", *mux,
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
    audit_entry(request.client.host if request.client else "",
                request.headers.get("cf-access-authenticated-user-email", ""), action, summary, result)


LOG_MAX = 2_000_000  # bytes; a log above this moves to <name>.1 (one old file is kept)
_log_lock = threading.Lock()


def append_jsonl(path, entries):
    with _log_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > LOG_MAX:
                path.replace(path.with_name(path.name + ".1"))
        except FileNotFoundError:
            pass
        with path.open("a") as f:
            f.writelines(json.dumps(e) + "\n" for e in entries)


def tail_jsonl(path, n):
    """Last n entries, newest first, reading into <name>.1 if the current file is short."""
    lines = []
    for p in (path, path.with_name(path.name + ".1")):
        if len(lines) >= n:
            break
        try:
            lines = p.read_text().splitlines() + lines
        except FileNotFoundError:
            pass
    out = []
    for line in lines[-n:][::-1]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def audit_entry(ip, via, action, summary, result):
    append_jsonl(AUDIT, [{"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "ip": ip, "via": via,
                          "action": action, "summary": summary, "result": result}])


# ---------------------------------------------------------------- ping / VIP
def split_target(t):
    """Check target -> (host, port or None). "192.0.2.5", "192.0.2.5:443", "2001:db8::5" and
    "[2001:db8::5]:443" are all valid: an IPv6 address needs brackets only when a port follows."""
    if t.startswith("["):
        host, _, rest = t[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else None
    if t.count(":") == 1:
        host, port = t.split(":")
        return host, int(port)
    return t, None


def ping(ip):
    host, port = split_target(ip)
    if port:  # TCP connect check
        t = time.time()
        try:
            socket.create_connection((host, port), timeout=2).close()
            return {"up": True, "ms": round((time.time() - t) * 1000, 1)}
        except OSError:
            return {"up": False, "ms": None}
    r = subprocess.run(["ping", "-c", "1", "-W", "1", host], capture_output=True, text=True)
    m = re.search(r"time=([\d.]+)", r.stdout)
    return {"up": r.returncode == 0, "ms": float(m.group(1)) if m else None}


DEVICE_GROUPS = ["core", "isp", "dns", "server", "iot", "client", "other"]


CONFIG_LOCK = threading.Lock()


def set_secrets(updates):
    """Write KEY=VALUE pairs into secrets.env (mode 600), keeping every other line as it is."""
    for k, v in updates.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", k) or "\n" in v or "\r" in v:
            raise ValueError(f"invalid secret {k}")
    with CONFIG_LOCK:
        path = ETC / "secrets.env"
        lines = path.read_text().splitlines() if path.exists() else []
        out, done = [], set()
        for line in lines:
            k = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
            if k in updates:
                out.append(f"{k}={updates[k]}")
                done.add(k)
            else:
                out.append(line)
        out += [f"{k}={v}" for k, v in updates.items() if k not in done]
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        E.update(updates)


def save_config():
    """Persist C to config.json atomically, keeping the previous version as config.json.bak."""
    with CONFIG_LOCK:
        _save_config()


def _save_config():
    path = ETC / "config.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(C, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    if path.exists():
        path.replace(ETC / "config.json.bak")
    tmp.replace(path)


def neighbour_ips():
    """MAC -> current IPv4, from the router's DHCP leases first, then its ARP table."""
    r = cached("router", 15, collect_router)
    if not isinstance(r, dict) or "leases" not in r:
        return {}
    ips = dict(r.get("_arp", {}))
    ips.update({l["mac"]: l["ip"] for l in r["leases"]})
    return ips


def resolve_device(d, macs):
    """Config entry -> (check target or None, current IP). A "mac" entry follows the device
    to whatever IP it has now; an "ip"/"target" entry is checked as written."""
    if d.get("mac"):
        ip = macs.get(d["mac"].lower())
        if not ip:
            return None, None
        return (f"{ip}:{d['port']}" if d.get("port") else ip), ip
    t = d.get("target") or d.get("ip", "")
    return t, split_target(t)[0] if t else t


def collect_ping():
    devs = C.get("devices", [])
    macs = neighbour_ips() if any(d.get("mac") for d in devs) else {}
    targets = [resolve_device(d, macs) for d in devs]
    todo = sorted({t for t, _ in targets if t})
    with cf.ThreadPoolExecutor(16) as ex:
        res = dict(zip(todo, ex.map(ping, todo)))
    out = []
    for d, (t, ip) in zip(devs, targets):
        st = res.get(t, {"up": False, "ms": None})
        out.append({"name": d["name"], "ip": t or "not seen", "current_ip": ip, "group": d.get("group", "other"),
                    "mac": d.get("mac"), "tracking": "mac" if d.get("mac") else "ip", **st,
                    "note": "MAC not seen in router DHCP/ARP" if d.get("mac") and not t else ""})
    return out


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


def vip6_owner():
    """Which Pi-hole answers neighbour discovery for the IPv6 DNS VIP (None if none configured)."""
    if not DNS_VIP6:
        return None
    subprocess.run(["ping", "-c", "1", "-W", "1", DNS_VIP6], capture_output=True)
    r = subprocess.run(["ip", "-6", "neigh", "show", DNS_VIP6], capture_output=True, text=True)
    m = re.search(r"lladdr ([0-9a-fA-F:]{17})", r.stdout)
    return VIP_MACS.get(m.group(1).lower(), m.group(1)) if m else "unknown"


# ---------------------------------------------------------------- OpenWrt
# Each marker is preceded by a newline: some sources (DDM caches) don't end with one.
WIFI_SH = r"""
for i in $(ubus list 'hostapd.*' 2>/dev/null | cut -d. -f2); do
  printf '\n@@WIFIINFO %s\n' "$i"; ubus call iwinfo info "{\"device\":\"$i\"}"
  printf '\n@@WIFIASSOC %s\n' "$i"; ubus call iwinfo assoclist "{\"device\":\"$i\"}"
done
"""
ROUTER_SH_BASE = r"""
printf '\n@@IF\n'; ubus call network.interface dump
printf '\n@@MWAN\n'; ubus call mwan3 status 2>/dev/null || echo '{}'
printf '\n@@SYS\n'; ubus call system info
printf '\n@@BOARD\n'; ubus call system board
printf '\n@@LEASES\n'; cat /tmp/dhcp.leases
printf '\n@@ARP\n'; cat /proc/net/arp
printf '\n@@DHCPCFG\n'; uci -q show dhcp | grep -E "^dhcp\.[A-Za-z0-9_]+\.(interface|start|limit|leasetime|ignore|dhcpv4)="
"""


DEFAULT_PORTS = ["wan", "lan1", "lan2", "lan3", "lan4", "br-lan"]


def router_ports():
    return [p for p in ROUTER.get("ports", DEFAULT_PORTS) if re.fullmatch(r"[A-Za-z0-9_.@-]{1,15}", str(p))]


def ports_sh():
    """Status of only the ports the NOC shows, as one JSON object. `ubus call network.device status`
    without a name asks every device for its link modes: seconds with many VLANs and macvlans."""
    one = """printf '"%s":' {n}; ubus call network.device status '{{"name":"{n}"}}' 2>/dev/null || echo '{{}}'; printf ','\n"""
    return "printf '\\n@@DEV\\n{'\n" + "".join(one.format(n=n) for n in router_ports()) + """printf '"":{}}\\n'\n"""


def router_sh():
    return ROUTER_SH_BASE + ports_sh() + "".join(f"printf '\\n@@ONT{i}\\n'; cat {shlex.quote(o['path'])} 2>/dev/null || echo '{{}}'\n"
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


def parse_arp(raw):
    """/proc/net/arp -> {mac: ip} for complete entries (flags 0x2) on any interface."""
    out = {}
    for line in raw.splitlines()[1:]:
        p = line.split()
        if len(p) >= 4 and p[2] == "0x2" and p[3] != "00:00:00:00:00:00":
            out[p[3].lower()] = p[0]
    return out


def parse_ont(raw, name):
    """One luci-app-vsol-ddm / luci-app-hsgq-ddm cache file -> compact summary."""
    vs = jload(raw, {}) or {}
    ddm, onu, dev = vs.get("ddm", {}), vs.get("onu", {}), vs.get("device", {})
    thr = vs.get("thresholds") or {}
    return {"name": name, "connected": vs.get("connected"), "rx_dbm": ddm.get("rx_power_dbm"),
            "tx_dbm": ddm.get("tx_power_dbm"), "temp_c": ddm.get("temperature_c"),
            "state": onu.get("registered_status"), "uptime": dev.get("uptime"), "firmware": dev.get("firmware"),
            "model": dev.get("model"), "fec_uncorrectable": ddm.get("fec_uncorrectable_codewords"),
            "alarms": {k[6:]: v for k, v in onu.items() if k.startswith("alarm_")}, "ts": vs.get("timestamp"),
            "state_code": onu.get("state"),
            # The ONT's own warning limits (SFF-8472 / G.984.2 class), used by the Telegram watcher.
            "limits": {k: thr.get(k) for k in ("rx_pwr_low_alarm", "rx_pwr_low_warn", "rx_pwr_high_warn", "tx_pwr_low_warn",
                                               "tx_pwr_high_warn", "temp_low_warn", "temp_high_warn")},
            "age_s": int(time.time() - vs["timestamp"]) if vs.get("timestamp") else None}


def collect_router():
    if not ROUTER_HOST:
        raise RuntimeError("router.host is not set in config.json")
    sec = sections(ssh(ROUTER_HOST, router_sh()))
    leases = []
    for line in sec.get("LEASES", "").splitlines():
        p = line.split()
        if len(p) >= 4:
            leases.append({"expires": int(p[0]), "mac": p[1].lower(), "ip": p[2], "name": "" if p[3] == "*" else p[3]})
    names = {l["mac"]: l for l in leases}

    ifaces, wans = [], wan_ifaces()
    for i in (jload(sec.get("IF"), {}) or {}).get("interface", []):
        if i.get("interface") == "loopback":
            continue
        addr = (i.get("ipv4-address") or [{}])[0]
        a6 = (i.get("ipv6-address") or [{}])[0]
        p6 = (i.get("ipv6-prefix") or i.get("ipv6-prefix-assignment") or [{}])[0]
        gw = next((r.get("nexthop") for r in i.get("route", []) if r.get("target") == "0.0.0.0"), None)
        ifaces.append({"name": i.get("interface"), "up": i.get("up"), "proto": i.get("proto"),
                       "device": i.get("l3_device") or i.get("device"), "uptime": i.get("uptime"),
                       "ip": f"{addr.get('address')}/{addr.get('mask')}" if addr.get("address") else "",
                       "ip6": f"{a6.get('address')}/{a6.get('mask')}" if a6.get("address") else "",
                       "prefix6": f"{p6.get('address')}/{p6.get('mask')}" if p6.get("address") else "",
                       "gateway": gw, "dns": i.get("dns-server", []), "metric": i.get("metric"),
                       "restartable": i.get("interface") in wans, "group": wan_group_of(i.get("interface"))})

    mwan = {}
    for name, m in ((jload(sec.get("MWAN"), {}) or {}).get("interfaces") or {}).items():
        t = (m.get("track_ip") or [{}])[0]
        mwan[name] = {"status": m.get("status"), "online": m.get("online"), "score": m.get("score"),
                      "latency": t.get("latency"), "loss": t.get("packetloss")}

    ports = []
    devs = jload(sec.get("DEV"), {}) or {}
    for name in router_ports():
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
            "wifi": parse_wifi(sec, names), "onts": onts, "_names": names, "_arp": parse_arp(sec.get("ARP", ""))}


WANQ_SH = r"""
for i in __IFACES__; do
  ( T=$(uci -q get mwan3.$i.track_ip | cut -d' ' -f1)
    r=$(mwan3 use $i ping -c 3 -W 1 -q "${T:-8.8.8.8}" 2>&1 | tr '\n' ' ')
    echo "$i|${T:-8.8.8.8}|$r" ) &
done
wait
"""


def collect_wan_quality():
    """Latency and loss of every monitored WAN, pinging its mwan3 track IP through that link.
    mwan3's own status reports 0 / 0 unless its quality checking is enabled."""
    names = sorted(n for n in wan_ifaces() if re.fullmatch(r"[A-Za-z0-9_]{1,32}", n))
    if not names or not ROUTER_HOST:
        return {}
    out = {}
    for line in ssh(ROUTER_HOST, WANQ_SH.replace("__IFACES__", " ".join(names)), timeout=20).splitlines():
        iface, _, rest = line.partition("|")
        target, _, text = rest.partition("|")
        loss = re.search(r"(\d+)% packet loss", text)
        rtt = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+) ms", text)
        if iface in names:
            out[iface] = {"loss": int(loss.group(1)) if loss else 100,
                          "latency": round(float(rtt.group(2)), 1) if rtt else None,
                          "target": target, "measured": True}
    return out


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
        return {"vip": DNS_VIP, "vip_owner": "no Pi-holes configured", "vip6": DNS_VIP6, "vip6_owner": None, "nodes": []}
    with cf.ThreadPoolExecutor(len(PIHOLES)) as ex:
        nodes = list(ex.map(collect_pihole_one, PIHOLES))
    return {"vip": DNS_VIP, "vip_owner": vip_owner(), "vip6": DNS_VIP6, "vip6_owner": vip6_owner(), "nodes": nodes}


# ---------------------------------------------------------------- Omada


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


def connect_services():
    """(Re)build the API clients from C and the secrets: Pi-holes, Omada, Proxmox."""
    global PIHOLES, OMC, OMADA, PVE
    # Each Pi-hole's app password lives in secrets.env under the name given by "password_env".
    PIHOLES = [PiHole(p["name"], p["url"].rstrip("/"), E.get(p.get("password_env", ""), ""))
               for p in DNS.get("piholes", [])]
    OMC = C.get("omada", {})
    OMADA = Omada()
    PVE = C.get("proxmox", {})


def apply_config():
    """Make a saved change take effect without a restart."""
    derive_settings()
    connect_services()
    with _cache_lock:
        _cache.clear()


connect_services()


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


def port_vlan_body(port, native, untag, tag):
    """VLAN write for a single switch port. Omada 6.x answers "-1 General error" unless the port's
    name, profile and profile-override flag come with the VLAN fields (LAGs don't need them).
    The current values are sent back unchanged, so only the VLANs change."""
    return {"name": port.get("name") or f"Port{port['port']}", "profileId": port["profile_id"],
            "profileOverrideEnable": bool(port.get("override")), **vlan_body(native, untag, tag)}


NETMETA = VAR / "networks.json"  # NOC-side DHCP plans per Omada network id


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
    """VLAN id -> router interface names riding it over the trunk (e.g. 100 -> ['wan_b'])."""
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
    if kind.startswith("settings_"):
        return settings.plan(sys.modules[__name__], kind, p)
    if kind.startswith("ow_"):
        return openwrt.plan(sys.modules[__name__], kind, p)
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
        invalidate("switch")  # plan from fresh controller data
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
            body = port_vlan_body(cur, native, untag, tag)  # VLANs change; name/profile/override sent back as they are
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
        invalidate("switch")  # plan from fresh controller data
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
        # Plan from fresh controller data: changes made directly in Omada must be seen immediately.
        invalidate("switch")
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
                      *dhcp_lines(plan)]
            # Optional: carry it tagged on the switch<->router LAG, so the router actually receives it.
            lag = None
            if p.get("carry_lag") not in (None, "", False):
                lag = next((x for x in sw.get("lags", []) if str(x["lag"]) == str(p["carry_lag"])), None)
                if not lag:
                    raise ValueError("unknown LAG")
                detail.append(f"Omada: carry it tagged on LAG {lag['lag']} (ports {' + '.join(map(str, lag['ports']))}, the link to the router)")
            elif not p.get("router"):
                detail.append("Not on any port yet: add it with Edit VLANs on the ports/LAG that should carry it.")
            # Optional: the router side (device, interface, firewall, DHCP, mwan3) in the same action.
            r_plan = None
            router = p.get("router") or {}
            if router.get("interface") or router.get("device"):
                if router.get("interface"):
                    r_plan = openwrt.plan(sys.modules[__name__], "ow_setup", router)
                else:
                    r_plan = openwrt.plan(sys.modules[__name__], "ow_device_create", router["device"])
                detail += ["Router (OpenWrt), applied after the switch:"] + ["  " + x for x in r_plan[1]]
                if not lag:
                    detail.append("⚠ The VLAN is not carried to the router, so the new router interface will have no traffic until you add it to the LAG")
                detail.append("If the router part fails, the new Omada network is removed again, so nothing is left half-done")

            def run():
                OMADA.call("POST", "/lan-networks", json=body)
                time.sleep(1)
                got = [n for n in OMADA.call("GET", "/lan-networks", params={"page": 1, "pageSize": 100}).get("data", [])
                       if n.get("vlan") == vlan and n.get("name") == name]
                if not got:
                    raise RuntimeError("controller accepted the request but the network isn't listed - check Omada")
                nid = got[0]["id"]
                m = load_meta()
                m[nid] = plan
                save_meta(m)
                res = {"verified": True, "id": nid}
                tagged = False
                try:
                    if lag:
                        tag = lag["tag_ids"] + [nid]
                        OMADA.call("PATCH", f"/switches/{SWITCH_MAC}/lags/{lag['lag']}", json=vlan_body(lag["native_id"], lag["untag_ids"], tag))
                        tagged = True
                        verify_vlans(lambda: port_details([lag["lag"]])["_lags"].get(lag["lag"], {}), lag["native_id"], lag["untag_ids"], tag, f"LAG {lag['lag']}")
                        res["lag"] = lag["lag"]
                    if r_plan:
                        res["router"] = r_plan[3]()
                except Exception as ex:
                    # Undo the switch side so a failed router step leaves nothing behind.
                    if tagged:
                        OMADA.call("PATCH", f"/switches/{SWITCH_MAC}/lags/{lag['lag']}", json=vlan_body(lag["native_id"], lag["untag_ids"], lag["tag_ids"]))
                    OMADA.call("DELETE", f"/lan-networks/{nid}")
                    m = load_meta()
                    m.pop(nid, None)
                    save_meta(m)
                    raise RuntimeError(f"{ex}. The new Omada network was removed again, so nothing was left half-done") from None
                return res
            word = r_plan[2] if r_plan else None
            return (f"Create network {name} (VLAN {vlan})" + (" on the switch and the router" if r_plan else ""),
                    detail, word, run, ["switch", "router", "owcfg"])

        cur = nets.get(p.get("id"))
        if not cur:
            raise ValueError("unknown network")
        if not cur["editable"]:
            raise ValueError(f"'{cur['name']}' is Omada's built-in network (the switch's management VLAN) - read-only here")

        if kind == "net_delete":
            nid = cur["id"]
            # Like Omada, a carried VLAN is first removed from its ports/LAGs, but here every
            # one of them is listed in the preview. A native (PVID) VLAN is never stripped
            # silently: that port would be left without an untagged network.
            natives = [f"port {x['port']}" for x in sw["ports"] if not x["lag"] and x["native_id"] == nid] + \
                      [f"LAG {x['lag']}" for x in sw.get("lags", []) if x["native_id"] == nid]
            if natives:
                raise ValueError(f"'{cur['name']}' is the native (untagged) VLAN on {', '.join(natives)} - "
                                 "give those a different native VLAN first (Edit VLANs)")
            detach = [("port", x["port"], x) for x in sw["ports"] if not x["lag"] and nid in x["untag_ids"] + x["tag_ids"]] + \
                     [("lag", x["lag"], x) for x in sw.get("lags", []) if nid in x["untag_ids"] + x["tag_ids"]]
            detail = [f"Delete network '{cur['name']}' (VLAN {cur['vlan']})"]
            if detach:
                detail.append("First removes it from: " + ", ".join(
                    f"{'LAG' if k == 'lag' else 'port'} {i} ({'untagged' if nid in x['untag_ids'] else 'tagged'})" for k, i, x in detach))
                for k, i, x in detach:
                    if k == "lag" or x.get("protected"):
                        detail.append(f"⚠ {'LAG ' + str(i) if k == 'lag' else 'port ' + str(i)} is "
                                      f"{x.get('protected') or 'protected'}")
            else:
                detail.append("No port or LAG carries it.")
            # Optional router clean-up, each part only if ticked (device / interface / mwan3).
            ro = p.get("router") or {}
            r_plan = None
            if any(ro.get(k) for k in ("device", "interface", "mwan")):
                r_plan = openwrt.plan(sys.modules[__name__], "ow_vlan_cleanup", {"vlan": cur["vlan"], **ro})
                detail += ["Router (OpenWrt), done first:"] + ["  " + x for x in r_plan[1]]
                detail.append("If the router part fails, the switch is left untouched")
            elif users.get(cur["vlan"]):
                detail.append(f"⚠ Router interface(s) on {TRUNK_DEV}.{cur['vlan']} lose this VLAN: {', '.join(users[cur['vlan']])}"
                              " (kept on the router: tick the router options to remove them too)")

            def run():
                out = {}
                if r_plan:
                    out["router"] = r_plan[3]()  # raises (and rolls back) before the switch is touched
                out.update(omada_delete())
                return out

            def omada_delete():
                for k, i, x in detach:
                    untag = [v for v in x["untag_ids"] if v != nid]
                    tag = [v for v in x["tag_ids"] if v != nid]
                    path = f"/switches/{SWITCH_MAC}/lags/{i}" if k == "lag" else f"/switches/{SWITCH_MAC}/ports/{i}"
                    OMADA.call("PATCH", path, json=vlan_body(x["native_id"], untag, tag) if k == "lag"
                               else port_vlan_body(x, x["native_id"], untag, tag))
                    read = (lambda i=i: port_details([i])["_lags"].get(i, {})) if k == "lag" else (lambda i=i: port_details().get(i, {}))
                    verify_vlans(read, x["native_id"], untag, tag, f"{k} {i}")
                OMADA.call("DELETE", f"/lan-networks/{cur['id']}")
                time.sleep(1)
                if any(n.get("id") == cur["id"] for n in OMADA.call("GET", "/lan-networks", params={"page": 1, "pageSize": 100}).get("data", [])):
                    raise RuntimeError("controller still lists the network - check Omada")
                m = load_meta()
                if m.pop(cur["id"], None) is not None:
                    save_meta(m)
                return {"verified": True}
            return (f"Delete network {cur['name']}" + (" and its router setup" if r_plan else ""), detail,
                    f"vlan {cur['vlan']}", run, ["switch", "router", "owcfg"])

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

    if kind in ("device_add", "device_edit", "device_delete"):
        devs = C.setdefault("devices", [])
        names = [d["name"] for d in devs]
        orig = p.get("original_name")
        if kind != "device_add" and orig not in names:
            raise ValueError("unknown device")
        if kind == "device_delete":
            cur = devs[names.index(orig)]

            def run():
                C["devices"] = [d for d in C["devices"] if d["name"] != orig]
                save_config()
                return {"saved": True}
            how = f"MAC {cur['mac']}" if cur.get("mac") else cur.get("target")
            return f"Stop tracking {orig}", [f"Removes '{orig}' ({how}) from device health checks",
                                              "The device itself is not touched"], None, run, ["ping"]

        name = (p.get("name") or "").strip()
        if not 1 <= len(name) <= 64 or not re.fullmatch(r"[\w .\-()/+]+", name):
            raise ValueError("name: 1-64 letters, digits, space, _ . - ( ) / +")
        if name in names and name != orig:
            raise ValueError(f"a device named '{name}' already exists")
        group = p.get("group") or "other"
        if group not in DEVICE_GROUPS:
            raise ValueError("unknown group")
        port = p.get("port") or None
        if port is not None:
            port = int(port)
            if not 1 <= port <= 65535:
                raise ValueError("port 1-65535")
        entry = {"name": name, "group": group}
        mode = p.get("mode", "ip")
        if mode == "mac":
            mac = (p.get("mac") or "").strip().lower().replace("-", ":")
            if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
                raise ValueError("MAC must look like aa:bb:cc:dd:ee:ff")
            entry["mac"] = mac
            if port:
                entry["port"] = port
            now = neighbour_ips().get(mac)
            how = f"follows MAC {mac} (currently {now or 'not seen on the network'})"
        else:
            try:
                addr = ipaddress.ip_address((p.get("ip") or "").strip())
            except ValueError:
                raise ValueError("enter a valid IPv4 or IPv6 address")
            ip = str(addr)
            entry["target"] = (f"[{ip}]:{port}" if addr.version == 6 else f"{ip}:{port}") if port else ip
            how = f"checks {entry['target']}" + (" (TCP)" if port else " (ping)")
        detail = [f"{'Track' if kind == 'device_add' else 'Update'} '{name}' [{group}]: {how}"]
        if kind == "device_edit":
            old = devs[names.index(orig)]
            detail.insert(0, "Was: " + (f"MAC {old['mac']}" if old.get("mac") else old.get("target", "?")) +
                          (f", name '{orig}'" if orig != name else ""))

        def run():
            if kind == "device_add":
                C["devices"].append(entry)
            else:
                C["devices"][[d["name"] for d in C["devices"]].index(orig)] = entry
                for path in (ROUTER.get("wan_paths") or {}).values():  # keep diagnosis paths pointing at it
                    if path.get("isp_device") == orig:
                        path["isp_device"] = name
            save_config()
            return {"saved": True}
        return (f"{'Add' if kind == 'device_add' else 'Edit'} device {name}", detail, None, run, ["ping"])

    if kind == "wan_group_restart":
        group = str(p.get("group", ""))
        members = wan_groups().get(group)
        if not members:
            raise ValueError(f"unknown WAN group '{group}' (groups: {', '.join(wan_groups()) or 'none'})")
        script = "".join(f"ifup {shlex.quote(i)}; sleep 3\n" for i in members)

        def run():
            ssh(ROUTER_HOST, script, timeout=15 + 5 * len(members))
            return {"restarted": members}
        return (f"Restart all {group} links ({len(members)})",
                [f"Router runs ifup on {', '.join(members)}, 3 s apart",
                 "Each drops for ~5-30 s; mwan3 moves traffic to the other ISPs meanwhile"], None, run, ["router"])

    if kind == "wan_groups_set":
        groups = p.get("groups")
        if not isinstance(groups, dict):
            raise ValueError("groups must be {name: [interfaces]}")
        known = set(wan_candidates())
        clean, seen = {}, {}
        for g, members in groups.items():
            g = str(g).strip().lower()
            if not re.fullmatch(r"[a-z0-9_-]{1,20}", g):
                raise ValueError(f"group name '{g}': use 1-20 letters, digits, - or _")
            members = [str(m) for m in (members or [])]
            if not members:
                raise ValueError(f"group '{g}' has no interfaces")
            for m in members:
                if m not in known:
                    raise ValueError(f"'{m}' is not a router interface")
                if m in seen:
                    raise ValueError(f"'{m}' is in both '{seen[m]}' and '{g}'")
                seen[m] = g
            clean[g] = members
        old = wan_groups()
        detail = [f"{g}: {', '.join(m)}" + ("" if old.get(g) == m else "  (was: " + (", ".join(old[g]) if g in old else "new group") + ")")
                  for g, m in clean.items()] + [f"{g}: removed (was {', '.join(m)})" for g, m in old.items() if g not in clean]
        # Optional per-group path used by the WAN diagnosis: switch port, ISP box, fibre ONT.
        old_paths = ROUTER.get("wan_paths") or {}
        paths = old_paths if p.get("paths") is None else {}
        if p.get("paths") is not None:
            dev_names = {d["name"] for d in C.get("devices", [])}
            ont_names = {o["name"] for o in ONT_CACHES}
            for g, path in (p.get("paths") or {}).items():
                g = str(g).strip().lower()
                if g not in clean:
                    continue
                path = path or {}
                entry = {}
                if path.get("switch_port") not in (None, "", 0):
                    port = int(path["switch_port"])
                    if not 1 <= port <= SWITCH_PORTS:
                        raise ValueError(f"{g}: switch port must be 1-{SWITCH_PORTS}")
                    entry["switch_port"] = port
                for key, names, what in (("isp_device", dev_names, "tracked device"), ("ont", ont_names, "fibre ONT source")):
                    if path.get(key):
                        if path[key] not in names:
                            raise ValueError(f"{g}: '{path[key]}' is not a {what}")
                        entry[key] = path[key]
                if entry:
                    paths[g] = entry
            for g in sorted(set(paths) | set(old_paths)):
                if paths.get(g) != old_paths.get(g):
                    pp = paths.get(g)
                    detail.append(f"{g} path: " + (", ".join(f"{k.replace('_', ' ')} {v}" for k, v in pp.items()) if pp else "none")
                                  + f"  (was: {', '.join(f'{k} {v}' for k, v in (old_paths.get(g) or {}).items()) or 'none'})")
        if clean == old and paths == old_paths:
            raise ValueError("nothing changed")

        def run():
            ROUTER["wan_groups"] = clean
            ROUTER["wan_paths"] = paths
            save_config()
            return {"saved": True}
        return "Update WAN groups", detail, None, run, ["router"]

    if kind == "wan_restart":
        iface = p.get("iface")
        if iface not in wan_ifaces():
            raise ValueError("not a restartable WAN")

        def run():
            ssh(ROUTER_HOST, f"ifup {iface}")
            return "restarted"
        return f"Restart WAN interface {iface}", [f"Router runs: ifup {iface}", "That connection drops for ~5-30 s; mwan3 moves traffic to the others"], None, run, ["router"]

    raise ValueError("unknown action")


# ---------------------------------------------------------------- app + auth
app = FastAPI(title="NOC", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)  # the page is ~160 KB, ~35 KB gzipped
TG = notify.Telegram(sys.modules[__name__])


@app.on_event("startup")
def start_workers():
    # The watcher and Telegram bot run in this process (single uvicorn worker).
    # NOC_NO_WORKERS=1 keeps them off, e.g. in offline tests.
    if os.environ.get("NOC_NO_WORKERS") != "1":
        TG.start()
SESSION_KEY = E["NOC_SESSION_KEY"].encode()
SESSION_HOURS = 12


def session_sig(user, exp):
    # the password's fingerprint is part of the signature: a new password signs old sessions out
    return hmac.new(SESSION_KEY, f"{user}:{exp}:{auth.session_salt(E)}".encode(), hashlib.sha256).hexdigest()


def make_session(user):
    exp = int(time.time()) + SESSION_HOURS * 3600
    return f"{user}:{exp}:{session_sig(user, exp)}"


def valid_session(tok):
    try:
        user, exp, sig = tok.split(":")
        good = session_sig(user, exp)
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except Exception:
        return False


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path in ("/login", "/login/forgot", "/login/reset", "/healthz") or valid_session(request.cookies.get("noc", "")):
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
        auth.verify(str(form.get("password", "")), E)
    if not ok:
        await asyncio.sleep(1)
        return RedirectResponse("/login?bad=1", status_code=303)
    return signed_in(request, RedirectResponse("/", status_code=303))


def signed_in(request, resp):
    resp.set_cookie("noc", make_session(E["NOC_ADMIN_USER"]), httponly=True, samesite="strict",
                    max_age=SESSION_HOURS * 3600, secure=request.headers.get("x-forwarded-proto") == "https")
    return resp


def client_ip(request):
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def set_password(request, new, how):
    set_secrets(auth.updates(new))
    audit(request, "password", how, {"ok": True})
    TG.queue(f"🔑 <b>{notify.e(SITE_NAME)}</b>: {how.lower()} from {notify.e(client_ip(request))}. "
             "Other sessions are signed out.")


@app.post("/api/account/password")
async def change_password(request: Request):
    p = await request.json()
    cur, new = str(p.get("current", "")), str(p.get("new", ""))
    if not auth.verify(cur, E):
        await asyncio.sleep(1)
        return JSONResponse({"error": "The current password is wrong."}, status_code=400)
    why = auth.check_new(new, E["NOC_ADMIN_USER"]) or ("it is the current password" if new == cur else None)
    if why:
        return JSONResponse({"error": f"New password: {why}."}, status_code=400)
    await asyncio.to_thread(set_password, request, new, "Changed the NOC password")
    return signed_in(request, JSONResponse({"ok": True}))


# Forgot password: a one-time code goes to the paired Telegram chat, the only channel the NOC has
# outside the browser. Without Telegram the reset is done on the NOC's console with noc-passwd.
RESET_TTL, RESET_TRIES, RESET_EVERY = 600, 5, 60
RESET = {"hash": None, "exp": 0.0, "tries": 0, "sent": 0.0}


@app.post("/login/forgot")
async def forgot_password(request: Request):
    if not (TG.token() and TG.chat_id()):
        return JSONResponse({"error": "Telegram isn't set up, so no code can be sent. Reset the password on the "
                                      "NOC's console instead: run noc-passwd as root.", "console": True}, status_code=400)
    now = time.time()
    if now - RESET["sent"] < RESET_EVERY:
        return JSONResponse({"error": f"A code was just sent. You can ask again in {int(RESET_EVERY - (now - RESET['sent'])) + 1} s."},
                            status_code=429)
    code = f"{secrets.randbelow(10 ** 8):08d}"
    text = (f"🔑 <b>{notify.e(SITE_NAME)}</b>: password reset requested from {notify.e(client_ip(request))}.\n"
            f"Code: <code>{code[:4]}-{code[4:]}</code> (valid {RESET_TTL // 60} min, single use).\n"
            "If this wasn't you, ignore it: the password stays as it is.")
    try:
        await asyncio.to_thread(TG.send, TG.chat_id(), text)
    except Exception as ex:
        return JSONResponse({"error": f"Could not send the code over Telegram: {ex}"}, status_code=502)
    RESET.update(hash=hashlib.sha256(code.encode()).hexdigest(), exp=now + RESET_TTL, tries=0, sent=now)
    audit(request, "password", "Requested a password reset code (sent to Telegram)", {"ok": True})
    return {"sent": True, "valid_min": RESET_TTL // 60}


@app.post("/login/reset")
async def reset_password(request: Request):
    p = await request.json()
    code, new = re.sub(r"\D", "", str(p.get("code", ""))), str(p.get("new", ""))
    if not RESET["hash"] or time.time() > RESET["exp"]:
        return JSONResponse({"error": "No valid code: ask for a new one."}, status_code=400)
    if not hmac.compare_digest(hashlib.sha256(code.encode()).hexdigest(), RESET["hash"]):
        RESET["tries"] += 1
        if RESET["tries"] >= RESET_TRIES:
            RESET.update(hash=None, exp=0.0)
            return JSONResponse({"error": "Wrong code too many times: ask for a new one."}, status_code=400)
        await asyncio.sleep(1)
        return JSONResponse({"error": f"Wrong code ({RESET_TRIES - RESET['tries']} tries left)."}, status_code=400)
    why = auth.check_new(new, E["NOC_ADMIN_USER"])
    if why:
        return JSONResponse({"error": f"New password: {why}."}, status_code=400)  # the code stays valid
    RESET.update(hash=None, exp=0.0)
    await asyncio.to_thread(set_password, request, new, "Reset the NOC password with a Telegram code")
    return signed_in(request, JSONResponse({"ok": True}))


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
    with cf.ThreadPoolExecutor(6) as ex:
        f = {k: ex.submit(cached, k, ttl, fn, VIEW_STALE) for k, ttl, fn in [
            ("ping", 10, collect_ping), ("router", 15, collect_router), ("dns", 15, collect_dns),
            ("proxmox", 20, collect_proxmox), ("switch", 20, collect_switch), ("wanq", 30, collect_wan_quality)]}
        r = {k: v.result() for k, v in f.items()}
    router = r["router"] if "error" not in r["router"] else {}
    q = r["wanq"] if "error" not in r["wanq"] else {}
    return {"devices": r["ping"],
            "wans": [{"name": i["name"], "up": i["up"], "ip": i["ip"], "ip6": i.get("ip6", ""), "uptime": i["uptime"],
                      **(router.get("mwan", {}).get(i["name"], {})), **q.get(i["name"], {})}
                     for i in router.get("interfaces", []) if i["name"] in wan_ifaces()],
            "onts": router.get("onts", []), "router_system": router.get("system"),
            "dns": r["dns"], "proxmox": r["proxmox"],
            "switch_ports_up": sum(1 for p in r["switch"].get("ports", []) if p["link"]) if "ports" in r["switch"] else None,
            "errors": {k: v["error"] for k, v in r.items() if isinstance(v, dict) and "error" in v}}


@app.get("/api/devices")
def api_devices():
    live = {x["name"]: x for x in cached("ping", 10, collect_ping, VIEW_STALE)} if C.get("devices") else {}
    r = cached("router", 15, collect_router, VIEW_STALE)
    leases = r.get("leases", []) if isinstance(r, dict) else []
    tracked_macs = {d["mac"].lower() for d in C.get("devices", []) if d.get("mac")}
    tracked_ips = {(d.get("target") or "").split(":")[0] for d in C.get("devices", [])}
    tracked_ips |= {x.get("current_ip") for x in live.values() if x.get("current_ip")}
    return {"groups": DEVICE_GROUPS,
            "devices": [{**d, **{k: live.get(d["name"], {}).get(k) for k in ("up", "ms", "current_ip", "note")}}
                        for d in C.get("devices", [])],
            "suggestions": [l for l in leases if l["mac"] not in tracked_macs and l["ip"] not in tracked_ips]}


@app.get("/api/router")
def api_router():
    d = cached("router", 15, collect_router, VIEW_STALE)
    q = cached("wanq", 30, collect_wan_quality, VIEW_STALE)
    out = {k: v for k, v in d.items() if not k.startswith("_")}
    if isinstance(q, dict) and "error" not in q and isinstance(out.get("mwan"), dict):
        out["mwan"] = {n: {**m, **q.get(n, {})} for n, m in out["mwan"].items()}
    return out


@app.get("/api/ap")
def api_ap():
    return cached("ap", 15, collect_ap, VIEW_STALE)


@app.get("/api/dns")
def api_dns():
    return cached("dns", 10, collect_dns, VIEW_STALE)


@app.get("/api/switch")
def api_switch():
    return cached("switch", 20, collect_switch, VIEW_STALE)


@app.get("/api/proxmox")
def api_proxmox():
    return cached("proxmox", 20, collect_proxmox, VIEW_STALE)


@app.get("/api/audit")
def api_audit():
    return tail_jsonl(AUDIT, 100)


@app.get("/api/alerts")
def api_alerts():
    r = cached("router", 15, collect_router)
    procs = {i["name"]: i.get("proto") for i in r.get("interfaces", [])} if isinstance(r, dict) else {}
    return {**TG.status(), "groups": wan_groups(), "monitored": sorted(wan_ifaces()),
            "candidates": [{"name": n, "proto": procs.get(n)} for n in wan_candidates()]}


@app.get("/api/openwrt")
def api_openwrt():
    """Router config (devices, interfaces, firewall zones, DHCP, mwan3) for the Router and MultiWAN tabs."""
    return cached("owcfg", 10, lambda: openwrt.read(sys.modules[__name__]), VIEW_STALE)


@app.get("/api/settings")
def api_settings():
    return settings.current(sys.modules[__name__])


@app.get("/api/diagnose")
def api_diagnose(target: str):
    """Walk a WAN link's (or every link in a WAN group's) path and report where it breaks."""
    targets = wan_groups().get(target) or ([target] if target in wan_ifaces() else None)
    if not targets:
        return JSONResponse({"error": f"'{target}' is not a monitored WAN or group"}, status_code=400)
    return {"results": list(TG.run_diagnoses(targets).values())}


@app.post("/api/alerts/pair")
def api_alerts_pair(request: Request):
    if not TG.token():
        return JSONResponse({"error": "Add TELEGRAM_BOT_TOKEN to secrets.env first"}, status_code=400)
    code = TG.new_pairing_code()
    audit(request, "telegram_pair_code", "Created a Telegram pairing code", {"ok": True})
    return {"code": code, "bot": TG.bot_name, "expires_in": notify.PAIR_TTL}


@app.post("/api/alerts/test")
def api_alerts_test():
    if not TG.chat_id():
        return JSONResponse({"error": "Pair a Telegram chat first"}, status_code=400)
    try:
        TG.send(TG.chat_id(), f"👋 Test message from <b>{notify.e(SITE_NAME)}</b>. Alerts will arrive here.")
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=502)
    return {"ok": True}


@app.post("/api/alerts/unpair")
def api_alerts_unpair(request: Request):
    (C.get("telegram") or {}).pop("chat_id", None)
    save_config()
    audit(request, "telegram_unpair", "Unpaired the Telegram chat", {"ok": True})
    return {"ok": True}


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
