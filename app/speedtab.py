"""The NOC's Speed tab: on-demand speed tests per WAN and the live load on each WAN.

Load: hardware flow offload hides forwarded traffic from the WAN devices' own counters, so they read about 0
while gigabits pass. The connection table still counts it (nf_conntrack_acct), and a connection's address on
the router's side says which WAN it uses. Load is the change in each connection's bytes between two samples,
added up per WAN (a WAN's IPv6 sibling wanN_6 counts as wanN). It reads the table only while the tab is open.

Tests: a download from speedwatch.url through one WAN with `mwan3 use <iface>` (binding curl to the device
works only for PPPoE; DHCP links have no route of their own). One by one measures each WAN alone; together
measures all at once, which shows the total. Results join the hourly watch's history (VAR/speedwatch.jsonl).
"""
import ipaddress
import re
import shlex
import threading
import time

import speedwatch

DEFAULTS = {"test_bytes": 100_000_000, "test_timeout_s": 12}
NAME = re.compile(r"[A-Za-z0-9_.-]{1,32}$")
MAX_GAP_S = 15    # samples further apart than this are not compared: new connections would all look fresh
# Reading /proc/net/nf_conntrack is not atomic: a busy table can skip a connection in one read and list it in
# the next, with its whole lifetime's bytes. A connection never seen before only counts if its bytes could have
# come in this interval at no more than this rate; otherwise it becomes the baseline (seen on a live router:
# a 573 MB torrent flow reappearing in a 1.3 s window read as 3.6 Gbit/s).
NEW_FLOW_MAX_MBPS = 1000

# router: its uptime (the clock the sample was taken by), then the raw table; parsing on the host is ~15x faster
LOAD_SCRIPT = "cut -d' ' -f1 /proc/uptime\ncat /proc/net/nf_conntrack\n"


def router_clock(text):
    """The router's uptime from LOAD_SCRIPT's first line, or None. The time the table was read: the SSH round
    trip after it takes 0.3-18 s and varies, so the NOC's own clock would stretch or squeeze the interval."""
    try:
        return float(text.split("\n", 1)[0])
    except ValueError:
        return None


def natural(name):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", name)]


def exploded(addr):
    """An address as the connection table writes it: IPv6 in full, lowercase (the /prefix dropped)."""
    addr = (addr or "").split("/")[0]
    try:
        return ipaddress.ip_address(addr).exploded
    except ValueError:
        return None


def base(name):
    return name[:-2] if name.endswith("_6") else name


def wan_rows(interfaces, monitored):
    """Router interfaces -> the WANs to show, one row per link (wanN_6 is merged into wanN) and
    {address on the router: WAN name} for the load."""
    by_name, rows, addrs = {i["name"]: i for i in interfaces}, [], {}
    for name in sorted({base(n) for n in monitored}, key=natural):
        i = by_name.get(name) or {}
        rows.append({"name": name, "device": i.get("device"), "up": bool(i.get("up")), "ip": (i.get("ip") or "").split("/")[0]})
        for member in (name, name + "_6"):
            m = by_name.get(member) or {}
            for a in (m.get("ip"), m.get("ip6")):
                if exploded(a):
                    addrs[exploded(a)] = name
    return rows, addrs


