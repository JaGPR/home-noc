# Home NOC

A single-page command center for a home lab built on **OpenWrt**, **Pi-hole**,
**TP-Link Omada** and **Proxmox**: see every device, link and service in one place,
and make the common changes safely, with a preview, a confirmation, a read-back
check and an audit log for each one.

Plus the field notes that came with it: a **highly available Pi-hole** kit and a
**network audit playbook**.

---

## Features

| Tab | You see | You can do (always preview → confirm) |
|---|---|---|
| **Overview** | Every device up/down (ping or TCP), all WAN links with mwan3 latency/loss, which Pi-hole holds the DNS VIP (and the IPv6 VIP), fibre signal (Rx/Tx/temp/state) from your ONT, Proxmox and router health | — |
| **DNS** | Each Pi-hole: queries, % blocked, blocklist size, top blocked domains, top clients | Pause blocking (5/15/30/60 min or until resumed) and resume, on all Pi-holes at once |
| **Switch** | Every port: link, speed, PoE, profile, **native / untagged / tagged VLANs**; LAGs; networks with their DHCP source | Edit a port's VLANs or profile, enable/disable a port, edit a LAG's VLANs, create/edit/delete networks (VLANs); a new network can also tag a LAG and set up the router side (device, interface, DHCP) in one step, undone if any step fails |
| **Router** | Interfaces (IPv4 and IPv6 addresses and prefixes, gateway, zone, measured latency/loss), network devices, physical ports, ONT details and alarms, DHCP leases | Restart or diagnose a WAN; create / edit / delete devices and interfaces with a LuCI-style wizard (device → interface → firewall + DHCP → mwan3), applied with automatic rollback. See [docs/openwrt.md](docs/openwrt.md) |
| **MultiWAN** | mwan3 interfaces with live status, members, policies, rules in order | Edit tracking; create / edit / delete members, policies and rules; reorder rules |
| **Alerts** | Telegram setup and pairing, open problems (with the diagnosed cause), recent alerts | Pair / unpair Telegram, send a test |
| **Settings** | Every setting the NOC uses; secrets shown only as set / not set | Edit DNS servers (Pi-holes, VIP, app passwords), add / edit / remove ISP boxes (modems, ONTs, fibre sticks), WAN groups and their diagnosis paths, fibre ONT sources, protected ports, router/AP/Omada/Proxmox connections, alert timing, site name. Changes apply live. Change the NOC password; a forgotten one is reset with a one-time code sent to Telegram (or `noc-passwd` on the console) |
| **Wi-Fi** | Every radio on the router and AP, every client with name, IP, signal and rates | — |
| **Proxmox** | Host CPU/RAM, storage fill, every VM/container (CPU, RAM, disk) | Read-only |
| **Audit log** | Every change: when, from where, what, result | — |

**Telegram alerts and bot** (optional): devices, WAN links, Pi-holes, the DNS VIP and fibre optics are checked every 30 s, and a change that holds for two checks is sent to your paired chat. The bot answers `/status`, `/wan`, `/dns`, `/optics` and more, and can restart a WAN or a whole ISP group or pause ad-blocking, each after a Confirm button. It also reports **Proxmox backups**: each finished run with every guest's result, plus failed, stuck or missed backups and a full or offline backup storage (`/backups` lists each guest's latest backup). See [docs/telegram.md](docs/telegram.md).

**PPPoE speed watch** (optional): an ISP can throttle one session of a multi-session PPPoE WAN while the rest stay fast. `app/speedwatch.py`, run hourly by cron (`deploy/noc-speedwatch.cron`), downloads a slice of `speedwatch.url` through every PPPoE interface on the router, retests a slow one, and sends Telegram a message when a session drops below `speedwatch.slow_mbps` and when it recovers. History: `/var/lib/noc/speedwatch.jsonl`.

**On phones**: below 900 px wide the sidebar becomes a slide-in menu and a bottom bar keeps Overview, Router, Switch and Alerts one tap away; on phones every table turns into one card per row, dialogs open as bottom sheets, and controls are touch-sized (fields use 16 px text so iOS doesn't zoom in). Add it to your home screen for a full-screen app.

### Safety model
- **Preview first.** Every change shows exactly what will happen, e.g.
  *"Tagged + IoT (VLAN 20) · Result → untagged: Default | tagged: …"*.
- **Protected items** (uplinks, servers) need their name typed to confirm. Some changes are **refused**:
  editing LAG member ports directly, changing a LAG's native VLAN (it carries the switch's management), editing the built-in management network.
- **Impact warnings.** Removing a VLAN from the uplink names the router interface it would cut;
  deleting a network is refused while any port still carries it.
- **Verified writes.** After a switch change the port is re-read and any mismatch is reported.
- **Least privilege.** Read-only Proxmox token, a dedicated SSH key, Pi-hole app passwords, an Omada API client. No personal passwords.

