"""Proxmox cluster view for the NOC: pure functions over Proxmox API answers.

app.collect_proxmox() fetches; these shape the answers (and the alert checks) so they test without the app.
A host that is not in a cluster is a cluster of one: "cluster" is None and "nodes" lists just that host.
"""


def pct(used, total):
    return round(100 * used / total, 1) if total else None


def storage_rows(rows):
    return [{"name": s["storage"], "type": s.get("type"), "used_pct": pct(s.get("used", 0), s.get("total")),
             "total_gb": round(s.get("total", 0) / 1e9)} for s in rows or [] if s.get("active")]


def guest_row(g):
    return {"vmid": g["vmid"], "name": g.get("name"), "type": g["type"], "status": g["status"], "node": g.get("node"),
            "cpu_pct": round(100 * g.get("cpu", 0), 1), "mem_pct": pct(g.get("mem", 0), g.get("maxmem")),
            "disk_pct": pct(g.get("disk", 0), g.get("maxdisk")) if g["type"] == "lxc" else None,
            "uptime": g.get("uptime")}


def node_row(name, info, status, storage, guests, local):
    """One node. info: its /cluster/status entry ({} without a cluster); status: /nodes/<n>/status or None."""
    online = status is not None if not info else bool(info.get("online"))
    mine = [g for g in guests if g["node"] == name]
    row = {"name": name, "ip": info.get("ip"), "online": online, "local": local,
           "guests_running": sum(g["status"] == "running" for g in mine), "guests": len(mine),
           "cpu_pct": None, "mem_pct": None, "mem_gb": None, "uptime": None, "version": None, "cpu_model": None,
           "storage": storage_rows(storage)}
    if status:
        mem = status.get("memory") or {}
        row.update(cpu_pct=round(100 * status.get("cpu", 0), 1), mem_pct=pct(mem.get("used", 0), mem.get("total")),
                   mem_gb=round(mem.get("total", 0) / 2**30, 1) if mem.get("total") else None,
                   uptime=status.get("uptime"), version=status.get("pveversion"),
                   cpu_model=(status.get("cpuinfo") or {}).get("model"))
    return row


def view(main, main_status, resources, main_storage, cluster_status=(), statuses=None, storages=None):
    """The /api/proxmox answer. main: the configured node (its fields stay at the top level, as before);
    statuses / storages: {node: answer} for the other online nodes (absent = offline or unreadable)."""
    statuses, storages = dict(statuses or {}), dict(storages or {})
    statuses[main], storages[main] = main_status, main_storage
    guests = sorted((guest_row(g) for g in resources or []), key=lambda g: g["vmid"])
    entries = {e["name"]: e for e in cluster_status or () if e.get("type") == "node"}
    head = next((e for e in cluster_status or () if e.get("type") == "cluster"), None)
    names = sorted(entries or [main], key=lambda n: (n != main, n))
    nodes = [node_row(n, entries.get(n, {}), statuses.get(n), storages.get(n), guests, n == main) for n in names]
    return {"node": main, "cpu_pct": round(100 * main_status["cpu"], 1),
            "mem_pct": pct(main_status["memory"]["used"], main_status["memory"]["total"]),
            "uptime": main_status.get("uptime"), "version": main_status.get("pveversion"),
            "guests": guests, "storage": storage_rows(main_storage), "nodes": nodes,
            "cluster": {"name": head.get("name"), "quorate": bool(head.get("quorate")),
                        "nodes": len(nodes), "online": sum(n["online"] for n in nodes)} if head else None}


def checks(px):
    """Alert checks from a view(): {key: (ok, text)}. One per cluster node, plus quorum for a cluster."""
    out = {}
    if not isinstance(px, dict) or not px.get("cluster"):
        return out  # a single host is already watched by its ping check
    for n in px.get("nodes") or []:
        where = f" ({n['ip']})" if n.get("ip") else ""
        out[f"pve:{n['name']}"] = (n["online"], f"Proxmox node {n['name']}{where}: {'online' if n['online'] else 'offline'}")
    c = px["cluster"]
    out["pve:quorum"] = (c["quorate"], f"Proxmox cluster {c['name']}: " + (
        f"quorate, {c['online']}/{c['nodes']} nodes online" if c["quorate"] else
        "lost quorum: running guests keep going, but none can be started, stopped or changed"))
    return out