def parse_flows(text):
    """LOAD_SCRIPT output -> {connection: (router-side address, original direction bytes, reply bytes, dst)}."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 8:
            try:
                out[" ".join(parts[:5])] = (parts[6], int(parts[5]), int(parts[7]), parts[2])
            except ValueError:
                pass
            continue
        if len(parts) < 8:
            continue
        proto = parts[2]
        s = d = r_dst = p = q = None
        orig_b = reply_b = 0
        c = e = 0
        for tok in parts:
            if tok.startswith("src=") and s is None:
                s = tok[4:]
            elif tok.startswith("dst="):
                if c == 0:
                    d = tok[4:]
                    c = 1
                else:
                    r_dst = tok[4:]
            elif tok.startswith("sport=") and p is None:
                p = tok[6:]
            elif tok.startswith("dport=") and q is None:
                q = tok[6:]
            elif tok.startswith("bytes="):
                if e == 0:
                    try:
                        orig_b = int(tok[6:])
                    except ValueError:
                        pass
                    e = 1
                else:
                    try:
                        reply_b = int(tok[6:])
                    except ValueError:
                        pass
        if r_dst and s and d and p and q:
            out[f"{proto} {s} {d} {p} {q}"] = (r_dst, orig_b, reply_b, d)
    return out


class Load:
    """Turns connection-table samples into Mbps per WAN. Only connections of a WAN are kept."""

    def __init__(self):
        self.prev, self.ts = None, None   # prev: {connection: (up, down, last seen)}, kept MAX_GAP_S after a missed read
        self.warm = False                 # the first comparison only builds the baseline: one read may have skipped flows

    def update(self, flows, addrs, now):
        """-> {wan: {"down_mbps", "up_mbps", "flows"}}, or {} for the first two samples after a gap: nothing to
        compare yet, then a baseline made of two reads (a connection one read skipped would look new)."""
        out, cur = {}, {}
        fresh = self.prev is not None and now - self.ts <= MAX_GAP_S and now > self.ts
        dt = now - self.ts if fresh else 0
        new_cap = NEW_FLOW_MAX_MBPS * 1e6 / 8 * dt
        for key, (reply_dst, orig, reply, dst) in flows.items():
            if reply_dst in addrs:          # started here: the reply direction is the download
                wan, up, down = addrs[reply_dst], orig, reply
            elif dst in addrs:              # started outside (a forwarded port): the original direction is the download
                wan, up, down = addrs[dst], reply, orig
            else:
                continue                    # LAN to LAN, or not on a monitored WAN
            cur[key] = (up, down, now)
            w = out.setdefault(wan, {"down": 0, "up": 0, "flows": 0})
            w["flows"] += 1
            if fresh:
                was = self.prev.get(key)
                if was is None and up + down > new_cap:
                    continue                # not new: a read missed it before; its bytes become the baseline
                was_up, was_down = (was or (0, 0, 0))[:2]   # a new connection: all its bytes are from this interval
                w["up"] += max(0, up - was_up)
                w["down"] += max(0, down - was_down)
        if fresh:                           # remember connections a read skipped, until they are MAX_GAP_S old
            cur.update({k: v for k, v in self.prev.items() if k not in cur and now - v[2] <= MAX_GAP_S})
        self.prev, self.ts = cur, now
        warm, self.warm = self.warm, fresh
        if not (fresh and warm):
            return {}
        return {k: {"down_mbps": round(v["down"] * 8 / 1e6 / dt, 1), "up_mbps": round(v["up"] * 8 / 1e6 / dt, 1), "flows": v["flows"]}
                for k, v in out.items()}


def test_script(names, url, nbytes, timeout, together=False):
    """Shell for the router: "<wan> <bytes/s> <bytes> <http code>" per WAN (speedwatch.parse reads it)."""
    for n in names:
        if not NAME.match(n):
            raise ValueError(f"bad interface name {n!r}")
    lines = [f"mwan3 use {shlex.quote(n)} curl -4 -s -o /dev/null -r 0-{int(nbytes) - 1} --max-time {int(timeout)} "
             f"-w \"{n} %{{speed_download}} %{{size_download}} %{{http_code}}\\n\" {shlex.quote(url)}" + (" &" if together else "")
             for n in names]
    # curl exits non-zero at --max-time, but it has still measured: the script must not read as failed
    return "\n".join(lines + (["wait"] if together else []) + ["exit 0"]) + "\n"


class Tests:
    """One test run at a time, in a background thread; `state` is what the page shows while it runs."""

    def __init__(self, noc):
        self.noc, self.lock = noc, threading.Lock()
        self.state = {"running": False, "mode": None, "results": {}, "started": None, "finished": None, "total_mbps": None, "error": None}

    def cfg(self):
        return {**speedwatch.DEFAULTS, **DEFAULTS, **(self.noc.C.get("speedwatch") or {})}

    def start(self, names, together=False):
        """Start a run; -> a copy of the state. ValueError when it can't start."""
        cfg = self.cfg()
        if not cfg.get("url"):
            raise ValueError("speedwatch.url is not set in config.json")
        if not names:
            raise ValueError("no WAN to test")
        with self.lock:
            if self.state["running"]:
                raise ValueError("a speed test is already running")
            self.state = {"running": True, "mode": "together" if together else "each", "error": None, "total_mbps": None,
                          "started": time.time(), "finished": None, "results": {n: {"status": "waiting", "mbps": None} for n in names}}
            snapshot = self.snapshot_locked()
        threading.Thread(target=self.run, args=(list(names), together, cfg), daemon=True).start()
        return snapshot

    def snapshot_locked(self):
        return {**self.state, "results": {k: dict(v) for k, v in self.state["results"].items()}}

    def snapshot(self):
        with self.lock:
            return self.snapshot_locked()

    def note(self, name, status, mbps=None):
        with self.lock:
            self.state["results"][name] = {"status": status, "mbps": mbps}

    def measure(self, names, cfg, together):
        script = test_script(names, cfg["url"], cfg["test_bytes"], cfg["test_timeout_s"], together)
        return speedwatch.parse(self.noc.ssh(self.noc.ROUTER_HOST, script, timeout=int(cfg["test_timeout_s"]) * (1 if together else len(names)) + 30))

    def run(self, names, together, cfg):
        done, err = {}, None
        try:
            with speedwatch.test_lock(self.noc.VAR):   # not on top of the hourly watch: each would spoil the other
                groups = [names] if together else [[n] for n in names]
                for group in groups:
                    for n in group:
                        self.note(n, "running")
                    got = self.measure(group, cfg, together)
                    for n in group:
                        v = got.get(n)
                        done[n] = v if isinstance(v, int) else None
                        self.note(n, "done" if isinstance(v, int) else "failed", done[n])
        except Exception as ex:   # e.g. the router's SSH: show it, don't leave the run hanging
            err = f"{type(ex).__name__}: {ex}"
        nums = [v for v in done.values() if v is not None]
        with self.lock:
            for r in self.state["results"].values():
                if r["status"] in ("waiting", "running"):
                    r["status"] = "failed"
            self.state.update(running=False, finished=time.time(), error=err, total_mbps=sum(nums) if together and nums else None)
        if done:
            try:
                self.noc.append_jsonl(self.noc.VAR / "speedwatch.jsonl", [{
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "mbps": dict(sorted(done.items())),
                    "source": "together" if together else "manual"}])
            except OSError:
                pass


