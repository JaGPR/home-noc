"""Data used per WAN, per day and per month, counted all the time (not only while the Speed tab is open).

Hardware flow offload hides forwarded traffic from the WAN devices' own byte counters, so they can't be used
(they read about 0 while gigabits pass). Like the Speed tab's load, this counts the connection table instead:
every INTERVAL_S it reads it (speedtab.LOAD_SCRIPT) and adds each connection's new bytes to its WAN, by the
connection's address on the router's side (a WAN's IPv6 sibling wanN_6 counts as wanN).

A router restart loses nothing: the totals live on the NOC, and the first read after it (the router's uptime went
back) counts every connection in full, since all of them began after the boot.

What it can miss: a connection that opens and closes between two reads, and is gone from the table by the next.
Closed TCP connections stay in the table for about 2 minutes (TIME_WAIT) and UDP ones 30 s or more, so reads
every 30 s see nearly all of them. When the NOC was stopped, nothing is counted for that time (the page says
since when it has been counting).

Per device too: each connection's LAN device (its MAC where the router knows it, so its IPv4 and IPv6 add up),
per WAN per month, and per day for the daily summary.

Alerts on Telegram (config "usage"): a cap per ISP group or WAN (caps_gb) sends a message when 80% and 100% of it
are used, once each a month; a daily summary (daily_summary, at summary_hour) tells yesterday's data per ISP, its
top devices and the month so far. The Telegram bot's /usage, /today and /speed use month_text, summary and
speed_text.

Stored in VAR/usage.json: {"since": unix time, "months": {"YYYY-MM": {wan: [down bytes, up bytes]}},
"days": {"YYYY-MM-DD": {wan: [down, up]}}, "devices": {"YYYY-MM": {device: {"ips": [...], "w": {wan: [down, up]}}}},
"device_days": {"YYYY-MM-DD": {device: [down, up]}}, "sent": what the alerts have sent}; days kept for DAYS_KEPT
days (devices: DEVICE_DAYS_KEPT), months for good.
"""
import html
import json
import os
import threading
import time

import speedtab

INTERVAL_S = 30
RETAIN_S = 600         # a connection one read skipped keeps its baseline this long
MAX_GAP_S = 180        # reads further apart than this are not compared (the NOC or router was away)
REBOOT_WINDOW_S = 1800 # a router seen back within this uptime: its whole table counts (see Counter.update)
DAYS_KEPT = 400


class Counter:
    """Connection-table reads -> new bytes per WAN since the last read."""

    def __init__(self):
        self.prev, self.ts, self.warm = None, None, False
        self.hosts = {}    # the last read's new bytes per LAN address: {address: {wan: [down, up]}}

    def update(self, flows, addrs, now):
        """-> {wan: [down bytes, up bytes]} since the last read; {} for the first two reads after a gap (the
        first has nothing to compare, the second only builds a baseline of two reads). The same bytes by the
        LAN device behind each connection go to self.hosts."""
        hosts = {}
        # The router restarted (its uptime went back): its table is new, so every connection in it began since the
        # boot and all of its bytes are new. Count them in full instead of building a baseline, so nothing is lost.
        if self.ts is not None and now < self.ts and now <= REBOOT_WINDOW_S:
            self.prev, self.ts, self.warm = {}, 0.0, True
        fresh = self.prev is not None and 0 < now - self.ts <= (now if self.ts == 0.0 else MAX_GAP_S)
        dt = now - self.ts if fresh else 0
        new_cap = speedtab.NEW_FLOW_MAX_MBPS * 1e6 / 8 * dt
        out, cur = {}, {}
        for key, flow in flows.items():
            reply_dst, orig, reply, dst = flow[:4]
            if reply_dst in addrs:          # started here: the reply direction is the download
                wan, up, down, lan = addrs[reply_dst], orig, reply, flow[4] if len(flow) > 4 else None
            elif dst in addrs:              # started outside (a forwarded port): the LAN device is the reply source
                wan, up, down, lan = addrs[dst], reply, orig, flow[5] if len(flow) > 5 else None
            else:
                continue
            cur[key] = (up, down, now)
            if not fresh:
                continue
            was = self.prev.get(key)
            if was is None and up + down > new_cap:
                continue                    # not new: earlier reads missed it; its bytes become the baseline
            was_up, was_down = (was or (0, 0, 0))[:2]
            d, u = max(0, down - was_down), max(0, up - was_up)
            w = out.setdefault(wan, [0, 0])
            w[0] += d
            w[1] += u
            if lan and (d or u):
                h = hosts.setdefault(lan, {}).setdefault(wan, [0, 0])
                h[0] += d
                h[1] += u
        if fresh:
            cur.update({k: v for k, v in self.prev.items() if k not in cur and now - v[2] <= RETAIN_S})
        self.prev, self.ts = cur, now
        warm, self.warm = self.warm, fresh
        self.hosts = hosts if fresh and warm else {}
        return out if fresh and warm else {}


