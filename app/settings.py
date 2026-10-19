"""Settings tab: every part of config.json (and the secrets the NOC needs) is editable here.

Each section is an action kind (settings_<section>) that goes through the usual
preview -> confirm -> audit flow. Saving rewrites config.json (previous copy kept as
config.json.bak) and, for passwords and tokens, secrets.env; then apply_config() makes
the change live without a restart.

Secrets are write-only: the page only learns whether one is set. A blank secret field
keeps the current value. Secrets never appear in previews or the audit log.
"""
import ipaddress
import re
import subprocess

NAME = re.compile(r"[\w .()+-]{1,40}")
LABEL = re.compile(r"[^\"'<>\\\r\n]{1,60}")  # free-text labels: any text except quotes, < >, backslash, line breaks
HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,252}[A-Za-z0-9])?")
URL = re.compile(r"https?://[A-Za-z0-9.-]+(:\d{1,5})?/?")
MAC = re.compile(r"([0-9a-f]{2}:){5}[0-9a-f]{2}")
PATH = re.compile(r"/[\w./-]{1,200}")


MONITORING_DEFAULTS = {"check_every_s": 30, "confirm_checks": 2, "ont_stale_s": 180,
                       "backup_alerts": True, "backup_success": True, "backup_store_warn_pct": 85, "backup_long_h": 4}


def _secret_set(noc, key):
    return bool(key and noc.E.get(key))


def current(noc):
    """Values for the Settings tab. Secrets are reported only as set / not set."""
    C, R = noc.C, noc.ROUTER
    tg = {**MONITORING_DEFAULTS, **(C.get("telegram") or {})}
    om, pv, sw = C.get("omada") or {}, C.get("proxmox") or {}, C.get("switch") or {}
    return {
        "general": {"site_name": noc.SITE_NAME},
        "dns": {"vip": noc.DNS_VIP or "", "vip6": noc.DNS_VIP6 or "",
                "piholes": [{"name": p["name"], "url": p.get("url", ""), "mac": p.get("mac", ""),
                             "password_set": _secret_set(noc, p.get("password_env"))}
                            for p in noc.DNS.get("piholes", [])]},
        "connections": {"router_host": R.get("host", ""), "ap_host": (C.get("access_point") or {}).get("host", ""),
                        "switch_mac": sw.get("mac", ""), "switch_ports": sw.get("ports", 10),
                        "omada_url": om.get("url", ""), "omadac_id": om.get("omadac_id", ""), "site_id": om.get("site_id", ""),
                        "omada_client_set": _secret_set(noc, "OMADA_CLIENT_ID") and _secret_set(noc, "OMADA_CLIENT_SECRET"),
                        "proxmox_url": pv.get("url", ""), "proxmox_node": pv.get("node", ""),
                        "pve_token_set": _secret_set(noc, "PVE_TOKEN")},
        "monitoring": {k: tg[k] for k in MONITORING_DEFAULTS},
        "onts": [{"name": o["name"], "path": o["path"]} for o in noc.ONT_CACHES],
        "protected_ports": {str(k): v for k, v in sorted(noc.PROTECTED_PORTS.items())},
        "wan": {"groups": noc.wan_groups(), "paths": R.get("wan_paths") or {},
                "candidates": noc.wan_candidates(),
                "devices": [d["name"] for d in C.get("devices", [])],
                "onts": [o["name"] for o in noc.ONT_CACHES], "switch_ports": noc.SWITCH_PORTS},
    }


def _ipv4(v, what):
    try:
        ipaddress.IPv4Address(v)
    except ValueError:
        raise ValueError(f"{what} must be an IPv4 address") from None


def _host(v, what):
    if not HOST.fullmatch(v or ""):
        raise ValueError(f"{what} must be an IP address or hostname")


def _change(detail, label, old, new, secret=False):
    if secret:
        if new:
            detail.append(f"{label}: will be replaced")
        return
    if (old or "") != (new or ""):
        detail.append(f"{label}: {old or '(none)'} → {new or '(none)'}")


def _why(ex):
    """Short reason for a failed Pi-hole login test."""
    code = getattr(getattr(ex, "response", None), "status_code", None)
    if code in (401, 403):
        return "wrong app password"
    if code:
        return f"HTTP {code}"
    name = type(ex).__name__
    if "Timeout" in name:
        return "no answer (wrong address, or it's down)"
    if "Connection" in name:
        return "connection refused or unreachable (wrong address or port?)"
    return name


