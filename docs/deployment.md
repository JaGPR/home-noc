# Deploying the NOC

The NOC is one small Python service (FastAPI + Uvicorn) with a single-page UI.
It runs best in its own Debian container on Proxmox: about 2 cores, 2 GB RAM and 16 GB disk.

## 1. Create the container (Proxmox host)
```sh
CTID=120 IP=192.0.2.20/24 GW=192.0.2.1 DNS=192.0.2.10 \
SSH_KEY=/root/.ssh/authorized_keys sh deploy/create-lxc.sh
```
Any Debian 12/13 machine or VM works too. Skip to step 2.

## 2. Install the app (inside the container)
```sh
apt-get install -y git && git clone https://github.com/JaGPR/home-noc /root/home-noc
cd /root/home-noc && sh deploy/install.sh
```
On the first install it creates `/etc/noc/config.json` (from the example),
`/etc/noc/secrets.env` (with a generated admin password), and the NOC's own SSH
key, and prints the public key. Upgrades keep all three.

## 3. Describe your network: `/etc/noc/config.json`
Start from [`config/config.example.json`](../config/config.example.json):

| Key | Meaning |
|---|---|
| `site_name` | Title shown in the UI |
| `devices[]` | Health checks: `target` is an IP (ping) or `host:port` (TCP), for devices that ignore ping from other subnets |
| `dns.vip`, `dns.piholes[]` | The keepalived VIP, and each Pi-hole's URL, the **name** of its password variable in `secrets.env`, and its MAC (used to show which node holds the VIP) |
| `router.host` | OpenWrt router reached over SSH with the NOC key; also used by the Router and MultiWAN editors ([openwrt.md](openwrt.md)) |
| `router.wan_interfaces` | Interfaces the UI may restart (each restart is previewed and confirmed) |
| `router.trunk_device` | Device carrying VLANs to the switch (`bond`, `eth1`, …); VLAN *N* = `<trunk>.N` |
| `router.ont_caches[]` | DDM cache files from [luci-app-vsol-ddm](https://github.com/AliLostInTheDark/luci-app-vsol-ddm) / [luci-app-hsgq-ddm](https://github.com/JaGPR/luci-app-hsgq-ddm) to show fibre signal |
| `access_point.host` | Optional second OpenWrt device for Wi-Fi clients |
| `switch.mac`, `switch.ports` | The Omada-managed switch |
| `switch.protected_ports` | Ports where any change needs the port name typed to confirm (uplinks, servers) |
| `omada.*` | Controller URL, `omadacId`, site ID |
| `proxmox.url`, `proxmox.node` | Proxmox API (read-only token) |
| `new_network_defaults` | Address suggestions when creating a VLAN, e.g. `10.0.{n}.1/24` |

## 4. Credentials: `/etc/noc/secrets.env` (chmod 600)
Never commit this file. Each integration gets **its own, minimal** credential:

| System | How |
|---|---|
| **OpenWrt router / AP** | Add the NOC's public key to `/etc/dropbear/authorized_keys` on each |
| **Pi-hole v6** | Settings → Web interface / API → *Configure app password*, one per Pi-hole |
| **Omada** | Global/Settings → Platform Integration → Open API → Add New App, **Client mode**, admin role for your site. Put the client ID/secret in secrets.env; the omadacId and site ID go in config.json (the site ID is listed under `/openapi/v1/{omadacId}/sites`) |
| **Proxmox** | `sh deploy/proxmox-token.sh` on the Proxmox host creates a **PVEAuditor (read-only)** token |
| **NOC login** | `NOC_ADMIN_USER` / `NOC_ADMIN_PASS` (first install; replaced by `NOC_ADMIN_PASS_HASH` once you change it in Settings → NOC password); `NOC_SESSION_KEY` signs sessions. Forgot it: **Forgot password?** on the sign-in page sends a one-time code to the paired Telegram chat, or run `noc-passwd` as root on the NOC |

Then: `systemctl restart noc` and open `http://<noc-ip>:8080`.

## 5. Optional: remote access through Cloudflare Tunnel
Add a public hostname (e.g. `noc.example.com` → `http://<noc-ip>:8080`) **and an Access
application with a policy in front of it**. Never publish it without Access: the NOC can
pause DNS and change VLANs. If `cloudflared` runs on another host, add that host's IP to
`--forwarded-allow-ips` in `deploy/noc.service`.

## Troubleshooting
| Symptom | Check |
|---|---|
| A card shows an error | The error text names the source: SSH key missing, wrong token, unreachable host |
| Device shows down but works | It may ignore ping from other subnets. Use a `host:port` target |
| Fibre data "stale" | The router's DDM poller stopped; restart it (`/etc/init.d/vsol_ddm restart`) |
| Omada errors `-1001` | Wrong request shape; check controller version (tested with 6.2) |
| `journalctl -u noc` | Service log |
