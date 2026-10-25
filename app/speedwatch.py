"""Per-session speed watch for multi-session PPPoE WANs (Telegram alerts).

An ISP can throttle one PPPoE session of several (e.g. 40 Mbps on one, 300+ on the
rest) while the link as a whole still looks fine. Run from cron, this downloads a
slice of a test file through every PPPoE interface on the router, one at a time,
and tells the paired Telegram chat when a session becomes slow and when it recovers.

- A session is slow below `slow_mbps`. A slow reading is retested once after
  `retest_s` and only counts if it is slow both times (no alert for a blip).
- If every session is slow, the test server or the whole line is the likely cause:
  that is reported once as "all sessions slow" instead of one alert per session.
- Each run is appended to VAR/speedwatch.jsonl; alert state is kept in VAR/speedwatch.json.

Config (config.json): "speedwatch": {"url": "<http file of 100 MB or more>", "slow_mbps": 100,
"bytes": 40000000, "timeout_s": 10, "retest_s": 60}

Hourly from cron:  cd /opt/noc && NOC_NO_WORKERS=1 python3 speedwatch.py
Look only (no Telegram, no saved state):  python3 speedwatch.py --dry-run
"""
import html
import json
import shlex
import sys
import time

e = html.escape

DEFAULTS = {"slow_mbps": 100, "bytes": 40_000_000, "timeout_s": 10, "retest_s": 60}


def router_script(url, nbytes, timeout):
    """Shell for the router: one line per PPPoE interface, "<iface> down" or
    "<iface> <bytes/s> <bytes> <http code>" from curl bound to pppoe-<iface>."""
    return (
        "for i in $(uci -q show network | sed -n \"s/^network\\.\\([^.]*\\)\\.proto='pppoe'$/\\1/p\"); do\n"
        "  [ \"$(ifstatus $i | jsonfilter -e @.up)\" = true ] || { echo \"$i down\"; continue; }\n"
        f"  curl -4 -s -o /dev/null --interface pppoe-$i -r 0-{int(nbytes) - 1} --max-time {int(timeout)} "
        f"-w \"$i %{{speed_download}} %{{size_download}} %{{http_code}}\\n\" {shlex.quote(url)}\n"
        "done\n")


def parse(text):
    """Router output -> {iface: Mbps (int), None (download failed) or "down"}."""
    out = {}
    for line in text.splitlines():
        p = line.split()
        if len(p) == 2 and p[1] == "down":
            out[p[0]] = "down"
        elif len(p) == 4:
            try:
                ok = p[3] in ("200", "206") and float(p[2]) > 0
                out[p[0]] = round(float(p[1]) * 8 / 1e6) if ok else None
            except ValueError:
                out[p[0]] = None
    return out


def best(a, b):
    """Per interface, the better of two readings (a number beats None and "down")."""
    out = {}
    for k in a.keys() | b.keys():
        nums = [v for v in (a.get(k), b.get(k)) if isinstance(v, int)]
        out[k] = max(nums) if nums else b.get(k, a.get(k))
    return out


def assess(speeds, slow_mbps):
    """Readings -> {"slow": {iface: Mbps}, "fast": {iface: Mbps}, "failed": [...], "overall": bool}.
    overall: every measured session is slow (then the test server or the line is the likely cause)."""
    nums = {k: v for k, v in speeds.items() if isinstance(v, int)}
    return {"slow": {k: v for k, v in nums.items() if v < slow_mbps},
            "fast": {k: v for k, v in nums.items() if v >= slow_mbps},
            "failed": sorted(k for k, v in speeds.items() if v is None),
            "overall": bool(nums) and all(v < slow_mbps for v in nums.values())}


def span(values):
    lo, hi = min(values), max(values)
    return f"{lo}" if lo == hi else f"{lo}-{hi}"


def ago(seconds):
    m = int(seconds // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def changes(prev, a, now, site):
    """(previous state, assessment) -> (messages to send, new state).
    State: {"slow": {iface: since}, "overall": since or None}. While every session is slow,
    the per-session state is left as it was: one "all slow" alert, not one per session."""
    prev = prev or {}
    was, overall, msgs = dict(prev.get("slow") or {}), prev.get("overall"), []
    if a["overall"]:
        if not overall:
            msgs.append(f"🐢 <b>{e(site)}</b>: every PPPoE session is slow ({span(list(a['slow'].values()))} Mbps): "
                        "the test server or the whole line, not one session.")
            overall = now
        return msgs, {"slow": was, "overall": overall}
    if overall and a["fast"]:
        msgs.append(f"✅ <b>{e(site)}</b>: PPPoE sessions are fast again "
                    f"({span(list(a['fast'].values()))} Mbps, all slow for {ago(now - overall)}).")
        overall = None
    others = f" (others {span(list(a['fast'].values()))} Mbps)" if a["fast"] else ""
    for k in sorted(a["slow"]):
        if k not in was:
            msgs.append(f"🐢 <b>{e(site)}</b>: PPPoE session <b>{e(k)}</b> is slow: "
                        f"{a['slow'][k]} Mbps{others}, confirmed by a retest.")
    for k in sorted(was):
        if k in a["fast"]:
            msgs.append(f"✅ <b>{e(site)}</b>: <b>{e(k)}</b> is back to {a['fast'][k]} Mbps "
                        f"(slow for {ago(now - was[k])}).")
    # a slow session that is now down or failing stays tracked until it measures fast
    slow = {k: v for k, v in was.items() if k not in a["fast"]}
    slow.update({k: was.get(k, now) for k in a["slow"]})
    return msgs, {"slow": slow, "overall": overall}


def measure(noc, cfg):
    """Test every session; if any is slow, test again and keep each session's better reading."""
    script = router_script(cfg["url"], cfg["bytes"], cfg["timeout_s"])
    speeds = parse(noc.ssh(noc.ROUTER_HOST, script, timeout=240))
    if assess(speeds, cfg["slow_mbps"])["slow"]:
        time.sleep(cfg["retest_s"])
        speeds = best(speeds, parse(noc.ssh(noc.ROUTER_HOST, script, timeout=240)))
    return speeds


def run(noc, dry_run=False, now=None):
    cfg = {**DEFAULTS, **(noc.C.get("speedwatch") or {})}
    if not cfg.get("url") or not noc.ROUTER_HOST:
        raise SystemExit("speedwatch.url and router.host must be set in config.json")
    speeds = measure(noc, cfg)
    now = now or time.time()
    path = noc.VAR / "speedwatch.json"
    try:
        prev = json.loads(path.read_text())
    except (OSError, ValueError):
        prev = {}
    msgs, state = changes(prev, assess(speeds, cfg["slow_mbps"]), now, noc.SITE_NAME)
    line = {"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "mbps": dict(sorted(speeds.items()))}
    print(json.dumps(line))
    for m in msgs:
        print("alert:", m)
    if dry_run:
        return msgs
    noc.append_jsonl(noc.VAR / "speedwatch.jsonl", [line])
    path.write_text(json.dumps(state))
    if msgs and noc.TG.token() and noc.TG.chat_id():
        for m in msgs:
            noc.TG.send(noc.TG.chat_id(), m)
    return msgs


if __name__ == "__main__":
    import io
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        import app as noc_app
    run(noc_app, dry_run="--dry-run" in sys.argv)
