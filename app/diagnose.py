"""Find out why a WAN link is down by walking its path layer by layer.

Layers, in the order traffic depends on them:
  1. Router config   interface exists, not disabled, not stopped by hand
  2. Router link     the interface's device chain (VLAN -> bond -> member ports, or
                     macvlan -> physical port) has carrier
  3. Switch          LAG and ISP-facing port enabled, linked, carrying the WAN's VLAN
  4. ISP box         the ONT/ONU answers on its management address
  5. Fibre           no LOS/LOF, registered with the OLT (O5), light above the alarm level
  6. ISP session     DHCP lease / PPPoE session up (with the router's own error code)
  7. Beyond the ISP  the ISP gateway and the mwan3 track IP answer through this link

The first layer that fails is reported as the cause; later layers usually fail as a
consequence. Which switch port, ISP box and ONT belong to a WAN comes from
config.json router.wan_paths, keyed by WAN group (or interface name).
"""
import re
import shlex
import time

ROUTER_SH = r"""
IF=__IF__
printf '\n@@STATUS\n'; ubus call network.interface.$IF status 2>/dev/null || echo '{}'
printf '\n@@CFG\n'
echo "disabled=$(uci -q get network.$IF.disabled)"
echo "proto=$(uci -q get network.$IF.proto)"
echo "device=$(uci -q get network.$IF.device)"
echo "mwan=$(uci -q get mwan3.$IF.enabled)"
echo "track=$(uci -q get mwan3.$IF.track_ip | cut -d' ' -f1)"
printf '\n@@LINKS\n'
d=$(uci -q get network.$IF.device); chain=""
while [ -n "$d" ] && [ -e "/sys/class/net/$d" ]; do
  chain="$chain $d"
  # Stop at a bond (its member ports are listed below) and at a switch port: the next
  # "lower" device would be the switch chip's CPU conduit (has a dsa/ dir), not a cable.
  [ -d "/sys/class/net/$d/bonding" ] && break
  l=$(ls -d /sys/class/net/$d/lower_* 2>/dev/null | head -n1); [ -n "$l" ] || break
  d=${l##*/lower_}
  [ -d "/sys/class/net/$d/dsa" ] && break
done
[ -n "$chain" ] || echo "MISSING $(uci -q get network.$IF.device)"
for d in $chain; do echo "DEV $d $(cat /sys/class/net/$d/operstate 2>/dev/null) $(cat /sys/class/net/$d/carrier 2>/dev/null)"; done
last=${chain##* }
if [ -n "$last" ] && [ -f "/sys/class/net/$last/bonding/slaves" ]; then
  for s in $(cat /sys/class/net/$last/bonding/slaves); do echo "SLAVE $s $(cat /sys/class/net/$s/operstate 2>/dev/null) $(cat /sys/class/net/$s/carrier 2>/dev/null)"; done
fi
printf '\n@@LOG\n'
logread 2>/dev/null | grep -F -e "'$IF'" -e " $IF (" | grep -viE 'user|pass|secret' | tail -n 15
printf '\n@@PING\n'
S=$(ubus call network.interface.$IF status 2>/dev/null)
L3=$(echo "$S" | jsonfilter -e '@.l3_device' 2>/dev/null)
GW=$(echo "$S" | jsonfilter -e "@.route[@.target='0.0.0.0'].nexthop" 2>/dev/null | head -n1)
T=$(uci -q get mwan3.$IF.track_ip | cut -d' ' -f1)
# Send through this link the way mwan3track does: `ping -I dev` alone is routed by the
# main table on a multi-WAN router and fails for every link but the default one.
USE=""; command -v mwan3 >/dev/null 2>&1 && USE="mwan3 use $IF"
if [ -n "$L3" ] && [ -n "$GW" ]; then $USE ping -c 2 -W 1 -I "$L3" "$GW" >/dev/null 2>&1 && echo "gw ok $GW" || echo "gw fail $GW"; fi
if [ -n "$L3" ] && [ -n "$T" ]; then $USE ping -c 2 -W 2 -I "$L3" "$T" >/dev/null 2>&1 && echo "track ok $T" || echo "track fail $T"; fi
echo "mwan $(ubus call mwan3 status 2>/dev/null | jsonfilter -e "@.interfaces.$IF.status" 2>/dev/null)"
"""

