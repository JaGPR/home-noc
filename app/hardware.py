"""The Proxmox host's fans, through noc-hw on the host (deploy/proxmox/noc-hw).

The NOC's key on the host may run noc-hw and nothing else (install-noc-hw.sh). The NOC only
reads status and stores a wanted mode; the host's own noc-hw guard applies it, runs every fan
at 100 % when the CPU or GPU gets hot, and hands every fan back to the board / GPU when it stops.

Config (config.json): "hardware": {"host": "<Proxmox host address>"}
"""
import json
import shlex

TARGETS = {"board": "Case and CPU fans", "gpu": "GPU fan"}
MODES = {"auto": "Auto (board / GPU firmware)", "quiet": "Quiet curve", "balanced": "Balanced curve",
         "full": "Full speed", "manual": "Fixed speed"}


def host(noc):
    return (noc.C.get("hardware") or {}).get("host")


def call(noc, *args):
    if not host(noc):
        raise RuntimeError("hardware.host is not set in config.json")
    out = json.loads(noc.ssh(host(noc), "", timeout=15, command="noc-hw " + " ".join(shlex.quote(str(a)) for a in args)))
    if isinstance(out, dict) and out.get("error"):
        raise RuntimeError(out["error"])
    return out


def collect(noc):
    return call(noc, "status")


def describe(target, mode, pct):
    what = MODES[mode] + (f" at {pct} %" if mode == "manual" else "")
    return f"{TARGETS[target]}: {what}"


def plan(noc, kind, p):
    """hw_fan {target, mode, pct}: (summary, detail, confirm word, run, cache keys to refresh)."""
    if kind != "hw_fan":
        raise ValueError("unknown action")
    target, mode = p.get("target"), p.get("mode")
    if target not in TARGETS:
        raise ValueError(f"target must be one of {', '.join(TARGETS)}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    pct = None
    if mode == "manual":
        try:
            pct = int(p.get("pct"))
        except (TypeError, ValueError):
            raise ValueError("a fixed speed needs a fan % (0-100)")
        if not 0 <= pct <= 100:
            raise ValueError("fan % must be 0-100")
    cur = noc.cached("hardware", 5, lambda: collect(noc))
    fans = [f for f in (cur.get("fans") or []) if f.get("target") == target] if isinstance(cur, dict) else []
    detail = [f"Now: {', '.join(f['name'] + ' ' + str(f['rpm']) + ' rpm' + (' (auto)' if f['auto'] else '') for f in fans) or 'no fans read'}"]
    if mode == "manual" and fans:
        floor = max(f.get("min_pct", 0) for f in fans)
        if pct < floor:
            detail.append(f"Below the {floor} % minimum: the host runs these fans at {floor} %")
    if mode != "auto":
        detail.append(f"The host's guard runs them at 100 % above {cur.get('hot_c', 85)} °C (CPU or GPU) "
                      "and hands them back to the firmware if it stops")

    def run():
        call(noc, "set", target, mode, *([pct] if mode == "manual" else []))
        return {"target": target, "mode": mode, "pct": pct}
    return describe(target, mode, pct), detail, None, run, ["hardware"]
