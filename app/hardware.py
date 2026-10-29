"""The Proxmox host's fans and RGB lighting, through noc-hw on the host (deploy/proxmox/noc-hw).

The NOC's key on the host may run noc-hw and nothing else (install-noc-hw.sh). The NOC only
reads status and stores a wanted mode; the host's own noc-hw guard applies it, runs every fan
at 100 % when the CPU or GPU gets hot, and hands every fan back to the board / GPU when it stops.
Lighting (RAM, GPU, board) goes through OpenRGB on the host; each change is read back there.

Config (config.json): "hardware": {"host": "<Proxmox host address>"}
"""
import json
import re
import shlex

TARGETS = {"board": "Case and CPU fans", "gpu": "GPU fan"}
LIGHTS = {"ram": "RAM lighting", "gpu": "GPU lighting", "board": "Motherboard lighting"}
MODES = {"auto": "Auto (board / GPU firmware)", "quiet": "Quiet curve", "balanced": "Balanced curve",
         "full": "Full speed", "manual": "Fixed speed"}


def host(noc):
    return (noc.C.get("hardware") or {}).get("host")


def call(noc, *args, timeout=15):
    if not host(noc):
        raise RuntimeError("hardware.host is not set in config.json")
    out = json.loads(noc.ssh(host(noc), "", timeout=timeout, command="noc-hw " + " ".join(shlex.quote(str(a)) for a in args)))
    if isinstance(out, dict) and out.get("error"):
        raise RuntimeError(out["error"])
    return out


def collect(noc):
    return call(noc, "status")


def describe(target, mode, pct, name=None):
    what = "follow Case and CPU fans" if mode == "group" else MODES[mode] + (f" at {pct} %" if mode == "manual" else "")
    return f"{name or TARGETS[target]}: {what}"


def plan_light(noc, p):
    group, mode, color = p.get("group"), p.get("mode"), (p.get("color") or "").lstrip("#") or None
    if group not in LIGHTS:
        raise ValueError(f"group must be one of {', '.join(LIGHTS)}")
    cur = noc.cached("hardware", 5, lambda: collect(noc))
    g = ((cur.get("lights") or {}).get("groups") or {}).get(group) if isinstance(cur, dict) else None
    if not g:
        raise ValueError(f"no {LIGHTS[group].lower()} found on the host")
    if mode not in g["modes"]:
        raise ValueError(f"{LIGHTS[group]} modes: {', '.join(g['modes'])}")
    if color and not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        raise ValueError("colour must be 6 hex digits (RRGGBB)")
    color = color.upper() if color and mode != "Off" else None
    names = sorted({d["name"] for d in g["devices"]})
    detail = [f"Now: {g.get('mode') or 'mixed or not reported'} on {len(g['devices'])} device(s): {', '.join(names)}"]
    if any(d.get("mode") == "Direct" for d in g["devices"]):
        detail.append("This device does not report its mode, so the change can't be read back")
    if group == "ram":
        detail.append("Written to the RAM's lighting controller over SMBus (OpenRGB); the host re-applies it after a reboot")

    def run():
        out = call(noc, "light", group, mode, *([color] if color else []), timeout=60)
        return {"group": group, "mode": mode, "color": color, "verified": out.get("verified")}
    return f"{LIGHTS[group]}: {mode}" + (f" #{color}" if color else ""), detail, None, run, ["hardware"]


def plan(noc, kind, p):
    """hw_fan {target, mode, pct}, hw_light {group, mode, color}: (summary, detail, confirm word, run, cache keys)."""
    if kind == "hw_light":
        return plan_light(noc, p)
    if kind != "hw_fan":
        raise ValueError("unknown action")
    target, mode = p.get("target"), p.get("mode")
    one = isinstance(target, str) and re.fullmatch(r"fan\d+", target)   # one board header with its own mode
    if target not in TARGETS and not one:
        raise ValueError(f"target must be one of {', '.join(TARGETS)} or a single fan")
    if mode not in MODES and not (one and mode == "group"):
        raise ValueError(f"mode must be one of {', '.join(MODES)}" + (" or group" if one else ""))
    pct = None
    if mode == "manual":
        try:
            pct = int(p.get("pct"))
        except (TypeError, ValueError):
            raise ValueError("a fixed speed needs a fan % (0-100)")
        if not 0 <= pct <= 100:
            raise ValueError("fan % must be 0-100")
    cur = noc.cached("hardware", 5, lambda: collect(noc))
    allfans = (cur.get("fans") or []) if isinstance(cur, dict) else []
    fans = [f for f in allfans if (f.get("id") == target if one else f.get("target") == target)]
    if one and not fans:
        raise ValueError(f"{target} is not a fan the host controls")
    detail = [f"Now: {', '.join(f['name'] + ' ' + str(f['rpm']) + ' rpm' + (' (auto)' if f['auto'] else '') for f in fans) or 'no fans read'}"]
    if mode == "manual" and fans:
        floor = max(f.get("min_pct", 0) for f in fans)
        if pct < floor:
            detail.append(f"Below the {floor} % minimum: the host runs these fans at {floor} %")
    if one:
        board = ((cur.get("wanted") or {}).get("board") or {"mode": "auto"})
        detail.append("Back to the Case and CPU fans setting (" + describe("board", board["mode"], board.get("pct")).split(": ", 1)[1] + ")"
                      if mode == "group" else "Only this fan; the other case fans keep the Case and CPU fans setting")
    if mode not in ("auto", "group"):
        detail.append(f"The host's guard runs them at 100 % above {cur.get('hot_c', 85)} °C (CPU or GPU) "
                      "and hands them back to the firmware if it stops")

    def run():
        call(noc, "set", target, mode, *([pct] if mode == "manual" else []))
        return {"target": target, "mode": mode, "pct": pct}
    return describe(target, mode, pct, fans[0]["name"] if one else None), detail, None, run, ["hardware"]