def _secret_value(v, what):
    v = (v or "").strip()
    if "\n" in v or "\r" in v or len(v) > 512:
        raise ValueError(f"{what} is not valid")
    return v


def _host_key(host):
    """Fetch a host's SSH key (trust on first use). Returns (known_hosts lines, fingerprints)."""
    r = subprocess.run(["ssh-keyscan", "-T", "5", "-t", "ed25519,ecdsa,rsa", host], capture_output=True, text=True, timeout=20)
    lines = [l for l in r.stdout.splitlines() if l and not l.startswith("#")]
    if not lines:
        raise ValueError(f"can't reach SSH on {host} (port 22) to read its host key")
    fp = subprocess.run(["ssh-keygen", "-lf", "-"], input="\n".join(lines) + "\n", capture_output=True, text=True)
    return lines, [" ".join(l.split()[1:2] + l.split()[-1:]) for l in fp.stdout.splitlines()]


def plan(noc, kind, p):
    """Returns (summary, detail, confirm_word, run, cache_keys) like plan_action."""
    C, detail = noc.C, []

    def commit(mutate, secrets=None, after=None):
        def run():
            if secrets:
                noc.set_secrets(secrets)
            mutate()
            noc.save_config()
            if after:
                after()
            noc.apply_config()
            return {"saved": True}
        return run

    if kind == "settings_general":
        name = (p.get("site_name") or "").strip()
        if not NAME.fullmatch(name):
            raise ValueError("site name: 1-40 letters, digits, spaces or . ( ) + - _")
        _change(detail, "Site name", noc.SITE_NAME, name)
        if not detail:
            raise ValueError("nothing changed")
        return "Update general settings", detail, None, commit(lambda: C.__setitem__("site_name", name)), []

    if kind == "settings_dns":
        vip = (p.get("vip") or "").strip()
        if vip:
            _ipv4(vip, "VIP")
        vip6 = (p.get("vip6", noc.DNS_VIP6 or "") or "").strip()
        if vip6:
            try:
                vip6 = str(ipaddress.IPv6Address(vip6))
            except ValueError:
                raise ValueError("IPv6 VIP must be an IPv6 address, e.g. fd00:db8:1::10") from None
        old = {x["name"]: x for x in noc.DNS.get("piholes", [])}
        new, secrets, names = [], {}, set()
        for x in p.get("piholes") or []:
            name, url = (x.get("name") or "").strip(), (x.get("url") or "").strip().rstrip("/")
            mac = (x.get("mac") or "").strip().lower().replace("-", ":")
            if not NAME.fullmatch(name):
                raise ValueError(f"Pi-hole name '{name}': 1-40 letters, digits, spaces or . ( ) + - _")
            if name in names:
                raise ValueError(f"two Pi-holes are called '{name}'")
            names.add(name)
            if not URL.fullmatch(url + "/"):
                raise ValueError(f"{name}: URL must look like http://10.0.0.5 or http://10.0.0.5:8080")
            if mac and not MAC.fullmatch(mac):
                raise ValueError(f"{name}: MAC must look like aa:bb:cc:dd:ee:ff")
            was = old.get(x.get("original_name") or name)
            slug = re.sub(r"^PI_?HOLE_?", "", re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")) or "X"
            env = (was or {}).get("password_env") or f"PIHOLE_{slug}_PASS"  # "Pi-hole VM" -> PIHOLE_VM_PASS
            pw = _secret_value(x.get("password"), f"{name} password")
            if not pw and not noc.E.get(env):
                raise ValueError(f"{name}: enter its app password (Pi-hole → Settings → Web interface / API → App password)")
            if pw:
                secrets[env] = pw
            entry = {"name": name, "url": url, "password_env": env, **({"mac": mac} if mac else {})}
            new.append(entry)
            if not was:
                detail.append(f"Add {name}: {url}" + (f", MAC {mac}" if mac else ""))
            else:
                if was["name"] != name:
                    detail.append(f"Rename {was['name']} → {name}")
                _change(detail, f"{name} URL", was.get("url"), url)
                _change(detail, f"{name} MAC", was.get("mac"), mac)
            if pw:
                detail.append(f"{name} app password: will be {'replaced' if was and noc.E.get(env) else 'set'}")
            if pw or not was or (was.get("url") != url):  # check the login now, so a typo shows in the preview
                try:
                    noc.PiHole(name, url, pw or noc.E.get(env, ""))._auth()
                    detail.append(f"{name}: login test OK")
                except Exception as ex:
                    detail.append(f"⚠ {name}: login test failed ({_why(ex)}). It will be saved anyway.")
        kept = {x.get("original_name") or x.get("name") for x in p.get("piholes") or []}
        detail += [f"Remove {n}" for n in old if n not in kept]
        _change(detail, "VIP", noc.DNS_VIP, vip)
        _change(detail, "IPv6 VIP", noc.DNS_VIP6, vip6)
        if not detail:
            raise ValueError("nothing changed")

        def mutate():
            C.setdefault("dns", {})["vip"] = vip or None
            C["dns"]["vip6"] = vip6 or None
            C["dns"]["piholes"] = new
        return "Update DNS servers", detail, None, commit(mutate, secrets), ["dns"]

    if kind == "settings_connections":
        cur = current(noc)["connections"]
        v = {k: (str(p.get(k)).strip() if p.get(k) is not None else "") for k in cur}
        _host(v["router_host"], "Router address")
        if v["ap_host"]:
            _host(v["ap_host"], "Access point address")
        mac = v["switch_mac"].upper().replace(":", "-")
        if mac and not re.fullmatch(r"([0-9A-F]{2}-){5}[0-9A-F]{2}", mac):
            raise ValueError("switch MAC must look like AA-BB-CC-DD-EE-FF")
        ports = int(v["switch_ports"] or 0)
        if not 4 <= ports <= 64:
            raise ValueError("switch ports must be 4-64")
        for k in ("omada_url", "proxmox_url"):
            if v[k] and not URL.fullmatch(v[k].rstrip("/") + "/"):
                raise ValueError(f"{k.replace('_', ' ')} must look like https://10.0.0.5:8043")
        for k in ("omadac_id", "site_id", "proxmox_node"):
            if v[k] and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", v[k]):
                raise ValueError(f"{k.replace('_', ' ')} has invalid characters")
        secrets = {}
        for field, key in (("omada_client_id", "OMADA_CLIENT_ID"), ("omada_client_secret", "OMADA_CLIENT_SECRET"), ("pve_token", "PVE_TOKEN")):
            val = _secret_value(p.get(field), field.replace("_", " "))
            if val:
                if key == "PVE_TOKEN" and not re.fullmatch(r"[^@\s]+@[^!\s]+![^=\s]+=\S+", val):
                    raise ValueError("Proxmox token must look like user@realm!tokenid=secret")
                secrets[key] = val
            _change(detail, field.replace("_", " ").replace("pve", "Proxmox").capitalize(), None, val, secret=True)
        labels = {"router_host": "Router address", "ap_host": "Access point address", "switch_ports": "Switch ports",
                  "omada_url": "Omada URL", "omadac_id": "Omada controller ID", "site_id": "Omada site ID",
                  "proxmox_url": "Proxmox URL", "proxmox_node": "Proxmox node"}
        for k, label in labels.items():
            _change(detail, label, str(cur[k]), v[k])
        _change(detail, "Switch MAC", cur["switch_mac"], mac)
        keys = []
        for k in ("router_host", "ap_host"):
            if v[k] and v[k] != cur[k]:
                lines, fps = _host_key(v[k])
                keys += lines
                detail.append(f"New SSH host key for {v[k]}: {'; '.join(fps)} (it must already have the NOC's public key)")
        if not detail:
            raise ValueError("nothing changed")

        def mutate():
            C.setdefault("router", {})["host"] = v["router_host"]
            if v["ap_host"]:
                C.setdefault("access_point", {})["host"] = v["ap_host"]
            else:
                C.pop("access_point", None)
            C.setdefault("switch", {}).update(mac=mac, ports=ports)
            C.setdefault("omada", {}).update(url=v["omada_url"].rstrip("/"), omadac_id=v["omadac_id"], site_id=v["site_id"])
            C.setdefault("proxmox", {}).update(url=v["proxmox_url"].rstrip("/"), node=v["proxmox_node"])

        def pin_keys():
            if keys:
                with open(noc.KNOWN_HOSTS, "a") as f:
                    f.write("\n".join(keys) + "\n")
        return "Update connections", detail, None, commit(mutate, secrets, pin_keys), []

    if kind == "settings_monitoring":
        limits = {"check_every_s": (10, 600, "Check every (s)"), "confirm_checks": (1, 10, "Checks in a row before alerting"),
                  "ont_stale_s": (60, 3600, "ONT reading counts as stale after (s)"),
                  "backup_store_warn_pct": (50, 99, "Backup storage warning at (% used)"),
                  "backup_long_h": (1, 48, "Backup counts as stuck after (hours)")}
        flags = {"backup_alerts": "Backup alerts", "backup_success": "Message on successful backups"}
        cur = current(noc)["monitoring"]
        new = {}
        for k, (lo, hi, label) in limits.items():
            n = int(p.get(k, cur[k]))
            if not lo <= n <= hi:
                raise ValueError(f"{label}: must be {lo}-{hi}")
            new[k] = n
            _change(detail, label, str(cur[k]), str(n))
        for k, label in flags.items():
            v = p.get(k, cur[k])
            if not isinstance(v, bool):
                raise ValueError(f"{label}: must be on or off")
            new[k] = v
            _change(detail, label, "on" if cur[k] else "off", "on" if v else "off")
        if not detail:
            raise ValueError("nothing changed")
        return "Update monitoring", detail, None, commit(lambda: C.setdefault("telegram", {}).update(new)), []

    if kind == "settings_onts":
        old = {o["name"]: o for o in noc.ONT_CACHES}
        new, renames, names = [], {}, set()
        for o in p.get("onts") or []:
            name, path = (o.get("name") or "").strip(), (o.get("path") or "").strip()
            if not NAME.fullmatch(name):
                raise ValueError(f"ONT name '{name}': 1-40 letters, digits, spaces or . ( ) + - _")
            if name in names:
                raise ValueError(f"two ONT sources are called '{name}'")
            names.add(name)
            if not PATH.fullmatch(path):
                raise ValueError(f"{name}: path must be an absolute file path on the router, e.g. /tmp/vsol_ddm_cache.json")
            new.append({"name": name, "path": path})
            orig = o.get("original_name") or name
            if orig in old and orig != name:
                renames[orig] = name
                detail.append(f"Rename {orig} → {name}")
            if orig not in old:
                detail.append(f"Add {name}: {path}")
            else:
                _change(detail, f"{name} path", old[orig]["path"], path)
        kept = {o.get("original_name") or o.get("name") for o in p.get("onts") or []}
        detail += [f"Remove {n}" for n in old if n not in kept]
        if not detail:
            raise ValueError("nothing changed")

        def mutate():
            C.setdefault("router", {})["ont_caches"] = new
            for path in (C["router"].get("wan_paths") or {}).values():
                if path.get("ont") in renames:
                    path["ont"] = renames[path["ont"]]
                elif path.get("ont") and path["ont"] not in names:
                    path.pop("ont")
        return "Update fibre ONT sources", detail, None, commit(mutate), ["router"]

    if kind == "settings_protected":
        new = {}
        for port, label in (p.get("ports") or {}).items():
            n, label = int(port), str(label or "").strip()
            if not 1 <= n <= noc.SWITCH_PORTS:
                raise ValueError(f"port {n}: must be 1-{noc.SWITCH_PORTS}")
            if not label:
                raise ValueError(f"port {n}: give it a short label (what is plugged in)")
            if not LABEL.fullmatch(label):
                raise ValueError(f"port {n}: label must be up to 60 characters, without quotes, < > or backslash")
            new[str(n)] = label
        old = {str(k): v for k, v in noc.PROTECTED_PORTS.items()}
        for k in sorted(set(old) | set(new), key=int):
            _change(detail, f"Port {k}", old.get(k), new.get(k))
        if not detail:
            raise ValueError("nothing changed")
        return "Update protected switch ports", detail, None, commit(
            lambda: C.setdefault("switch", {}).__setitem__("protected_ports", new)), ["switch"]

    raise ValueError("unknown settings section")