def add(store, deltas, when):
    """Add {wan: [down, up]} to the month and day of `when` (local time). Returns store."""
    if not deltas:
        return store
    t = time.localtime(when)
    for key, period in ((time.strftime("%Y-%m", t), "months"), (time.strftime("%Y-%m-%d", t), "days")):
        bucket = store.setdefault(period, {}).setdefault(key, {})
        for wan, (down, up) in deltas.items():
            v = bucket.setdefault(wan, [0, 0])
            v[0] += int(down)
            v[1] += int(up)
    days = sorted(store.get("days", {}))
    for old in days[:-DAYS_KEPT]:
        del store["days"][old]
    return store


def load(path):
    try:
        s = json.loads(path.read_text())
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path, store):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(store, separators=(",", ":")))
    os.replace(tmp, path)




# ---------------------------------------------------------------- devices
DEVICE_DAYS_KEPT = 8   # per-device days are only for the daily summary
DEVICES_SHOWN = 50
ROUTER_ID = "router"


def identity_maps(router, neigh):
    """-> ({address: MAC}, the router's own addresses) from DHCP leases, ARP and the IPv6 neighbour table."""
    r = router if isinstance(router, dict) else {}
    mac_of = {l["ip"]: l["mac"] for l in r.get("leases") or []}
    mac_of.update({ip: mac for mac, ip in (r.get("_arp") or {}).items()})
    mac_of.update(neigh or {})
    own = {speedtab.exploded(x) for i in r.get("interfaces") or [] for x in (i.get("ip"), i.get("ip6"))} - {None}
    return mac_of, own


def identity(addr, mac_of, own):
    """A device's key: its MAC where the router knows it (so its IPv4 and IPv6 add up), else its address."""
    a = speedtab.exploded(addr) or addr
    if a in own:
        return ROUTER_ID
    return mac_of.get(addr) or mac_of.get(a) or a


def add_devices(store, hosts, mac_of, own, when):
    """Add the last read's {address: {wan: [down, up]}} to each device's month (per WAN) and day (total)."""
    if not hosts:
        return store
    t = time.localtime(when)
    month, day = time.strftime("%Y-%m", t), time.strftime("%Y-%m-%d", t)
    mon = store.setdefault("devices", {}).setdefault(month, {})
    dd = store.setdefault("device_days", {}).setdefault(day, {})
    for addr, per in hosts.items():
        key = identity(addr, mac_of, own)
        d = mon.setdefault(key, {"ips": [], "w": {}})
        if addr not in d["ips"]:
            d["ips"] = (d["ips"] + [addr])[-4:]
        tot = dd.setdefault(key, [0, 0])
        for wan, (down, up) in per.items():
            v = d["w"].setdefault(wan, [0, 0])
            v[0] += int(down)
            v[1] += int(up)
            tot[0] += int(down)
            tot[1] += int(up)
    for old in sorted(store["device_days"])[:-DEVICE_DAYS_KEPT]:
        del store["device_days"][old]
    return store


def namer(router, devices):
    """-> name(key, ips): the NOC's device list first (by MAC, then address), then the DHCP lease's hostname."""
    r = router if isinstance(router, dict) else {}
    lease = {l["mac"]: l.get("name") for l in r.get("leases") or [] if l.get("name")}
    by_mac = {d["mac"].lower(): d["name"] for d in devices or [] if d.get("mac")}
    by_ip = {}
    for d in devices or []:
        t = d.get("target") or ""
        t = t.rsplit(":", 1)[0] if t.count(":") == 1 else t      # "192.0.2.5:80" -> "192.0.2.5"; IPv6 stays whole
        if speedtab.exploded(t):
            by_ip[speedtab.exploded(t)] = d["name"]

    def name(key, ips):
        if key == ROUTER_ID:
            return "Router (its own traffic)"
        return by_mac.get(key) or next((by_ip[speedtab.exploded(i)] for i in ips if speedtab.exploded(i) in by_ip), "") \
            or lease.get(key, "")
    return name


def is_mac(key):
    return len(key) == 17 and key.count(":") == 5


