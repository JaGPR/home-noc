"""The Proxmox hosts' fans and RGB lighting, through noc-hw on each host (deploy/proxmox/noc-hw).

The NOC's key on a host may run noc-hw and nothing else (install-noc-hw.sh). The NOC only
reads status and stores a wanted mode; the host's own noc-hw guard applies it, runs every fan
at 100 % when the CPU or GPU gets hot, and hands every fan back to the board / GPU when it stops.
Lighting (RAM, GPU, board) goes through OpenRGB on the host; each change is read back there.
An ASUS laptop host (noc-hw kind "asus") has its two fans and an ASUS profile instead of headers and a GPU fan.

Config (config.json): "hardware": {"host": "<main Proxmox host address>",
                                   "hosts": {"<node name>": "<address>", ...}}   (more hosts, optional)
"""
import json
import re
import shlex

TARGETS = {"board": "Case and CPU fans", "gpu": "GPU fan"}
ASUS_TARGETS = {"board": "Both fans", "profile": "ASUS profile"}
LIGHTS = {"ram": "RAM lighting", "gpu": "GPU lighting", "board": "Motherboard lighting"}
MODES = {"auto": "Auto (board / GPU firmware)", "quiet": "Quiet curve", "balanced": "Balanced curve",
         "full": "Full speed", "manual": "Fixed speed"}


def others(noc):
    """{name: address} of the hosts besides the main one."""
    return dict((noc.C.get("hardware") or {}).get("hosts") or {})


def host(noc, name=None):
    """The main host's address, or the named extra host's (None when not configured)."""
    return (noc.C.get("hardware") or {}).get("host") if not name else others(noc).get(name)


def cache_key(name=None):
    return f"hardware:{name}" if name else "hardware"


def call(noc, *args, timeout=15, name=None):
    addr = host(noc, name)
    if not addr:
        raise RuntimeError(f"hardware host {name} is not in hardware.hosts" if name else "hardware.host is not set in config.json")
    out = json.loads(noc.ssh(addr, "", timeout=timeout, command="noc-hw " + " ".join(shlex.quote(str(a)) for a in args)))
    if isinstance(out, dict) and out.get("error"):
        raise RuntimeError(out["error"])
    return out


def collect(noc, name=None):
    return call(noc, "status", name=name)


def labels(cur):
    return ASUS_TARGETS if isinstance(cur, dict) and cur.get("kind") == "asus" else TARGETS


def describe(target, mode, pct, name=None, cur=None):
    if target == "profile":
        return f"{name or 'ASUS profile'}: {mode.capitalize()}"
    what = "follow Case and CPU fans" if mode == "group" else MODES[mode] + (f" at {pct} %" if mode == "manual" else "")
    if mode == "group" and labels(cur) is ASUS_TARGETS:
        what = "follow Both fans"
    return f"{name or labels(cur)[target]}: {what}"


def which(noc, p):
    """The host a request is for: None = the main host, else a name from hardware.hosts."""
    name = p.get("host") or None
    if name is not None and name not in others(noc):
        raise ValueError(f"unknown hardware host {name!r}")
    return name


def plan_light(noc, p):
    group, mode, color = p.get("group"), p.get("mode"), (p.get("color") or "").lstrip("#") or None
    name = which(noc, p)
    if group not in LIGHTS:
        raise ValueError(f"group must be one of {', '.join(LIGHTS)}")
    cur = noc.cached(cache_key(name), 5, lambda: collect(noc, name))
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
        out = call(noc, "light", group, mode, *([color] if color else []), timeout=60, name=name)
        return {"group": group, "mode": mode, "color": color, "verified": out.get("verified")}
    return (f"{name + ' ' if name else ''}{LIGHTS[group]}: {mode}" + (f" #{color}" if color else ""),
            detail, None, run, [cache_key(name)])


def plan_profile(noc, name, cur, mode):
    prof = (cur.get("profile") or {}) if isinstance(cur, dict) else {}
    if not prof.get("choices"):
        raise ValueError("this host has no ASUS profile")
    if mode not in prof["choices"]:
        raise ValueError(f"profile must be one of {', '.join(prof['choices'])}")
    detail = [f"Now: {prof.get('current') or 'unknown'}",
              "The firmware's fan and power profile (Quiet limits CPU power and noise, Performance the opposite); "
              "the fan settings below still apply on top of it, and the host puts it back if the firmware changes it"]

    def run():
        call(noc, "set", "profile", mode, name=name)
        return {"target": "profile", "mode": mode}
    return f"{name + ' ' if name else ''}{describe('profile', mode, None)}", detail, None, run, [cache_key(name)]


def plan(noc, kind, p):
    """hw_fan {target, mode, pct, host}, hw_light {group, mode, color, host}: (summary, detail, confirm word, run, cache keys).
    host: a name from hardware.hosts; absent = the main host."""
    if kind == "hw_light":
        return plan_light(noc, p)
    if kind != "hw_fan":
        raise ValueError("unknown action")
    name = which(noc, p)
    target, mode = p.get("target"), p.get("mode")
    cur = noc.cached(cache_key(name), 5, lambda: collect(noc, name))
    known = list((cur.get("targets") if isinstance(cur, dict) else None) or TARGETS)
    if target == "profile" and "profile" in known:
        return plan_profile(noc, name, cur, mode)
    one = isinstance(target, str) and re.fullmatch(r"fan\d+", target)   # one fan with its own mode
    if (target not in known or target == "profile") and not one:
        raise ValueError(f"target must be one of {', '.join(t for t in known if t != 'profile')} or a single fan")
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
    allfans = (cur.get("fans") or []) if isinstance(cur, dict) else []
    fans = [f for f in allfans if (f.get("id") == target if one else f.get("target") == target)]
    if one and not fans:
        raise ValueError(f"{target} is not a fan the host controls")
    detail = [f"Now: {', '.join(f['name'] + ' ' + str(f['rpm']) + ' rpm' + (' (auto)' if f['auto'] else '') for f in fans) or 'no fans read'}"]
    if mode == "manual" and fans:
        floor = max(f.get("min_pct", 0) for f in fans)
        if pct < floor:
            detail.append(f"Below the {floor} % minimum: the host runs these fans at {floor} %")
    group = labels(cur)["board"]
    if one:
        board = ((cur.get("wanted") or {}).get("board") or {"mode": "auto"})
        detail.append(f"Back to the {group} setting (" + describe("board", board["mode"], board.get("pct"), cur=cur).split(": ", 1)[1] + ")"
                      if mode == "group" else f"Only this fan; the other fans keep the {group} setting")
    if mode not in ("auto", "group"):
        detail.append(f"The host's guard runs them at 100 % above {cur.get('hot_c', 85)} °C (CPU or GPU) "
                      "and hands them back to the firmware if it stops")
    if isinstance(cur, dict) and cur.get("kind") == "asus" and mode not in ("auto", "group"):
        detail.append("Written to the laptop firmware as its fan curve, so the laptop keeps following it on its own")

    def run():
        call(noc, "set", target, mode, *([pct] if mode == "manual" else []), name=name)
        return {"target": target, "mode": mode, "pct": pct}
    summary = describe(target, mode, pct, fans[0]["name"] if one else None, cur)
    return f"{name + ' ' if name else ''}{summary}", detail, None, run, [cache_key(name)]