def history_rows(lines, limit=48):
    """speedwatch.jsonl entries (oldest first) -> the newest `limit` that measured each WAN alone: a together
    run shares the line between WANs, so it says nothing about one WAN's own speed."""
    return [x for x in lines if x.get("source") != "together" and isinstance(x.get("mbps"), dict)][-limit:]


def utilisation(rows, total_down):
    """-> (capacity Mbps, % of it in use now). Capacity is the sum of each WAN's last speed test, for WANs that are
    up: what the links carried on their own, so an estimate (tests one by one can add up to more than the line)."""
    tested = [w["last_mbps"] for w in rows if w["up"] and isinstance(w.get("last_mbps"), int) and w["last_mbps"] > 0]
    capacity = sum(tested) if tested else None
    if capacity is None or total_down is None:
        return capacity, None
    return capacity, min(100, round(100 * total_down / capacity))


def view(noc, tests, load):
    """The /api/speed answer: every WAN with its load and last result, the running test, the history."""
    cfg = tests.cfg()
    r = noc.cached("router", 15, noc.collect_router)
    if not isinstance(r, dict) or "error" in r:
        raise RuntimeError((r or {}).get("error", "router not readable"))
    rows, addrs = wan_rows(r.get("interfaces", []), noc.wan_ifaces())
    rates, load_error = {}, None
    try:
        def sample():
            text = noc.ssh(noc.ROUTER_HOST, LOAD_SCRIPT, timeout=18)
            return load.update(parse_flows(text), addrs, router_clock(text) or time.time())
        rates = noc.cached("wanload", 2, sample)
        if isinstance(rates, dict) and "error" in rates:
            load_error, rates = rates["error"], {}
    except Exception as ex:
        load_error = str(ex)
    hist = history_rows(list(reversed(noc.tail_jsonl(noc.VAR / "speedwatch.jsonl", 200))))   # tail_jsonl: newest first
    state = tests.snapshot()
    for w in rows:
        w.update(rates.get(w["name"]) or {"down_mbps": None if not rates else 0.0, "up_mbps": None if not rates else 0.0, "flows": 0})
        w["last_mbps"], w["last_ts"] = next(((h["mbps"][w["name"]], h["ts"]) for h in reversed(hist)
                                             if isinstance(h["mbps"].get(w["name"]), int)), (None, None))
    up = [w for w in rows if w["down_mbps"] is not None]
    total_down = round(sum(w["down_mbps"] for w in up), 1) if up else None
    capacity, use = utilisation(rows, total_down)
    return {"configured": bool(cfg.get("url")), "url": cfg.get("url"), "test_mb": round(int(cfg["test_bytes"]) / 1e6),
            "slow_mbps": cfg["slow_mbps"], "wans": rows, "test": state, "load_error": load_error,
            "total_down_mbps": total_down,
            "total_up_mbps": round(sum(w["up_mbps"] for w in up), 1) if up else None,
            "capacity_mbps": capacity, "utilisation_pct": use,
            "history": [{"ts": h["ts"], "mbps": h["mbps"], "source": h.get("source", "hourly")} for h in hist]}


def pick(rows, names):
    """The WANs a test request names (None = every WAN that is up). ValueError for one that isn't a WAN."""
    known = {w["name"]: w for w in rows}
    if names is None:
        return [w["name"] for w in rows if w["up"]]
    if not isinstance(names, list):
        raise ValueError("names must be a list")
    for n in names:
        if n not in known:
            raise ValueError(f"{n!r} is not a monitored WAN")
    return list(dict.fromkeys(names))