# ---------------------------------------------------------------- the page
def view(store, rows, groups, month=None, router=None, devices=None):
    """The /api/usage answer for one month (default: the newest): every WAN with its data used, totals per ISP,
    the month's days, its busiest devices, and the months there are."""
    months = sorted((store.get("months") or {}), reverse=True)
    month = month if month in months else (months[0] if months else time.strftime("%Y-%m"))
    data = (store.get("months") or {}).get(month, {})
    isp_of = {m: g for g, members in (groups or {}).items() for m in members}
    dev_of = {w["name"]: w.get("device") for w in rows}
    names = sorted(set(dev_of) | set(data), key=speedtab.natural)
    wans = [{"wan": n, "device": dev_of.get(n), "isp": isp_of.get(n), "down_bytes": data.get(n, [0, 0])[0],
             "up_bytes": data.get(n, [0, 0])[1]} for n in names]
    isps = {}
    for w in wans:
        t = isps.setdefault(w["isp"] or w["wan"], {"isp": w["isp"] or w["wan"], "down_bytes": 0, "up_bytes": 0})
        t["down_bytes"] += w["down_bytes"]
        t["up_bytes"] += w["up_bytes"]
    days = [{"day": d, "down_bytes": sum(v[0] for v in per.values()), "up_bytes": sum(v[1] for v in per.values())}
            for d, per in sorted((store.get("days") or {}).items()) if d.startswith(month)]
    name = namer(router, devices)
    devs = []
    for key, d in ((store.get("devices") or {}).get(month) or {}).items():
        via = sorted(({"wan": w, "isp": isp_of.get(w), "device": dev_of.get(w), "down_bytes": v[0], "up_bytes": v[1]}
                      for w, v in d.get("w", {}).items()), key=lambda x: -(x["down_bytes"] + x["up_bytes"]))
        devs.append({"key": key, "name": name(key, d.get("ips", [])), "mac": key if is_mac(key) else None,
                     "ips": d.get("ips", []), "router": key == ROUTER_ID, "via": via,
                     "down_bytes": sum(x["down_bytes"] for x in via), "up_bytes": sum(x["up_bytes"] for x in via)})
    devs.sort(key=lambda x: -(x["down_bytes"] + x["up_bytes"]))
    return {"month": month, "months": months, "since": store.get("since"), "interval_s": INTERVAL_S,
            "wans": wans, "isps": list(isps.values()), "days": days, "devices": devs[:DEVICES_SHOWN],
            "devices_total": len(devs),
            "total_down_bytes": sum(w["down_bytes"] for w in wans), "total_up_bytes": sum(w["up_bytes"] for w in wans)}


# ---------------------------------------------------------------- alerts
DEFAULT_SETTINGS = {"caps_gb": {}, "daily_summary": True, "summary_hour": 9}
CAP_STEPS = (80, 100)   # % of a cap that sends a message, once each per month


def settings(cfg):
    s = {**DEFAULT_SETTINGS, **(cfg or {})}
    s["caps_gb"] = {str(k): float(v) for k, v in (s.get("caps_gb") or {}).items() if float(v) > 0}
    return s


def human(b):
    return f"{b / 1e12:.2f} TB" if b >= 1e12 else f"{b / 1e9:.1f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"


def used_by(month_data, name, groups):
    """Bytes (down + up) a cap's name used: an ISP group's WANs added up, or one WAN."""
    members = (groups or {}).get(name) or [name]
    return sum(sum(month_data.get(m, [0, 0])) for m in members)


def check(store, cfg, groups, site, name_of, now):
    """Messages due now (cap steps passed this month, the daily summary), each once: what was sent is kept in
    store["sent"]. Changes store; the caller saves it."""
    s, msgs = settings(cfg), []
    t = time.localtime(now)
    month, today = time.strftime("%Y-%m", t), time.strftime("%Y-%m-%d", t)
    sent = store.setdefault("sent", {})
    done = set(sent.get("caps", {}).get(month, []))
    data = (store.get("months") or {}).get(month, {})
    for name, gb in sorted(s["caps_gb"].items()):
        used = used_by(data, name, groups)
        passed = [x for x in CAP_STEPS if used >= gb * 1e9 * x / 100]
        if passed and f"{name}:{passed[-1]}" not in done:    # one message for the highest step newly passed
            icon = "🔴" if passed[-1] >= 100 else "🟠"
            msgs.append(f"{icon} <b>{site}</b>: <b>{name}</b> has used {human(used)} this month "
                        f"({100 * used / (gb * 1e9):.0f}% of its {gb:g} GB cap).")
        done |= {f"{name}:{x}" for x in passed}
    sent.setdefault("caps", {})[month] = sorted(done)
    for old in sorted(sent["caps"])[:-3]:
        del sent["caps"][old]
    if s["daily_summary"] and t.tm_hour >= int(s["summary_hour"]) and sent.get("summary") != today:
        sent["summary"] = today
        yday = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
        msg = summary(store, yday, groups, site, name_of)
        if msg:
            msgs.append(msg)
    return msgs


