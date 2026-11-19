"""GET /metrics: the NOC's network readings in the Prometheus text format, for long-term graphs in Grafana.

Off unless secrets.env has NOC_METRICS_TOKEN; Prometheus then sends it as "Authorization: Bearer <token>".
Everything comes from what the NOC already reads (the router, app/usage.py, the hourly speed watch): asking for
/metrics adds no load on the router beyond the cached router read.

  noc_wan_up{wan,isp,device}                     1 when the interface is up
  noc_wan_rate_bits{wan,isp,direction}           bits per second over the last 30 s read (app/usage.py)
  noc_wan_bytes_total{wan,isp,direction}         data used since counting began (a counter: use increase())
  noc_wan_month_bytes{wan,isp,direction}         data used this calendar month
  noc_device_month_bytes{device,name,direction}  this month's busiest devices (usage.DEVICES_SHOWN)
  noc_wan_latency_ms / noc_wan_loss_percent{wan} mwan3's health check of each link
  noc_wan_speedtest_mbps{wan,isp}                the last hourly speed test (app/speedwatch.py)
  noc_ont_rx_dbm / noc_ont_tx_dbm / noc_ont_temp_celsius{ont}   fibre modules
  noc_usage_since_seconds                        when counting began (unix time)
"""
import time

import speedtab
import usage

HELP = {
    "noc_wan_up": ("gauge", "1 when the WAN interface is up"),
    "noc_wan_rate_bits": ("gauge", "Bits per second on the WAN over the last read (about 30 s)"),
    "noc_wan_bytes_total": ("counter", "Bytes through the WAN since the NOC began counting"),
    "noc_wan_month_bytes": ("gauge", "Bytes through the WAN this calendar month"),
    "noc_device_month_bytes": ("gauge", "Bytes a LAN device used this calendar month"),
    "noc_wan_latency_ms": ("gauge", "mwan3 health check latency"),
    "noc_wan_loss_percent": ("gauge", "mwan3 health check packet loss"),
    "noc_wan_speedtest_mbps": ("gauge", "Last hourly speed test of the WAN"),
    "noc_ont_rx_dbm": ("gauge", "Fibre module received optical power"),
    "noc_ont_tx_dbm": ("gauge", "Fibre module transmitted optical power"),
    "noc_ont_temp_celsius": ("gauge", "Fibre module temperature"),
    "noc_usage_since_seconds": ("gauge", "Unix time the data-used counting began"),
}


def esc(v):
    return str(v).replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")


def render(samples):
    """[(name, {label: value}, number)] -> the exposition text, one HELP/TYPE per metric, in HELP's order."""
    by = {}
    for name, labels, value in samples:
        if value is None:
            continue
        by.setdefault(name, []).append((labels, value))
    out = []
    for name in sorted(by, key=lambda n: list(HELP).index(n) if n in HELP else len(HELP)):
        kind, text = HELP.get(name, ("gauge", name))
        out += [f"# HELP {name} {text}", f"# TYPE {name} {kind}"]
        for labels, value in by[name]:
            lab = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items() if v is not None)
            out.append(f"{name}{{{lab}}} {number(value)}" if lab else f"{name} {number(value)}")
    return "\n".join(out) + "\n"


def number(v):
    """Exact: whole numbers as integers (byte counters must not lose digits), others as Python writes floats."""
    f = float(v)
    return str(int(f)) if f.is_integer() and abs(f) < 2 ** 63 else repr(f)


def samples(store, rates, router, groups, monitored, devices, hist, now=None):
    """Everything /metrics shows, as render() takes it."""
    r = router if isinstance(router, dict) else {}
    rows, _ = speedtab.wan_rows(r.get("interfaces", []), monitored)
    isp_of = {m: g for g, members in (groups or {}).items() for m in members}
    out = []
    for w in rows:
        out.append(("noc_wan_up", {"wan": w["name"], "isp": isp_of.get(w["name"]), "device": w.get("device")}, 1 if w["up"] else 0))
    for wan, (d, u) in sorted((rates or {}).items()):
        for direction, v in (("down", d), ("up", u)):
            out.append(("noc_wan_rate_bits", {"wan": wan, "isp": isp_of.get(wan), "direction": direction}, v))
    totals = {}
    for per in (store.get("months") or {}).values():
        for wan, (d, u) in per.items():
            t = totals.setdefault(wan, [0, 0])
            t[0] += d
            t[1] += u
    month = time.strftime("%Y-%m", time.localtime(now or time.time()))
    this = (store.get("months") or {}).get(month, {})
    for wan in sorted(totals, key=speedtab.natural):
        for i, direction in ((0, "down"), (1, "up")):
            out.append(("noc_wan_bytes_total", {"wan": wan, "isp": isp_of.get(wan), "direction": direction}, totals[wan][i]))
            out.append(("noc_wan_month_bytes", {"wan": wan, "isp": isp_of.get(wan), "direction": direction}, this.get(wan, [0, 0])[i]))
    v = usage.view(store, rows, groups, month, r, devices)
    if v["month"] == month:
        for d in v["devices"]:
            label = d["name"] or (d["ips"][0] if d["ips"] else d["key"])
            out.append(("noc_device_month_bytes", {"device": d["key"], "name": label, "direction": "down"}, d["down_bytes"]))
            out.append(("noc_device_month_bytes", {"device": d["key"], "name": label, "direction": "up"}, d["up_bytes"]))
    for name, m in sorted((r.get("mwan") or {}).items()):
        out.append(("noc_wan_latency_ms", {"wan": name}, m.get("latency")))
        out.append(("noc_wan_loss_percent", {"wan": name}, m.get("loss")))
    last = {}
    for h in hist or []:
        for wan, mbps in (h.get("mbps") or {}).items():
            if isinstance(mbps, (int, float)):
                last[wan] = mbps
    for wan, mbps in sorted(last.items()):
        out.append(("noc_wan_speedtest_mbps", {"wan": wan, "isp": isp_of.get(wan)}, mbps))
    for o in r.get("onts") or []:
        for metric, key in (("noc_ont_rx_dbm", "rx_dbm"), ("noc_ont_tx_dbm", "tx_dbm"), ("noc_ont_temp_celsius", "temp_c")):
            if isinstance(o.get(key), (int, float)):
                out.append((metric, {"ont": o.get("name")}, o[key]))
    out.append(("noc_usage_since_seconds", {}, store.get("since")))
    return out