# pppd exit reasons as netifd records them (pppd(8) EXIT STATUS).
PPP_ERRORS = {
    "AUTH_TOPEER_FAILED": "the ISP rejected the PPPoE login (wrong username/password, account suspended, or too many sessions)",
    "PEER_AUTH_FAILED": "PPPoE authentication failed",
    "CONNECT_FAILED": "no answer to PPPoE discovery (no PADO): the ISP side or the ONU isn't passing PPPoE",
    "PEER_DEAD": "the ISP stopped answering keep-alives (LCP echo) and the session was dropped",
    "HANGUP": "the ISP hung up the session",
    "NEGOTIATION_FAILED": "PPP negotiation with the ISP failed",
    "OPTION_ERROR": "the router's PPPoE settings are invalid",
}


def sections(text):
    out, cur = {}, None
    for line in text.splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def diagnose(noc, iface):
    if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", iface or ""):
        raise ValueError("invalid interface name")
    group = noc.wan_group_of(iface)
    path = (noc.ROUTER.get("wan_paths") or {}).get(group or "", {}) or (noc.ROUTER.get("wan_paths") or {}).get(iface, {})
    steps = []

    def step(layer, ok, detail):
        steps.append({"layer": layer, "ok": ok, "detail": detail})

    def result():
        cause = next((s for s in steps if s["ok"] is False), None)
        if cause:
            seen = False
            for s in steps:  # layers above the cause are expected to fail with it
                if s is cause:
                    seen = True
                elif seen and s["ok"] is False:
                    s["consequence"] = True
        return {"iface": iface, "group": group, "ts": time.time(), "steps": steps,
                "layer": cause["layer"] if cause else None,
                "verdict": f"{cause['layer']}: {cause['detail']}" if cause else
                "No fault found right now: every layer checks out (it may have just recovered, or the problem is intermittent)."}

    # ---- router data (one SSH round trip)
    try:
        sec = sections(noc.ssh(noc.ROUTER_HOST, ROUTER_SH.replace("__IF__", shlex.quote(iface)), timeout=25))
    except Exception as ex:
        step("Router", False, f"the NOC can't reach the router over SSH ({ex}); nothing behind it can be checked")
        return result()
    st = noc.jload(sec.get("STATUS"), {}) or {}
    cfg = dict(line.split("=", 1) for line in sec.get("CFG", "").splitlines() if "=" in line)
    log = [line for line in sec.get("LOG", "").splitlines() if line.strip()]
    up, proto = bool(st.get("up")), st.get("proto") or cfg.get("proto")

    # 1. router config
    last_state = next((line for line in reversed(log) if f"Interface '{iface}' is" in line), "")
    if not st:
        step("Router config", False, f"interface '{iface}' doesn't exist on the router")
    elif cfg.get("disabled") == "1":
        step("Router config", False, f"interface '{iface}' is disabled in the router config (network.{iface}.disabled=1)")
    elif not up and (st.get("autostart") is False or last_state.endswith("is disabled")):
        when = " at " + last_state[11:19] if len(last_state) > 19 else ""
        step("Router config", False, f"interface '{iface}' was stopped on the router{when} (ifdown / LuCI Stop) and won't come back by itself; /restart {iface} starts it")
    else:
        step("Router config", True, f"'{iface}' enabled ({proto})" + ("; mwan3 tracking is disabled for it" if cfg.get("mwan") == "0" else ""))

    # 2. router link: walk the device chain from the physical end
    devs, slaves, missing = [], [], None
    for line in sec.get("LINKS", "").splitlines():
        p = line.split()
        if p and p[0] == "DEV":
            devs.append({"name": p[1], "oper": p[2] if len(p) > 2 else "?", "carrier": p[3] if len(p) > 3 else "?"})
        elif p and p[0] == "SLAVE":
            slaves.append({"name": p[1], "oper": p[2] if len(p) > 2 else "?", "carrier": p[3] if len(p) > 3 else "?"})
        elif p and p[0] == "MISSING":
            missing = p[1] if len(p) > 1 else "?"
    box = path.get("isp_device")
    if missing:
        step("Router link", False, f"device {missing} doesn't exist on the router")
    elif devs:
        phys = devs[-1]
        chain = " → ".join(d["name"] for d in devs)
        if slaves:
            down = [s["name"] for s in slaves if s["carrier"] != "1"]
            if len(down) == len(slaves):
                step("Router link", False, f"the router↔switch LAG ({phys['name']}: {', '.join(down)}) has no link on any cable")
            else:
                step("Router link", True, f"{chain} (LAG members: " + ", ".join(f"{s['name']} {'✓' if s['carrier'] == '1' else '✗'}" for s in slaves) + ")"
                     + (f"; running on one cable, {', '.join(down)} is down" if down else ""))
        elif phys["carrier"] != "1":
            step("Router link", False, f"no cable link on router port {phys['name']}" + (f": check {box} power and its cable" if box else ""))
        else:
            step("Router link", True, f"{chain}, link up")
        if steps[-1]["ok"] and any(d["oper"] not in ("up", "unknown") for d in devs[:-1]) and up is False:
            bad = next(d for d in devs[:-1] if d["oper"] not in ("up", "unknown"))
            steps[-1].update(ok=False, detail=f"router device {bad['name']} is {bad['oper']}")

    # 3. switch: LAG to the router and the ISP-facing port
    port_no = path.get("switch_port")
    vlan = next((int(m.group(1)) for d in devs if (m := re.fullmatch(rf"{re.escape(noc.TRUNK_DEV)}\.(\d+)", d["name"]))), None)
    if port_no or (vlan and slaves):
        sw = noc.cached("switch", 20, noc.collect_switch)
        if not isinstance(sw, dict) or "error" in sw:
            step("Switch", None, "Omada controller not reachable; switch not checked")
        else:
            net = next((n for n in sw.get("networks", []) if n.get("vlan") == vlan), None) if vlan else None
            nid = net["id"] if net else None
            problems, notes = [], []
            for lag in sw.get("lags", []):
                if lag.get("disabled"):
                    problems.append(f"LAG {lag['lag']} is disabled in Omada")
                elif not lag.get("members_up"):
                    problems.append(f"LAG {lag['lag']} has no links up")
                elif nid and nid not in lag["untag_ids"] + lag["tag_ids"]:
                    problems.append(f"VLAN {vlan} is not carried on LAG {lag['lag']} (switch↔router)")
            if vlan and not net:
                problems.append(f"VLAN {vlan} doesn't exist in Omada")
            if port_no:
                p = next((x for x in sw.get("ports", []) if x["port"] == int(port_no)), None)
                if not p:
                    problems.append(f"switch port {port_no} not found")
                elif p.get("disabled"):
                    problems.append(f"switch port {port_no} is disabled in Omada")
                elif not p.get("link"):
                    problems.append(f"no link on switch port {port_no}" + (f": {box} is powered off, unplugged or dead"
                                                                          + (" (an SFP stick may need reseating)" if p.get("sfp") else "") if box else ""))
                elif nid and nid not in p["untag_ids"] + p["tag_ids"] + [p.get("native_id")]:
                    problems.append(f"VLAN {vlan} is not on switch port {port_no}")
                else:
                    notes.append(f"port {port_no} link up ({p.get('speed')})")
            if nid and not problems:
                notes.append(f"VLAN {vlan} on the LAG" + (f" and port {port_no}" if port_no else ""))
            step("Switch", False if problems else True, "; ".join(problems or notes) or "OK")

    # 4. ISP box management
    if box:
        devs_live = noc.cached("ping", 10, noc.collect_ping)
        b = next((d for d in devs_live if d.get("name") == box), None) if isinstance(devs_live, list) else None
        if b is None:
            step("ISP box", None, f"{box} isn't a tracked device; not checked")
        elif b["up"]:
            step("ISP box", True, f"{box} answers on {b['ip']}")
        else:
            step("ISP box", False, f"{box} doesn't answer on its management address {b['ip']}: powered off or hung (power-cycle it)")

    # 5. fibre
    ont_name = path.get("ont")
    if ont_name:
        r = noc.cached("router", 15, noc.collect_router)
        o = next((x for x in (r.get("onts", []) if isinstance(r, dict) else []) if x["name"] == ont_name), None)
        stale = (noc.C.get("telegram") or {}).get("ont_stale_s", 180)
        if not o or not o.get("connected") or o.get("age_s") is None or o["age_s"] > stale:
            step("Fibre", None, f"no fresh optical reading from {ont_name}" + (f" (last {int(o['age_s'])} s ago)" if o and o.get("age_s") is not None else ""))
        else:
            lim = o.get("limits") or {}
            alarms = [k.upper() for k, v in (o.get("alarms") or {}).items() if v and not str(v).lower().startswith("clear")]
            rx, low = o.get("rx_dbm"), lim.get("rx_pwr_low_alarm")
            if "LOS" in alarms or rx is None:
                step("Fibre", False, f"no light on the fibre (LOS) at {ont_name}: fibre unplugged, bent or cut, or an ISP outage in your area")
            elif o.get("state_code") not in (None, "O5"):
                step("Fibre", False, f"{ont_name} has light (RX {rx} dBm) but isn't registered with the ISP (state {o.get('state') or o.get('state_code')}): ISP side or authorisation")
            elif low is not None and rx < low:
                step("Fibre", False, f"light too weak at {ont_name}: RX {rx} dBm, below the {low} dBm alarm level (dirty/bent fibre or bad splice)")
            elif alarms:
                step("Fibre", False, f"{ont_name} reports alarms {', '.join(alarms)}")
            else:
                step("Fibre", True, f"{ont_name}: RX {rx} dBm, registered ({o.get('state_code') or 'O5'})")

    # 6. ISP session
    addr = (st.get("ipv4-address") or [{}])[0].get("address")
    if st and cfg.get("disabled") != "1":
        errs = [e.get("code") for e in (st.get("errors") or []) if e.get("code")]
        if up and addr:
            step("ISP session", True, f"{proto} up, address {addr}")
        elif proto == "pppoe":
            known = [PPP_ERRORS.get(c, c) for c in errs]
            step("ISP session", False, "PPPoE session is down: " + ("; ".join(known) if known else "no error reported yet (still trying)"))
        elif proto == "dhcp":
            since_start = log[max((i for i, l in enumerate(log) if "udhcpc: started" in l), default=0):]
            asking = any("broadcasting discover" in l for l in since_start)
            got = any("lease of" in l for l in since_start)
            step("ISP session", False, "no DHCP lease: the ISP's DHCP server isn't answering" if asking and not got
                 else "no IPv4 address on the interface")
        else:
            step("ISP session", False, f"{proto} interface is not up")

    # 7. beyond the ISP
    pings = dict((p.split()[0], p.split()) for p in sec.get("PING", "").splitlines() if p.split())
    if up and addr:
        gw, tr = pings.get("gw"), pings.get("track")
        mwan = (pings.get("mwan") or ["", ""])[1:2] or [""]
        gw_ok, tr_ok = bool(gw and gw[1] == "ok"), bool(tr and tr[1] == "ok")
        gw_note = "" if gw_ok or not gw else f" (gateway {gw[2]} ignores ping, which many ISPs do)"
        if tr_ok:
            step("Beyond the ISP", True, f"{tr[2]} answers through this link" + gw_note)
        elif mwan[0] == "online":
            step("Beyond the ISP", None, f"test ping to {tr[2] if tr else 'the track IP'} failed but mwan3 reports the link online; retry /diagnose in a minute")
        elif tr and gw_ok:
            step("Beyond the ISP", False, f"the ISP gateway {gw[2]} answers but {tr[2]} doesn't: the ISP has no internet upstream right now")
        elif tr:
            step("Beyond the ISP", False, f"nothing answers through this link ({tr[2]}" + (f", gateway {gw[2]}" if gw else "")
                 + "): ISP-side outage beyond your connection")
    return result()


def short(d):
    """One line for alerts: the cause plus a ✓/✗ trail of the layers checked."""
    trail = " · ".join(f"{s['layer']} {'✓' if s['ok'] else '✗' if s['ok'] is False else '?'}" for s in d["steps"])
    return d["verdict"], trail