e = html.escape


def by_isp(per, groups):
    """{wan: [down, up]} -> {isp group (or the WAN when in none): {wan: [down, up]}}."""
    isp_of = {m: g for g, members in (groups or {}).items() for m in members}
    out = {}
    for wan, v in per.items():
        out.setdefault(isp_of.get(wan) or wan, {})[wan] = v
    return out


def isp_lines(per, groups, wans=False):
    """One line per ISP, busiest first: data used, down and up (and its WANs when `wans` and it has several)."""
    lines = []
    for isp, ws in sorted(by_isp(per, groups).items(), key=lambda x: -sum(sum(v) for v in x[1].values())):
        d, u = sum(v[0] for v in ws.values()), sum(v[1] for v in ws.values())
        lines.append(f"• <b>{e(isp)}</b>: {human(d + u)} (↓{human(d)} ↑{human(u)})")
        if wans and len(ws) > 1:
            lines.append("   " + " · ".join(f"{e(w)} {human(sum(v))}"
                                            for w, v in sorted(ws.items(), key=lambda x: speedtab.natural(x[0]))))
    return lines


def summary(store, day, groups, site, name_of, label=None):
    """The daily message: a day's data per ISP and its top 3 devices, and the month so far. None without data.
    `label` replaces "on <day>" in the title (the bot's /today says "today so far")."""
    per = (store.get("days") or {}).get(day)
    if not per:
        return None
    lines = [f"📊 <b>{e(site)}</b>: data used {e(label or 'on ' + day)}"]
    lines += isp_lines(per, groups)
    lines.append(f"Total: {human(sum(d + u for d, u in per.values()))}")
    devs = sorted(((store.get("device_days") or {}).get(day) or {}).items(), key=lambda x: -sum(x[1]))[:3]
    if devs:
        month = (store.get("devices") or {}).get(day[:7]) or {}
        lines.append("Top devices: " + ", ".join(f"{e(name_of(k, month.get(k, {}).get('ips', [])) or k)} {human(sum(v))}"
                                                 for k, v in devs))
    mtd = sum(sum(v) for v in ((store.get("months") or {}).get(day[:7]) or {}).values())
    lines.append(f"Month so far: {human(mtd)}")
    return "\n".join(lines)


def month_text(store, groups, site, name_of, cfg, month, top=5, now=None):
    """The bot's /usage: a month's data per ISP and WAN, how much of each cap is used, and its busiest devices."""
    per = (store.get("months") or {}).get(month)
    if not per:
        have = ", ".join(sorted(store.get("months") or {}, reverse=True)[:6])
        return f"No data counted for {e(month)}." + (f" Months there are: {e(have)}" if have else "")
    current = month == time.strftime("%Y-%m", time.localtime(now or time.time()))
    lines = [f"📊 <b>{e(site)}</b>: data used in {e(month)}" + (" so far" if current else "")]
    lines += isp_lines(per, groups, wans=True)
    d, u = sum(v[0] for v in per.values()), sum(v[1] for v in per.values())
    lines.append(f"<b>Total</b>: {human(d + u)} (↓{human(d)} ↑{human(u)})")
    caps = settings(cfg)["caps_gb"]
    if caps and current:
        lines.append("\n<b>Caps</b>")
        for name, gb in sorted(caps.items()):
            used = used_by(per, name, groups)
            pct = 100 * used / (gb * 1e9)
            icon = "🔴" if pct >= 100 else "🟠" if pct >= CAP_STEPS[0] else "🟢"
            lines.append(f"{icon} {e(name)}: {human(used)} of {gb:g} GB ({pct:.0f}%)")
    devs = (store.get("devices") or {}).get(month) or {}
    size = lambda dv: sum(sum(v) for v in dv.get("w", {}).values())
    busiest = sorted(devs.items(), key=lambda x: -size(x[1]))[:top]
    if busiest:
        lines.append(f"\n<b>Top {len(busiest)} devices</b> (of {len(devs)})")
        for key, dv in busiest:
            name = name_of(key, dv.get("ips", [])) or (dv.get("ips") or [key])[0]
            lines.append(f"• {e(name)}: {human(size(dv))}")
    if store.get("since"):
        lines.append(f"\n<i>Counting since {time.strftime('%Y-%m-%d %H:%M', time.localtime(store['since']))}</i>")
    return "\n".join(lines)


