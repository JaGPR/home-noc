#!/usr/bin/env python3
"""Write a first /etc/noc/config.json from the installer's answers (environment variables). Never overwrites
a config that has been edited: only one still identical to the example, which deploy/install.sh put there.

Only what is known at install time goes in; every other integration stays "not configured" until it is
set in the NOC's Settings tab. Env: SITE_NAME ROUTER_HOST PVE_HOST PVE_NODE HW_HOST SPEED_URL, NOC_ETC (default /etc/noc).
"""
import json
import os
from pathlib import Path


def build(env):
    router, pve, hw, url = env.get("ROUTER_HOST"), env.get("PVE_HOST"), env.get("HW_HOST"), env.get("SPEED_URL")
    devices = []
    if router:
        devices.append({"name": "Router", "target": router, "group": "core"})
    if pve:
        devices.append({"name": "Proxmox", "target": pve, "group": "server"})
    cfg = {"site_name": env.get("SITE_NAME") or "Home NOC", "devices": devices, "dns": {}, "switch": {}, "omada": {}, "telegram": {}}
    cfg["router"] = {"host": router} if router else {}
    cfg["proxmox"] = {"url": f"https://{pve}:8006", "node": env.get("PVE_NODE", "")} if pve else {}
    if hw:
        cfg["hardware"] = {"host": hw}
    if url:
        cfg["speedwatch"] = {"url": url, "slow_mbps": 100}
    return cfg


def main(env=os.environ):
    etc = Path(env.get("NOC_ETC", "/etc/noc"))
    path = etc / "config.json"
    example = Path(__file__).resolve().parents[1] / "config" / "config.example.json"
    if path.exists() and path.read_text() != example.read_text():
        print(f"{path} has been edited: leaving it as it is")
        return 0
    tmp = etc / "config.json.new"
    tmp.write_text(json.dumps(build(env), indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