## Quick start
```sh
# 1. On the Proxmox host: create a Debian container
CTID=120 IP=192.0.2.20/24 GW=192.0.2.1 sh deploy/create-lxc.sh

# 2. Inside the container
git clone https://github.com/JaGPR/home-noc && cd home-noc
sh deploy/install.sh

# 3. Describe your network and add credentials
nano /etc/noc/config.json      # from config/config.example.json
nano /etc/noc/secrets.env      # from config/secrets.env.example
systemctl restart noc          # → http://<container-ip>:8080
```
Full guide: **[docs/deployment.md](docs/deployment.md)**

## Requirements
| Component | Tested with | Used for |
|---|---|---|
| Debian 12/13 (LXC or VM) | Debian 13 on Proxmox 9.2 | Runs the NOC (Python 3, FastAPI, Uvicorn from Debian packages) |
| OpenWrt | 25.12 | Router/AP data over SSH + ubus; mwan3 optional |
| Pi-hole | v6 | DNS stats and blocking control (app passwords) |
| TP-Link Omada controller | 6.2 with an SG2210P switch | Ports, LAGs, VLANs, networks (Open API, client mode) |
| Proxmox VE | 9.2 | Host and guest status (read-only token) |
| Optional | [luci-app-vsol-ddm](https://github.com/AliLostInTheDark/luci-app-vsol-ddm), [luci-app-hsgq-ddm](https://github.com/JaGPR/luci-app-hsgq-ddm) | Fibre ONT signal on the overview |

Any integration you leave out of `config.json` simply shows "not configured".

## Repository layout
```
app/                 NOC backend (app.py), watcher + Telegram bot (notify.py), backup watcher (backups.py), WAN diagnosis (diagnose.py),
                     Settings tab (settings.py), router/mwan3 editing (openwrt.py), UI (static/)
config/              config.example.json, secrets.env.example
deploy/              create-lxc.sh, install.sh, noc.service, proxmox-token.sh
pihole-ha/           keepalived health check + configs, nebula-sync service, gravity cron
tests/               smoke_test.py (backend), ui/ (browser tests, fixture sanitiser, sample data)
docs/
  deployment.md      install, configure, connect each system, Cloudflare Access
  pihole-ha.md       two-node Pi-hole with VIP failover and sync, IPv6 DNS VIP, plus pitfalls
  network-audit.md   read-only audit checklist with commands and red flags
  architecture.md    reference layout, NOC data flow, single points of failure
  telegram.md        alerts, bot commands, pairing, WAN groups
  openwrt.md         router and mwan3 editing, apply-with-rollback
  ipv6-multiwan.md   IPv6 load balancing over several WAN sessions with mwan3 and NAT66
  daily-publish-task.md  the scheduled task that publishes the live NOC here each morning
```

## Tests
```sh
python tests/smoke_test.py                                  # backend, offline (needs the app's Python packages)
pip install ddt playwright && python -m playwright install chromium
python -m unittest discover -s tests/ui -p "test_*.py"     # UI: ~750 data-driven cases
```
The UI tests render the real page in Chromium against `tests/ui/fixtures.sample.json` (no NOC, network or
credentials needed) and check every tab at phone, tablet and desktop sizes: nothing scrolls sideways or spills out
of a card, touch targets and text fields are phone-sized, tables become cards, the drawer and bottom bar work,
dialogs fit the screen, plus unit tests of the page's JavaScript helpers. Without Playwright's Chromium they use an
installed Chrome or Edge. To refresh the fixtures from your own NOC, save its `/api/*` responses to
`tests/fixtures.json` (git-ignored) and run `python tests/ui/sanitize.py tests/fixtures.json tests/ui/fixtures.sample.json`:
it rewrites every address, MAC, ID and name into documentation values and refuses to write while anything real
survives. Put your own ISP and host names in `tests/ui/sanitize.local.json` (git-ignored; format in `sanitize.py`).

## API
All endpoints need a login session. `GET /api/meta`, `/api/overview`, `/api/router`,
`/api/ap`, `/api/dns`, `/api/switch`, `/api/proxmox`, `/api/audit`, `/api/alerts`, `/api/settings`, `/api/openwrt`, `/api/diagnose?target=`.
Changes: `POST /api/actions/preview` → `{id, summary, detail, confirm_word}`, then
`POST /api/actions/confirm {id, confirm_text}`. Previews expire after 2 minutes.

## Security notes
- Keep the NOC on your LAN, or behind **Cloudflare Access / Tailscale**. It can change VLANs and pause DNS.
- `/etc/noc/secrets.env` and `config.json` are root-only (600). Never commit them; `.gitignore` excludes them.
- The UI has its own login on top of any tunnel authentication.

## License
MIT, see [LICENSE](LICENSE).