def mbps(bits):
    return f"{bits / 1e9:.2f} Gbps" if bits >= 1e9 else f"{bits / 1e6:.1f} Mbps" if bits >= 1e5 else f"{bits / 1e3:.0f} kbps"


def speed_text(rates, groups, site, tested=None, age_s=None):
    """The bot's /speed: each WAN's download and upload over the collector's last read (about 30 s), per ISP, with
    each WAN's last hourly speed test (tested: {wan: Mbps})."""
    if not rates:
        return "No reading yet: the data counter reads the router every 30 s. Try again in a minute."
    tested = tested or {}
    lines = [f"⚡ <b>{e(site)}</b>: traffic right now" + (f" <i>(read {int(age_s)} s ago)</i>" if age_s is not None else "")]
    for isp, ws in sorted(by_isp(rates, groups).items(), key=lambda x: -sum(v[0] for v in x[1].values())):
        lines.append(f"<b>{e(isp)}</b>: ↓{mbps(sum(v[0] for v in ws.values()))} ↑{mbps(sum(v[1] for v in ws.values()))}")
        for w, (d, u) in sorted(ws.items(), key=lambda x: speedtab.natural(x[0])):
            t = tested.get(w)
            lines.append(f"   {e(w)}: ↓{mbps(d)} ↑{mbps(u)}" + (f" · tested {t:g} Mbps" if isinstance(t, (int, float)) else ""))
    d, u = sum(v[0] for v in rates.values()), sum(v[1] for v in rates.values())
    cap = sum(t for w, t in tested.items() if w in rates and isinstance(t, (int, float)) and t > 0)
    lines.append(f"<b>Total</b>: ↓{mbps(d)} ↑{mbps(u)}"
                 + (f" · {min(100, round(100 * d / (cap * 1e6)))}% of the tested {cap:g} Mbps" if cap else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- the thread
class Collector:
    """The background thread: reads the router every INTERVAL_S, keeps VAR/usage.json and sends the alerts."""

    def __init__(self, noc):
        self.noc, self.counter, self.lock = noc, Counter(), threading.Lock()
        self.path = noc.VAR / "usage.json"
        self.store = load(self.path)
        self.error = None
        self.rates = {}      # the last read's bits per second per WAN, for /metrics and /speed: {wan: [down, up]}
        self.rates_at = None  # when self.rates was read (unix time)

    def store_copy(self):
        with self.lock:
            return json.loads(json.dumps(self.store))

    def step(self, now=None):
        """One read of the router; adds what it measured, sends what is due and saves. Returns the deltas."""
        r = self.noc.cached("router", 15, self.noc.collect_router)
        if not isinstance(r, dict) or "error" in r:
            raise RuntimeError((r or {}).get("error", "router not readable"))
        _, addrs = speedtab.wan_rows(r.get("interfaces", []), self.noc.wan_ifaces())
        text = self.noc.ssh(self.noc.ROUTER_HOST, speedtab.LOAD_SCRIPT, timeout=25)
        clock = speedtab.router_clock(text)
        last = self.counter.ts
        deltas = self.counter.update(speedtab.parse_flows(text), addrs, clock if clock is not None else time.time())
        if deltas and last is not None and self.counter.ts > last:
            dt = self.counter.ts - last
            self.rates = {w: [d * 8 / dt, u * 8 / dt] for w, (d, u) in deltas.items()}
            self.rates_at = time.time()
        mac_of, own = identity_maps(r, speedtab.parse_neigh(text))
        when = now or time.time()
        groups = self.noc.wan_groups() if hasattr(self.noc, "wan_groups") else {}
        C = getattr(self.noc, "C", {}) or {}
        with self.lock:
            self.store.setdefault("since", int(when))
            add(self.store, deltas, when)
            add_devices(self.store, self.counter.hosts, mac_of, own, when)
            msgs = check(self.store, C.get("usage"), groups, getattr(self.noc, "SITE_NAME", "NOC"),
                         namer(r, C.get("devices")), when)
            save(self.path, self.store)
        tg = getattr(self.noc, "TG", None)
        for m in msgs:
            if tg is not None:
                tg.queue(m)
        return deltas

    def loop(self):
        while True:
            started = time.time()
            try:
                self.step()
                self.error = None
            except Exception as ex:            # the router away for a while: try again next time
                self.error = str(ex)
            time.sleep(max(1, INTERVAL_S - (time.time() - started)))

    def start(self):
        threading.Thread(target=self.loop, name="noc-usage", daemon=True).start()
