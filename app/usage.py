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

Stored in VAR/usage.json: {"since": unix time, "months": {"YYYY-MM": {wan: [down bytes, up bytes]}},
"days": {"YYYY-MM-DD": {wan: [down, up]}}}, days kept for DAYS_KEPT days, months for good.
"""
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

    def update(self, flows, addrs, now):
        """-> {wan: [down bytes, up bytes]} since the last read; {} for the first two reads after a gap (the
        first has nothing to compare, the second only builds a baseline of two reads)."""
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
                wan, up, down = addrs[reply_dst], orig, reply
            elif dst in addrs:              # started outside (a forwarded port)
                wan, up, down = addrs[dst], reply, orig
            else:
                continue
            cur[key] = (up, down, now)
            if not fresh:
                continue
            was = self.prev.get(key)
            if was is None and up + down > new_cap:
                continue                    # not new: earlier reads missed it; its bytes become the baseline
            was_up, was_down = (was or (0, 0, 0))[:2]
            w = out.setdefault(wan, [0, 0])
            w[0] += max(0, down - was_down)
            w[1] += max(0, up - was_up)
        if fresh:
            cur.update({k: v for k, v in self.prev.items() if k not in cur and now - v[2] <= RETAIN_S})
        self.prev, self.ts = cur, now
        warm, self.warm = self.warm, fresh
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


def view(store, rows, groups, month=None):
    """The /api/usage answer for one month (default: the newest): every WAN with its data used, totals per ISP,
    the month's days, and the months there are."""
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
    return {"month": month, "months": months, "since": store.get("since"), "interval_s": INTERVAL_S,
            "wans": wans, "isps": list(isps.values()), "days": days,
            "total_down_bytes": sum(w["down_bytes"] for w in wans), "total_up_bytes": sum(w["up_bytes"] for w in wans)}


class Collector:
    """The background thread: reads the router every INTERVAL_S and keeps VAR/usage.json."""

    def __init__(self, noc):
        self.noc, self.counter, self.lock = noc, Counter(), threading.Lock()
        self.path = noc.VAR / "usage.json"
        self.store = load(self.path)
        self.error = None

    def store_copy(self):
        with self.lock:
            return json.loads(json.dumps(self.store))

    def step(self, now=None):
        """One read of the router; adds what it measured and saves. Returns the deltas."""
        r = self.noc.cached("router", 15, self.noc.collect_router)
        if not isinstance(r, dict) or "error" in r:
            raise RuntimeError((r or {}).get("error", "router not readable"))
        _, addrs = speedtab.wan_rows(r.get("interfaces", []), self.noc.wan_ifaces())
        text = self.noc.ssh(self.noc.ROUTER_HOST, speedtab.LOAD_SCRIPT, timeout=25)
        clock = speedtab.router_clock(text)
        deltas = self.counter.update(speedtab.parse_flows(text), addrs, clock if clock is not None else time.time())
        with self.lock:
            self.store.setdefault("since", int(now or time.time()))
            add(self.store, deltas, now or time.time())
            save(self.path, self.store)
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
