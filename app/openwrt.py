"""OpenWrt router configuration from the NOC: devices, interfaces, firewall zones, DHCP
and mwan3 (MultiWAN Manager), modelled on what LuCI offers.

Safety: every change is one `uci batch` applied with automatic rollback, the way LuCI's
"apply with rollback" works. The router copies network/firewall/dhcp/mwan3, applies the
batch, runs `reload_config`, and starts a detached timer that restores the copies after
ROLLBACK_S seconds unless the NOC confirms. The NOC confirms only after reconnecting over
SSH and seeing every WAN that was online before the change online again. A change that
cuts the router off from the NOC therefore undoes itself.

Secrets (PPPoE passwords) are write-only: never returned by read(), never in previews or
the audit log.
"""
import ipaddress
import re
import secrets
import shlex
import time

CONFIGS = ("network", "firewall", "dhcp", "mwan3")
ROLLBACK_S = 90
LISTS = {"ports", "network", "track_ip", "use_member", "dns", "ipaddr", "list"}
SECRET_OPTS = {"password", "key", "psk"}

NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,14}")          # interface / section names (netifd: <= 15)
DEV = re.compile(r"[A-Za-z][A-Za-z0-9_.@-]{0,14}")        # Linux device names (IFNAMSIZ 15)
MAC = re.compile(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
SAFE = re.compile(r"[^'\n\r\\]{0,128}")                    # free text that is safe inside '...'

READ_SH = r"""
for c in network firewall dhcp mwan3; do uci -X show $c 2>/dev/null; done
printf '\n@@MWANSTATUS\n'; ubus call mwan3 status 2>/dev/null || echo '{}'
"""


# ------------------------------------------------------------------ reading
def parse_uci(text):
    """`uci -X show` -> {config: {section: {".type", ".name", ".index", option: value}}}."""
    out, idx = {}, {}
    for line in text.splitlines():
        if "=" not in line or line.startswith("@@"):
            continue
        key, _, val = line.partition("=")
        parts = key.split(".")
        if len(parts) == 2:
            cfg, sec = parts
            out.setdefault(cfg, {})[sec] = {".type": val, ".name": sec, ".index": idx.setdefault(cfg, 0)}
            idx[cfg] += 1
        elif len(parts) == 3 and parts[0] in out and parts[1] in out[parts[0]]:
            cfg, sec, opt = parts
            try:
                vals = shlex.split(val)
            except ValueError:
                vals = [val.strip("'")]
            out[cfg][sec][opt] = vals if (opt in LISTS or len(vals) > 1) else (vals[0] if vals else "")
    return out


def sections(cfg, typ):
    return sorted((s for s in cfg.values() if s[".type"] == typ), key=lambda s: s[".index"])


def as_list(v):
    return v if isinstance(v, list) else ([v] if v not in (None, "") else [])


def read(noc):
    """The router's config as the Router and MultiWAN tabs show it (no secrets)."""
    raw = noc.ssh(noc.ROUTER_HOST, READ_SH, timeout=25)
    body, _, status = raw.partition("\n@@MWANSTATUS\n")
    u = parse_uci(body)
    net, fw, dh, mw = (u.get(c, {}) for c in CONFIGS)
    mstatus = (noc.jload(status, {}) or {}).get("interfaces") or {}

    devices = []
    for d in sections(net, "device"):
        devices.append({"id": d[".name"], "name": d.get("name", ""), "type": d.get("type", "") or "ethernet",
                        "base": d.get("ifname", ""), "vid": d.get("vid", ""), "ports": as_list(d.get("ports")),
                        "macaddr": d.get("macaddr", ""), "ipv6": d.get("ipv6", "1") != "0", "mtu": d.get("mtu", ""),
                        "mode": d.get("mode", "")})
    ifaces = []
    for i in sections(net, "interface"):
        if i[".name"] == "loopback":
            continue
        e = {k: v for k, v in i.items() if not k.startswith(".") and k not in SECRET_OPTS}
        e.update(name=i[".name"], password_set=bool(i.get("password")),
                 ipaddr=as_list(i.get("ipaddr")), dns=as_list(i.get("dns")))
        ifaces.append(e)
    zones = [{"id": z[".name"], "name": z.get("name", ""), "network": as_list(z.get("network")),
              "input": z.get("input"), "output": z.get("output"), "forward": z.get("forward"),
              "masq": z.get("masq") == "1"} for z in sections(fw, "zone")]
    dhcp = [{"id": s[".name"], "interface": s.get("interface", ""), "start": s.get("start", ""),
             "limit": s.get("limit", ""), "leasetime": s.get("leasetime", ""), "ignore": s.get("ignore") == "1"}
            for s in sections(dh, "dhcp")]
    mwan = {
        "interfaces": [{**{k: v for k, v in s.items() if not k.startswith(".")}, "name": s[".name"],
                        "track_ip": as_list(s.get("track_ip")),
                        "status": (mstatus.get(s[".name"]) or {}).get("status")} for s in sections(mw, "interface")],
        "members": [{"name": s[".name"], "interface": s.get("interface", ""), "metric": s.get("metric", "1"),
                     "weight": s.get("weight", "1")} for s in sections(mw, "member")],
        "policies": [{"name": s[".name"], "use_member": as_list(s.get("use_member")),
                      "last_resort": s.get("last_resort", "unreachable")} for s in sections(mw, "policy")],
        "rules": [{**{k: v for k, v in s.items() if not k.startswith(".")}, "name": s[".name"]}
                  for s in sections(mw, "rule")],
    }
    used_dev = {}
    for i in ifaces:
        used_dev.setdefault(i.get("device", ""), []).append(f"interface {i['name']}")
    for d in devices:
        for p in d["ports"]:
            used_dev.setdefault(p, []).append(f"{d['name']} port")
        if d["base"]:
            used_dev.setdefault(d["base"], []).append(f"base of {d['name']}")
    for d in devices:
        d["used_by"] = used_dev.get(d["name"], [])
    zone_of = {n: z["name"] for z in zones for n in z["network"]}
    for i in ifaces:
        i["zone"] = zone_of.get(i["name"])
        i["mwan"] = any(m["name"] == i["name"] for m in mwan["interfaces"])
    return {"devices": devices, "interfaces": ifaces, "zones": zones, "dhcp": dhcp, "mwan": mwan,
            "trunk": noc.TRUNK_DEV, "rollback_s": ROLLBACK_S}


def related_to_vlan(c, trunk, vid):
    """Router objects that ride on switch VLAN `vid`: the trunk.vid device, devices built on it
    (MAC VLANs etc.), interfaces on any of those, and which of those interfaces mwan3 tracks."""
    vid = str(vid)
    base = [d for d in c["devices"] if d["type"] == "8021q" and d["base"] == trunk and str(d["vid"]) == vid]
    names = {d["name"] for d in base} or {f"{trunk}.{vid}"}
    devices = list(base)
    for d in c["devices"]:
        if d["base"] in names and d not in devices:
            devices.append(d)
            names.add(d["name"])
    ifaces = [i["name"] for i in c["interfaces"] if i.get("device") in names]
    tracked = {m["name"] for m in c["mwan"]["interfaces"]}
    return {"devices": devices, "interfaces": ifaces, "mwan": [i for i in ifaces if i in tracked]}


# ------------------------------------------------------------------ uci batch helpers
def q(v):
    v = str(v)
    if not SAFE.fullmatch(v):
        raise ValueError("values can't contain quotes, backslashes or line breaks")
    return f"'{v}'"


class Batch:
    def __init__(self):
        self.lines, self.detail, self.touched = [], [], set()

    def set(self, path, value):
        self.touched.add(path.split(".")[0])
        self.lines.append(f"set {path}={q(value)}")

    def section(self, path, typ):
        self.touched.add(path.split(".")[0])
        self.lines.append(f"set {path}={typ}")

    def delete(self, path):
        self.touched.add(path.split(".")[0])
        self.lines.append(f"delete {path}")

    def add_list(self, path, value):
        self.touched.add(path.split(".")[0])
        self.lines.append(f"add_list {path}={q(value)}")

    def del_list(self, path, value):
        self.touched.add(path.split(".")[0])
        self.lines.append(f"del_list {path}={q(value)}")

    def opt(self, path, value):
        """Set when given, delete when empty."""
        if value in (None, "", []):
            self.delete(path)
        elif isinstance(value, list):
            self.delete(path)
            for v in value:
                self.add_list(path, v)
        else:
            self.set(path, value)


def _int(v, lo, hi, what):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number") from None
    if not lo <= n <= hi:
        raise ValueError(f"{what} must be {lo}-{hi}")
    return n


def _ip(v, what, cidr=False):
    try:
        (ipaddress.IPv4Interface if cidr else ipaddress.IPv4Address)(v)
    except ValueError:
        raise ValueError(f"{what} must be an IPv4 address" + (" with prefix, e.g. 10.0.20.1/24" if cidr else "")) from None
    if cidr and "/" not in v:
        raise ValueError(f"{what} needs a prefix, e.g. {v}/24")
    return v


def _bool(v):
    return v in (True, 1, "1", "true", "on", "yes")


# ------------------------------------------------------------------ apply with rollback
APPLY_SH = r"""
ID=__ID__; B=/tmp/noc-rb-$ID
# Staged changes are shared with LuCI: never commit someone's unsaved edits along with ours.
for c in network firewall dhcp mwan3; do
  if [ -n "$(uci changes $c 2>/dev/null)" ]; then echo "PENDING-CHANGES $c"; exit 0; fi
done
mkdir -p "$B" || exit 1
for c in network firewall dhcp mwan3; do [ -f /etc/config/$c ] && cp /etc/config/$c "$B/$c"; done
cat > "$B/restore.sh" <<'RB'
#!/bin/sh
B="$(dirname "$0")"
[ -f "$B/confirmed" ] && exit 0
for c in network firewall dhcp mwan3; do [ -f "$B/$c" ] && cp "$B/$c" /etc/config/$c; done
reload_config
touch "$B/rolled_back"
logger -t noc "change $(basename "$B") rolled back"
RB
chmod +x "$B/restore.sh"
# `uci batch` exits 0 even when a line fails, so judge it by its error output.
# Deleting an option that isn't set ("Entry not found") is harmless.
ERR=$(uci batch 2>&1 <<'UCI'
__BATCH__
UCI
)
if printf '%s\n' "$ERR" | grep -v 'Entry not found' | grep -q .; then
  for c in network firewall dhcp mwan3; do uci revert $c 2>/dev/null; done
  rm -rf "$B"; echo "BATCH-FAILED: $ERR"; exit 0
fi
for c in network firewall dhcp mwan3; do uci commit $c; done
setsid sh -c "sleep __TIMEOUT__; sh '$B/restore.sh'" </dev/null >/dev/null 2>&1 &
reload_config
echo "APPLIED $ID"
"""


def _online(noc):
    st = noc.jload(noc.ssh(noc.ROUTER_HOST, "ubus call mwan3 status 2>/dev/null || echo '{}'", timeout=15), {}) or {}
    return {n for n, s in (st.get("interfaces") or {}).items() if s.get("status") == "online"}


def apply(noc, batch, allow_offline=()):
    """Apply a Batch with rollback; confirm once every previously-online WAN is online again."""
    if not batch.lines:
        raise ValueError("nothing to change")
    before = _online(noc)
    cid = secrets.token_hex(4)
    script = (APPLY_SH.replace("__ID__", cid).replace("__TIMEOUT__", str(ROLLBACK_S))
              .replace("__BATCH__", "\n".join(batch.lines)))
    try:
        out = noc.ssh(noc.ROUTER_HOST, script, timeout=60)
    except RuntimeError as ex:
        out = str(ex)
    if "PENDING-CHANGES" in out:
        cfg = out.split("PENDING-CHANGES", 1)[1].split()[0]
        raise RuntimeError(f"the router has unsaved changes in '{cfg}' (probably from LuCI); save or revert them there first. Nothing was applied")
    if "APPLIED" not in out:
        raise RuntimeError("the router refused the change, nothing was applied: " + out.split("BATCH-FAILED:", 1)[-1].strip()[:300])
    expect = before - set(allow_offline)
    deadline, missing = time.time() + 45, expect
    time.sleep(6)
    while time.time() < deadline:
        try:
            missing = expect - _online(noc)
        except Exception:
            missing = expect  # router not reachable (yet)
        if not missing:
            noc.ssh(noc.ROUTER_HOST, f"touch /tmp/noc-rb-{cid}/confirmed; rm -rf /tmp/noc-rb-{cid}", timeout=15)
            return {"applied": True, "change": cid, "configs": sorted(batch.touched)}
        time.sleep(5)
    try:
        noc.ssh(noc.ROUTER_HOST, f"sh /tmp/noc-rb-{cid}/restore.sh", timeout=40)
        how = "rolled back now"
    except Exception:
        how = f"the router will roll back by itself within {ROLLBACK_S} s"
    raise RuntimeError(f"after the change these WANs did not come back online: {', '.join(sorted(missing))}; {how}")


# ------------------------------------------------------------------ planners
def _cfg(noc):
    noc.invalidate("owcfg")
    c = noc.cached("owcfg", 10, lambda: read(noc))
    if "error" in c:
        raise RuntimeError(f"can't read the router config: {c['error']}")
    return c


def _device_fields(b, path, d, cur=None):
    """Write the editable device options (MAC, IPv6, MTU)."""
    cur = cur or {}
    mac = (d.get("macaddr") or "").strip()
    if mac and not MAC.fullmatch(mac):
        raise ValueError("MAC address must look like aa:bb:cc:dd:ee:ff")
    mtu = d.get("mtu")
    if mtu not in (None, ""):
        mtu = _int(mtu, 576, 9000, "MTU")
    ipv6 = _bool(d.get("ipv6"))
    b.opt(f"{path}.macaddr", mac)
    b.opt(f"{path}.mtu", mtu)
    b.set(f"{path}.ipv6", "1" if ipv6 else "0")
    if (cur.get("macaddr") or "") != mac:
        b.detail.append(f"MAC address: {cur.get('macaddr') or 'hardware default'} → {mac or 'hardware default'}")
    if cur.get("ipv6", True) != ipv6:
        b.detail.append(f"IPv6: {'on' if ipv6 else 'off'}")
    if str(cur.get("mtu") or "") != str(mtu or ""):
        b.detail.append(f"MTU: {cur.get('mtu') or 'default'} → {mtu or 'default'}")


def _new_device(c, b, d):
    """Create a VLAN (802.1q) or MAC VLAN device; returns its name."""
    typ = d.get("type")
    names = {x["name"] for x in c["devices"]}
    base = (d.get("base") or "").strip()
    if not DEV.fullmatch(base):
        raise ValueError("pick the base device")
    if typ == "8021q":
        vid = _int(d.get("vid"), 1, 4094, "VLAN ID")
        name = (d.get("name") or f"{base}.{vid}").strip()
    elif typ == "macvlan":
        vid = None
        name = (d.get("name") or "").strip()
    else:
        raise ValueError("device type must be VLAN (802.1q) or MAC VLAN")
    if not DEV.fullmatch(name):
        raise ValueError("device name: up to 15 letters, digits, . _ - (e.g. bond.200)")
    if name in names:
        raise ValueError(f"a device called {name} already exists")
    sec = "dev_" + re.sub(r"[^A-Za-z0-9_]", "_", name)
    path = f"network.{sec}"
    b.section(path, "device")
    b.set(f"{path}.type", typ)
    b.set(f"{path}.name", name)
    b.set(f"{path}.ifname", base)
    if typ == "8021q":
        b.set(f"{path}.vid", vid)
        b.detail.append(f"New device {name}: VLAN {vid} (802.1q) on {base}")
    else:
        b.set(f"{path}.mode", d.get("mode") or "vepa")
        b.detail.append(f"New device {name}: MAC VLAN on {base}")
    _device_fields(b, path, d)
    return name


def _iface_fields(c, b, i, cur=None):
    """Interface options per protocol. Unknown options already on the interface are kept."""
    cur = cur or {}
    name = cur.get("name") or i["name"]
    path = f"network.{name}"
    proto = i.get("proto")
    if proto not in ("static", "dhcp", "pppoe", "none"):
        raise ValueError("protocol must be static, DHCP client, PPPoE or unmanaged")
    dev = (i.get("device") or "").strip()
    known = {x["name"] for x in c["devices"]} | {x for d in c["devices"] for x in d["ports"]} | \
            {d["base"] for d in c["devices"] if d["base"]} | {"br-lan", "wan", "lan1", "lan2", "lan3", "lan4", "eth0", "eth1"}
    if dev and dev not in known and not re.fullmatch(r"(pppoe-|wan|lan|eth|bond|br-)[\w.-]*", dev):
        raise ValueError(f"unknown device {dev}")
    if not dev:
        raise ValueError("pick a device for the interface")
    b.set(f"{path}.proto", proto)
    b.set(f"{path}.device", dev)
    # protocol-specific options; switching protocol clears the old protocol's options
    if cur and cur.get("proto") != proto:
        for o in ("ipaddr", "netmask", "gateway", "hostname", "clientid", "username"):
            if cur.get(o):
                b.delete(f"{path}.{o}")
        if cur.get("password_set") and proto != "pppoe":
            b.delete(f"{path}.password")
    if proto == "static":
        addr = _ip((i.get("ipaddr") or "").strip(), "IPv4 address", cidr=True)
        b.delete(f"{path}.netmask")
        b.opt(f"{path}.ipaddr", addr)
        gw = (i.get("gateway") or "").strip()
        b.opt(f"{path}.gateway", _ip(gw, "Gateway") if gw else "")
    elif proto == "dhcp":
        host = (i.get("hostname") or "").strip()
        if host and not HOST.fullmatch(host):
            raise ValueError("hostname: letters, digits, . and -")
        b.opt(f"{path}.hostname", host)
        cl = (i.get("clientid") or "").strip()
        if cl and not re.fullmatch(r"[0-9A-Fa-f:]{2,128}", cl):
            raise ValueError("client ID must be hex")
        if "clientid" in i:
            b.opt(f"{path}.clientid", cl)
    elif proto == "pppoe":
        user = (i.get("username") or "").strip()
        if not user or not SAFE.fullmatch(user):
            raise ValueError("PPPoE needs a username (no quotes)")
        b.set(f"{path}.username", user)
        pw = i.get("password") or ""
        if pw:
            if not SAFE.fullmatch(pw):
                raise ValueError("password can't contain quotes, backslashes or line breaks")
            b.set(f"{path}.password", pw)
            b.detail.append("PPPoE password: will be set")
        elif not cur.get("password_set"):
            raise ValueError("PPPoE needs a password")
    dns = [x.strip() for x in (i.get("dns") or []) if x.strip()]
    for x in dns:
        _ip(x, "DNS server")
    if "dns" in i:
        b.opt(f"{path}.dns", dns)
    for o in ("metric",):
        if i.get(o) not in (None, ""):
            b.set(f"{path}.{o}", _int(i[o], 0, 9999, "metric"))
        elif o in i:
            b.delete(f"{path}.{o}")
    for o, default in (("defaultroute", True), ("peerdns", True), ("ipv6", True), ("auto", True)):
        if o in i:
            v = _bool(i[o])
            b.opt(f"{path}.{o}", "" if v == default else ("1" if v else "0"))
    if "ipv6" in i:
        b.opt(f"{path}.delegate", "" if _bool(i["ipv6"]) else "0")


def _zone_move(c, b, name, zone, cur_zone=None):
    if zone == cur_zone:
        return
    zid = {z["name"]: z["id"] for z in c["zones"]}
    if cur_zone in zid:
        b.del_list(f"firewall.{zid[cur_zone]}.network", name)
    if zone:
        if zone not in zid:
            raise ValueError(f"unknown firewall zone {zone}")
        b.add_list(f"firewall.{zid[zone]}.network", name)
    b.detail.append(f"Firewall zone: {cur_zone or 'none'} → {zone or 'none'}")


def _dhcp_server(c, b, name, ds, proto):
    cur = next((d for d in c["dhcp"] if d["interface"] == name), None)
    if not ds or proto != "static":
        if cur and not cur["ignore"] and ds is not None and not ds.get("enabled"):
            b.set(f"dhcp.{cur['id']}.ignore", "1")
            b.detail.append("DHCP server: off")
        return
    if not ds.get("enabled"):
        if cur and not cur["ignore"]:
            b.set(f"dhcp.{cur['id']}.ignore", "1")
            b.detail.append("DHCP server: off")
        return
    start, limit = _int(ds.get("start"), 2, 65000, "DHCP start"), _int(ds.get("limit"), 1, 65000, "DHCP limit")
    lease = (ds.get("leasetime") or "12h").strip()
    if not re.fullmatch(r"\d+[smhd]?|infinite", lease):
        raise ValueError("lease time like 12h, 30m or 1d")
    sec = cur["id"] if cur else re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not cur:
        b.section(f"dhcp.{sec}", "dhcp")
        b.set(f"dhcp.{sec}.interface", name)
    b.delete(f"dhcp.{sec}.ignore")
    b.set(f"dhcp.{sec}.start", start)
    b.set(f"dhcp.{sec}.limit", limit)
    b.set(f"dhcp.{sec}.leasetime", lease)
    b.set(f"dhcp.{sec}.dhcpv4", "server")
    dns = (ds.get("dns") or "").strip()
    if dns:
        _ip(dns, "DNS server for clients")
        b.delete(f"dhcp.{sec}.dhcp_option")
        b.add_list(f"dhcp.{sec}.dhcp_option", f"6,{dns}")
    b.detail.append(f"DHCP server: on, .{start} for {limit} addresses, lease {lease}" + (f", hands out DNS {dns}" if dns else ""))


def _mwan_iface_opts(b, name, m, new=False):
    path = f"mwan3.{name}"
    if new:
        b.section(path, "interface")
        b.set(f"{path}.family", "ipv4")
        b.set(f"{path}.track_method", "ping")
        b.set(f"{path}.size", "56")
        b.set(f"{path}.max_ttl", "60")
        b.set(f"{path}.initial_state", m.get("initial_state") or "online")
    track = [x.strip() for x in (m.get("track_ip") or []) if x.strip()]
    if not track:
        raise ValueError("mwan3 needs at least one tracking IP (e.g. 8.8.8.8)")
    for x in track:
        _ip(x, "tracking IP")
    b.set(f"{path}.enabled", "1" if _bool(m.get("enabled", True)) else "0")
    b.opt(f"{path}.track_ip", track)
    rel = _int(m.get("reliability", 1), 1, len(track), "reliability (must not exceed the number of tracking IPs)")
    b.set(f"{path}.reliability", rel)
    for o, lo, hi, d in (("count", 1, 5, 1), ("timeout", 1, 10, 4), ("interval", 1, 3600, 10),
                         ("failure_interval", 1, 3600, 5), ("recovery_interval", 1, 3600, 5), ("down", 1, 100, 5), ("up", 1, 100, 5)):
        b.set(f"{path}.{o}", _int(m.get(o, d), lo, hi, o.replace("_", " ")))
    if m.get("initial_state") in ("online", "offline") and not new:
        b.set(f"{path}.initial_state", m["initial_state"])


def _add_mwan(b, mw, name, m, member_metric=None):
    """Track a new interface in mwan3 and give it a member in the chosen policies."""
    if any(x["name"] == name for x in mw["interfaces"]):
        raise ValueError(f"mwan3 already has an interface {name}")
    _mwan_iface_opts(b, name, m, new=True)
    mem = (m.get("member_name") or f"m_{name}").strip()
    if not NAME.fullmatch(mem) or any(x["name"] == mem for x in mw["members"]):
        raise ValueError(f"mwan3 member name {mem} is invalid or taken")
    metric = _int(member_metric if member_metric is not None else m.get("metric", 1), 1, 256, "member metric")
    weight = _int(m.get("weight", 1), 1, 1000, "member weight")
    b.section(f"mwan3.{mem}", "member")
    b.set(f"mwan3.{mem}.interface", name)
    b.set(f"mwan3.{mem}.metric", metric)
    b.set(f"mwan3.{mem}.weight", weight)
    pols = {x["name"] for x in mw["policies"]}
    for pol in m.get("policies") or []:
        if pol not in pols:
            raise ValueError(f"unknown mwan3 policy {pol}")
        b.add_list(f"mwan3.{pol}.use_member", mem)
    b.detail.append(f"mwan3: track {', '.join(m.get('track_ip') or [])}, member {mem} (metric {metric}, weight {weight})"
                    + (f", in policies {', '.join(m.get('policies'))}" if m.get("policies") else ""))


def random_mac():
    """A locally administered, unicast MAC (stable once written to the config)."""
    raw = bytearray(secrets.token_bytes(6))
    raw[0] = (raw[0] & 0xFC) | 0x02
    return ":".join(f"{x:02x}" for x in raw)


def plan(noc, kind, p):
    """Returns (summary, detail, confirm_word, run, cache_keys) like plan_action."""
    c = _cfg(noc)
    b = Batch()
    devs = {d["name"]: d for d in c["devices"]}
    ifs = {i["name"]: i for i in c["interfaces"]}
    mw = c["mwan"]
    allow_offline = set()

    def done(summary, word=None):
        def run():
            r = apply(noc, b, allow_offline)
            noc.invalidate("owcfg", "router", "wanq")
            return r
        return summary, b.detail + [f"Applied with automatic rollback: if the router or a working WAN doesn't come back within "
                                    f"{ROLLBACK_S} s, the old config is restored"], word, run, ["router", "owcfg"]

    # ---------------- devices
    if kind == "ow_device_create":
        name = _new_device(c, b, p)
        return done(f"Create device {name} on the router")
    if kind == "ow_device_edit":
        d = next((x for x in c["devices"] if x["id"] == p.get("id")), None)
        if not d:
            raise ValueError("unknown device")
        _device_fields(b, f"network.{d['id']}", p, d)
        if not b.detail:
            raise ValueError("nothing changed")
        allow_offline |= {i["name"] for i in c["interfaces"] if i.get("device") == d["name"]}
        return done(f"Edit device {d['name']}")
    if kind == "ow_device_delete":
        d = next((x for x in c["devices"] if x["id"] == p.get("id")), None)
        if not d:
            raise ValueError("unknown device")
        if d["used_by"]:
            raise ValueError(f"{d['name']} is in use ({', '.join(d['used_by'])}); remove that first")
        b.delete(f"network.{d['id']}")
        b.detail.append(f"Delete device {d['name']} ({d['type']}{' VLAN ' + d['vid'] if d['vid'] else ''})")
        return done(f"Delete device {d['name']}")

    # ---------------- interfaces (and the wizard)
    if kind in ("ow_setup", "ow_iface_edit"):
        i = dict(p.get("interface") or {})
        editing = kind == "ow_iface_edit"
        if editing:
            cur = ifs.get(i.get("name"))
            if not cur:
                raise ValueError("unknown interface")
            allow_offline.add(cur["name"])
        else:
            cur = None
            if not NAME.fullmatch(i.get("name") or ""):
                raise ValueError("interface name: start with a letter, up to 15 letters, digits or _")
            if i["name"] in ifs:
                raise ValueError(f"an interface called {i['name']} already exists")
            dv = p.get("device") or {}
            if dv.get("mode") == "new":
                i["device"] = _new_device(c, b, dv)
                devs[i["device"]] = {"name": i["device"], "ports": [], "base": dv.get("base")}
                c["devices"].append(devs[i["device"]])
            b.section(f"network.{i['name']}", "interface")
            b.detail.append(f"New interface {i['name']}: {i.get('proto')} on {i.get('device')}")
        _iface_fields(c, b, i, cur)
        if editing:
            def norm(k, v):
                if k in ("defaultroute", "peerdns", "ipv6", "auto"):
                    return "on" if (v if isinstance(v, bool) else str(v if v is not None else "1") != "0") else "off"
                if isinstance(v, list):
                    return ", ".join(x for x in v if x) or "none"
                return str(v) if v not in (None, "") else "none"
            for k, v in i.items():
                if k in ("name", "password"):
                    continue
                old = cur.get(k, "1" if k in ("defaultroute", "peerdns", "ipv6", "auto") else None)
                if k == "ipaddr":
                    old = (cur.get("ipaddr") or [None])[0]
                if norm(k, old) != norm(k, v):
                    b.detail.append(f"{k}: {norm(k, old)} → {norm(k, v)}")
        name = i["name"]
        if "zone" in p:
            _zone_move(c, b, name, p.get("zone") or None, cur.get("zone") if cur else None)
        if "dhcp_server" in p:
            _dhcp_server(c, b, name, p.get("dhcp_server"), i.get("proto"))
        m = p.get("mwan")
        if m and m.get("enabled") and not editing:
            _add_mwan(b, mw, name, m)
            allow_offline.add(name)  # a brand-new WAN may take a while to come up
        if not b.detail:
            raise ValueError("nothing changed")
        word = name.lower() if editing and (cur.get("zone") == "lan" or name == noc.LAN_IFACE) else None
        return done(("Edit" if editing else "Create") + f" interface {name}" +
                    ("" if editing else " on the router"), word)

    if kind == "ow_vlan_cleanup":
        # Router objects that belong to a switch VLAN: the <trunk>.<vid> device, devices built on it
        # (e.g. MAC VLANs), the interfaces on any of those, and their mwan3 tracking/members.
        vid = str(_int(p.get("vlan"), 1, 4094, "VLAN ID"))
        rel = related_to_vlan(c, noc.TRUNK_DEV, vid)
        want_dev, want_if, want_mw = _bool(p.get("device")), _bool(p.get("interface")), _bool(p.get("mwan"))
        if want_dev and not rel["devices"]:
            raise ValueError(f"no router device for VLAN {vid}")
        if want_dev:
            want_if = True  # a device can't go while interfaces still use it
        if want_if and rel["mwan"]:
            want_mw = True  # mwan3 can't keep tracking an interface that no longer exists
        if want_mw:
            gone = set()
            for n in rel["mwan"]:
                b.delete(f"mwan3.{n}")
                b.detail.append(f"mwan3: stop tracking {n}")
            for mem in [x for x in mw["members"] if x["interface"] in rel["mwan"]]:
                b.delete(f"mwan3.{mem['name']}")
                gone.add(mem["name"])
                b.detail.append(f"mwan3: delete member {mem['name']}")
            for pol in mw["policies"]:
                hit = [x for x in pol["use_member"] if x in gone]
                for mem in hit:
                    b.del_list(f"mwan3.{pol['name']}.use_member", mem)
                if hit:
                    left = [x for x in pol["use_member"] if x not in gone]
                    b.detail.append(f"mwan3: remove {', '.join(hit)} from policy {pol['name']}"
                                    + ("" if left else f" ⚠ the policy is left empty (last resort: {pol['last_resort']})"))
        if want_if:
            for name in rel["interfaces"]:
                cur = ifs[name]
                if name == noc.LAN_IFACE:
                    raise ValueError("the LAN interface can't be deleted from the NOC")
                b.delete(f"network.{name}")
                b.detail.append(f"Delete interface {name} ({cur.get('proto')} on {cur.get('device')})")
                if cur.get("zone"):
                    z = next(z for z in c["zones"] if z["name"] == cur["zone"])
                    b.del_list(f"firewall.{z['id']}.network", name)
                for d in c["dhcp"]:
                    if d["interface"] == name:
                        b.delete(f"dhcp.{d['id']}")
                        b.detail.append(f"Delete DHCP settings of {name}")
                allow_offline.add(name)
        if want_dev:
            for d in rel["devices"][::-1]:  # MAC VLANs first, then the VLAN device under them
                outside = [u for u in d["used_by"] if not any(u == f"interface {i}" for i in rel["interfaces"])
                           and not any(u == f"base of {x['name']}" for x in rel["devices"])]
                if outside:
                    raise ValueError(f"{d['name']} is also used by {', '.join(outside)}; leave the device")
                b.delete(f"network.{d['id']}")
                b.detail.append(f"Delete device {d['name']}")
        if not b.detail:
            raise ValueError("nothing selected to remove on the router")
        allow_offline |= set(rel["mwan"])
        return done(f"Remove VLAN {vid} from the router")

    if kind == "ow_macvlan_bulk":
        base = (p.get("base") or "").strip()
        known = {d["name"] for d in c["devices"]} | {x for d in c["devices"] for x in d["ports"]}
        if base not in known:
            raise ValueError("pick the device the MAC VLANs sit on")
        rows = p.get("rows") or []
        if not 1 <= len(rows) <= 16:
            raise ValueError("create 1 to 16 MAC VLANs at a time")
        mode = p.get("mode") or "vepa"
        if mode not in ("vepa", "bridge", "private", "passthru"):
            raise ValueError("mode must be vepa, bridge, private or passthru")
        make_if = _bool(p.get("interfaces"))
        proto = p.get("proto") or "pppoe"
        dev_names, if_names = set(), set()
        for n, r in enumerate(rows, 1):
            dname = (r.get("dev_name") or "").strip()
            if dname in dev_names:
                raise ValueError(f"row {n}: device name {dname} is used twice")
            dev_names.add(dname)
            mac = random_mac() if p.get("mac") == "random" else (r.get("macaddr") or "").strip()
            name = _new_device(c, b, {"type": "macvlan", "base": base, "name": dname, "mode": mode, "macaddr": mac,
                                      "ipv6": _bool(p.get("ipv6"))})
            c["devices"].append({"name": name, "ports": [], "base": base})
            if not make_if:
                continue
            ifname = (r.get("ifname") or "").strip()
            if not NAME.fullmatch(ifname):
                raise ValueError(f"row {n}: interface name must start with a letter, up to 15 letters, digits or _")
            if ifname in ifs or ifname in if_names:
                raise ValueError(f"row {n}: an interface called {ifname} already exists")
            if_names.add(ifname)
            i = {"name": ifname, "proto": proto, "device": name, "ipv6": _bool(p.get("ipv6")), "auto": True}
            if proto != "none":
                i["metric"] = r.get("metric")
                if p.get("dns"):
                    i["dns"] = p.get("dns")
            if proto == "static":
                i["ipaddr"] = (r.get("ipaddr") or "").strip()
                i["gateway"] = (r.get("gateway") or "").strip()
            if proto == "dhcp":
                i["hostname"] = p.get("hostname") or ""
            if proto == "pppoe":
                i["username"], i["password"] = p.get("username"), p.get("password") or ""
            if proto in ("dhcp", "pppoe"):
                i["defaultroute"], i["peerdns"] = p.get("defaultroute", True), p.get("peerdns", True)
            b.section(f"network.{ifname}", "interface")
            detail_at = len(b.detail)
            _iface_fields(c, b, i)
            b.detail[detail_at:] = [x for x in b.detail[detail_at:] if not x.startswith("PPPoE password")]
            b.detail.append(f"  interface {ifname}: {proto} on {name}" + (f", metric {r.get('metric')}" if r.get("metric") not in (None, "") else "")
                            + (f", {i.get('ipaddr')}" if proto == "static" else "") + (f" via {i.get('gateway')}" if i.get("gateway") else ""))
            if p.get("zone"):
                _zone_move(c, b, ifname, p["zone"])
                b.detail.pop()
            m = p.get("mwan") or {}
            if m.get("enabled"):
                _add_mwan(b, mw, ifname, m)
                b.detail[-1] = "    " + b.detail[-1]
            allow_offline.add(ifname)
        if make_if:
            if p.get("zone"):
                b.detail.append(f"All in firewall zone {p['zone']}")
            if proto == "pppoe" and p.get("password"):
                b.detail.append("PPPoE password: will be set on each interface")
        mac_note = {"random": "a fixed random MAC each (stays the same across reboots)"}.get(p.get("mac"), "MACs as entered / kernel default")
        b.detail.insert(0, f"{len(rows)} MAC VLAN device(s) on {base} ({mode} mode), {mac_note}")
        return done(f"Create {len(rows)} MAC VLAN device(s) on {base}" + (" with interfaces" if make_if else ""))

    if kind == "ow_iface_delete":
        name = p.get("name")
        cur = ifs.get(name)
        if not cur:
            raise ValueError("unknown interface")
        if name in (noc.LAN_IFACE, "loopback"):
            raise ValueError("the LAN interface can't be deleted from the NOC")
        b.delete(f"network.{name}")
        b.detail.append(f"Delete interface {name} ({cur.get('proto')} on {cur.get('device')})")
        if cur.get("zone"):
            z = next(z for z in c["zones"] if z["name"] == cur["zone"])
            b.del_list(f"firewall.{z['id']}.network", name)
            b.detail.append(f"Remove it from firewall zone {cur['zone']}")
        for d in c["dhcp"]:
            if d["interface"] == name:
                b.delete(f"dhcp.{d['id']}")
                b.detail.append("Remove its DHCP settings")
        if any(x["name"] == name for x in mw["interfaces"]):
            b.delete(f"mwan3.{name}")
            b.detail.append(f"Remove mwan3 interface {name}")
        for mem in [x for x in mw["members"] if x["interface"] == name]:
            b.delete(f"mwan3.{mem['name']}")
            b.detail.append(f"Remove mwan3 member {mem['name']}")
            for pol in mw["policies"]:
                if mem["name"] in pol["use_member"]:
                    b.del_list(f"mwan3.{pol['name']}.use_member", mem["name"])
                    b.detail.append(f"Remove {mem['name']} from policy {pol['name']}")
        allow_offline.add(name)
        return done(f"Delete interface {name}", word=name.lower())

    # ---------------- mwan3
    if kind == "ow_mwan_iface":
        name = p.get("name")
        new = not any(x["name"] == name for x in mw["interfaces"])
        if new and name not in ifs:
            raise ValueError("mwan3 interfaces must match a router interface")
        _mwan_iface_opts(b, name, p, new=new)
        old = next((x for x in mw["interfaces"] if x["name"] == name), {})
        for k in ("enabled", "reliability", "count", "timeout", "interval", "failure_interval", "recovery_interval", "down", "up"):
            if not new and str(old.get(k, "")) != str(p.get(k, old.get(k, ""))) and k in p:
                b.detail.append(f"{k.replace('_', ' ')}: {old.get(k)} → {p.get(k)}")
        if not new and as_list(old.get("track_ip")) != [x.strip() for x in p.get("track_ip") or [] if x.strip()]:
            b.detail.append(f"tracking IPs: {', '.join(as_list(old.get('track_ip')))} → {', '.join(p.get('track_ip') or [])}")
        if new:
            b.detail.append(f"New mwan3 interface {name}, tracking {', '.join(p.get('track_ip') or [])}")
            if ifs[name].get("zone") != "wan":
                b.detail.append(f"⚠ {name} is in firewall zone '{ifs[name].get('zone') or 'none'}', not 'wan'; "
                                "traffic sent out of it is only NATed if that zone masquerades")
            m = p.get("member")
            if m:  # optional: give the new interface a member and put it in policies, like the wizard
                mem = (m.get("name") or f"m_{name}").strip()
                if not NAME.fullmatch(mem) or any(x["name"] == mem for x in mw["members"]):
                    raise ValueError(f"member name {mem} is invalid or already used")
                metric, weight = _int(m.get("metric", 1), 1, 256, "member metric"), _int(m.get("weight", 1), 1, 1000, "member weight")
                b.section(f"mwan3.{mem}", "member")
                b.set(f"mwan3.{mem}.interface", name)
                b.set(f"mwan3.{mem}.metric", metric)
                b.set(f"mwan3.{mem}.weight", weight)
                pols = {x["name"] for x in mw["policies"]}
                for pol in m.get("policies") or []:
                    if pol not in pols:
                        raise ValueError(f"unknown mwan3 policy {pol}")
                    b.add_list(f"mwan3.{pol}.use_member", mem)
                b.detail.append(f"New member {mem}: metric {metric}, weight {weight}"
                                + (f", in policies {', '.join(m.get('policies'))}" if m.get("policies") else ", not in any policy yet"))
        if not b.detail:
            raise ValueError("nothing changed")
        allow_offline.add(name)
        return done(("Add" if new else "Edit") + f" mwan3 interface {name}")

    if kind == "ow_mwan_iface_delete":
        name = p.get("name")
        if not any(x["name"] == name for x in mw["interfaces"]):
            raise ValueError("unknown mwan3 interface")
        b.delete(f"mwan3.{name}")
        b.detail.append(f"Stop tracking {name} in mwan3 (the router interface itself stays)")
        gone = {x["name"] for x in mw["members"] if x["interface"] == name}
        for mem in sorted(gone):
            b.delete(f"mwan3.{mem}")
            b.detail.append(f"Delete its member {mem}")
        for pol in mw["policies"]:
            hit = [x for x in pol["use_member"] if x in gone]
            for mem in hit:
                b.del_list(f"mwan3.{pol['name']}.use_member", mem)
            if hit:
                left = [x for x in pol["use_member"] if x not in gone]
                b.detail.append(f"Remove {', '.join(hit)} from policy {pol['name']}"
                                + ("" if left else " ⚠ the policy is left with no members: its traffic falls to the last resort ("
                                   f"{pol['last_resort']})"))
        allow_offline.add(name)
        return done(f"Remove mwan3 interface {name}", word=name.lower())

    if kind == "ow_mwan_member":
        orig = p.get("original_name")
        name = (p.get("name") or "").strip()
        if not NAME.fullmatch(name):
            raise ValueError("member name: start with a letter, up to 15 letters, digits or _")
        if p.get("interface") not in {x["name"] for x in mw["interfaces"]}:
            raise ValueError("pick an mwan3 interface")
        metric, weight = _int(p.get("metric"), 1, 256, "metric"), _int(p.get("weight"), 1, 1000, "weight")
        old = next((x for x in mw["members"] if x["name"] == orig), None)
        if (not old or orig != name) and any(x["name"] == name for x in mw["members"]):
            raise ValueError(f"member {name} already exists")
        if old and orig != name:
            raise ValueError("renaming a member isn't supported; create a new one and delete the old")
        b.section(f"mwan3.{name}", "member")
        b.set(f"mwan3.{name}.interface", p["interface"])
        b.set(f"mwan3.{name}.metric", metric)
        b.set(f"mwan3.{name}.weight", weight)
        b.detail.append(f"{'Edit' if old else 'New'} member {name}: {p['interface']}, metric {metric}, weight {weight}"
                        + (f" (was {old['interface']}, metric {old['metric']}, weight {old['weight']})" if old else ""))
        return done(("Edit" if old else "Add") + f" mwan3 member {name}")

    if kind == "ow_mwan_member_delete":
        name = p.get("name")
        if not any(x["name"] == name for x in mw["members"]):
            raise ValueError("unknown member")
        b.delete(f"mwan3.{name}")
        b.detail.append(f"Delete member {name}")
        for pol in mw["policies"]:
            if name in pol["use_member"]:
                b.del_list(f"mwan3.{pol['name']}.use_member", name)
                b.detail.append(f"Remove it from policy {pol['name']}")
        return done(f"Delete mwan3 member {name}")

    if kind == "ow_mwan_policy":
        orig = p.get("original_name")
        name = (p.get("name") or "").strip()
        if not NAME.fullmatch(name):
            raise ValueError("policy name: start with a letter, up to 15 letters, digits or _")
        old = next((x for x in mw["policies"] if x["name"] == orig), None)
        if old and orig != name:
            raise ValueError("renaming a policy isn't supported; create a new one, move the rules, delete the old")
        if not old and any(x["name"] == name for x in mw["policies"]):
            raise ValueError(f"policy {name} already exists")
        members = [m for m in p.get("use_member") or []]
        known = {x["name"] for x in mw["members"]}
        if not members or any(m not in known for m in members):
            raise ValueError("pick at least one existing member")
        lr = p.get("last_resort") or "unreachable"
        if lr not in ("unreachable", "blackhole", "default"):
            raise ValueError("last resort must be unreachable, blackhole or default")
        b.section(f"mwan3.{name}", "policy")
        b.opt(f"mwan3.{name}.use_member", members)
        b.set(f"mwan3.{name}.last_resort", lr)
        b.detail.append(f"{'Edit' if old else 'New'} policy {name}: {', '.join(members)}; last resort {lr}"
                        + (f" (was {', '.join(old['use_member'])}; {old['last_resort']})" if old else ""))
        return done(("Edit" if old else "Add") + f" mwan3 policy {name}")

    if kind == "ow_mwan_policy_delete":
        name = p.get("name")
        if not any(x["name"] == name for x in mw["policies"]):
            raise ValueError("unknown policy")
        users = [r["name"] for r in mw["rules"] if r.get("use_policy") == name]
        if users:
            raise ValueError(f"rules {', '.join(users)} use this policy; change or delete them first")
        b.delete(f"mwan3.{name}")
        b.detail.append(f"Delete policy {name}")
        return done(f"Delete mwan3 policy {name}")

    if kind == "ow_mwan_rule":
        orig = p.get("original_name")
        name = (p.get("name") or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", name):
            raise ValueError("rule name: start with a letter; letters, digits, _")
        old = next((x for x in mw["rules"] if x["name"] == orig), None)
        if old and orig != name:
            raise ValueError("renaming a rule isn't supported; create a new one and delete the old")
        if not old and any(x["name"] == name for x in mw["rules"]):
            raise ValueError(f"rule {name} already exists")
        pol = p.get("use_policy")
        if pol not in {x["name"] for x in mw["policies"]} | {"default"}:
            raise ValueError("pick a policy")
        proto = p.get("proto") or "all"
        if proto not in ("all", "tcp", "udp", "icmp", "esp"):
            raise ValueError("protocol must be all, tcp, udp, icmp or esp")
        path = f"mwan3.{name}"
        b.section(path, "rule")
        b.set(f"{path}.family", "ipv4")
        b.set(f"{path}.proto", proto)
        for k in ("src_ip", "dest_ip"):
            v = (p.get(k) or "").strip()
            if v:
                try:
                    ipaddress.IPv4Network(v, strict=False)
                except ValueError:
                    raise ValueError(f"{k.replace('_', ' ')} must be an IPv4 address or network, e.g. 192.0.2.5/32") from None
            b.opt(f"{path}.{k}", v)
        for k in ("src_port", "dest_port"):
            v = (p.get(k) or "").strip()
            if v and (proto not in ("tcp", "udp") or not re.fullmatch(r"\d{1,5}([-:,]\d{1,5})*", v)):
                raise ValueError(f"{k.replace('_', ' ')} needs protocol tcp or udp and looks like 443 or 1000-2000")
            b.opt(f"{path}.{k}", v)
        sticky = _bool(p.get("sticky"))
        b.set(f"{path}.sticky", "1" if sticky else "0")
        if sticky:
            b.set(f"{path}.timeout", _int(p.get("timeout") or 600, 1, 1000000, "sticky timeout"))
        b.set(f"{path}.use_policy", pol)
        src, dst = p.get("src_ip") or "any", p.get("dest_ip") or "any"
        b.detail.append(f"{'Edit' if old else 'New'} rule {name}: {proto} from {src} to {dst} → policy {pol}"
                        + (" (sticky)" if sticky else ""))
        if not old:
            b.detail.append("New rules go last; move them up so they match before broader rules")
        return done(("Edit" if old else "Add") + f" mwan3 rule {name}")

    if kind == "ow_mwan_rule_delete":
        name = p.get("name")
        if not any(x["name"] == name for x in mw["rules"]):
            raise ValueError("unknown rule")
        b.delete(f"mwan3.{name}")
        b.detail.append(f"Delete rule {name}")
        return done(f"Delete mwan3 rule {name}")

    if kind == "ow_mwan_rule_move":
        rules = [r["name"] for r in mw["rules"]]
        name, step = p.get("name"), _int(p.get("step"), -1, 1, "step")
        if name not in rules or step == 0:
            raise ValueError("unknown rule")
        i = rules.index(name)
        j = i + step
        if not 0 <= j < len(rules):
            raise ValueError("already at the edge")
        rules[i], rules[j] = rules[j], rules[i]
        u = parse_uci(noc.ssh(noc.ROUTER_HOST, "uci -X show mwan3", timeout=15)).get("mwan3", {})
        first = min(u[r][".index"] for r in rules)
        for n, r in enumerate(rules):
            b.lines.append(f"reorder mwan3.{r}={first + n}")
        b.touched.add("mwan3")
        b.detail.append(f"Move rule {name} {'up' if step < 0 else 'down'}: order becomes {', '.join(rules)}")
        return done(f"Reorder mwan3 rules")

    raise ValueError("unknown router action")
