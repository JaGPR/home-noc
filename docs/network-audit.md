# Home network audit playbook

A repeatable, **read-only** checklist for an OpenWrt + Proxmox + Pi-hole + Omada
home lab. Every item below found a real problem on the network this project was
built on. Run the checks, then fix in order of severity.

## 1. Inventory
```sh
# From any LAN host: who answers?
for i in $(seq 1 254); do (ping -c1 -W1 192.0.2.$i >/dev/null && echo 192.0.2.$i) & done; wait
# On the router: DHCP leases (names + MACs), ARP, Wi-Fi clients
cat /tmp/dhcp.leases; cat /proc/net/arp
for i in $(ubus list 'hostapd.*' | cut -d. -f2); do iwinfo $i assoclist; done
```
Phones with randomised MACs often ignore ping, so the router's lease list is the real inventory.

## 2. Router (OpenWrt)
| Check | Command | Red flag |
|---|---|---|
| Default routes and metrics | `ip route \| grep ^default` | A route **without a metric** (metric 0) from a management or secondary interface. It silently becomes the router's own default route. Fix: untick *Use default gateway* (`defaultroute=0`) on management links; give every WAN an explicit metric |
| Multi-WAN health | `mwan3 status` | Lines tracked with a **single** ping target (`8.8.8.8`); policies with **no fallback member** |
| PPPoE stability | `logread \| grep -iE 'pppd\|terminated\|PADO'` | Repeated "terminated" → session limits, line or ONT problems. Many sessions on one account can trigger ISP limits |
| CGNAT | Interface IP in `100.64.0.0/10`, or `ip` ≠ `curl --interface <wan> ifconfig.me` | No inbound connections possible: use a tunnel (Cloudflare/Tailscale) or ask the ISP for a public IP |
| Wi-Fi | `uci show wireless` | Wrong `country`, `psk-mixed` (WPA1), 2.4 GHz served by one radio only |
| DHCP DNS | `uci show dhcp.lan.dhcp_option` | Clients not given the Pi-hole VIP; clients with DNS overridden by hand |
| Stale config | `uci show dhcp \| grep host`, `uci show network` | Reserved IPs for devices that no longer exist, unused VLAN devices |
| Management access | `grep ^root: /etc/shadow` on **every** OpenWrt box | **Empty root password**, especially on dumb APs, means anyone on the LAN is root |
| Tooling | `sleep 0.5; echo $?` | BusyBox `sleep` rejects fractions: scripts that pace telnet sessions with fractional sleeps silently don't wait |
| Tooling | `command -v timeout` | Stock BusyBox has no `timeout`; telnet-based tools need `coreutils-timeout` |

## 3. Fibre ONT / GPON
| Check | Healthy (Class B+ GPON) |
|---|---|
| Rx power | −8 to −27 dBm; below about −24 dBm leaves little margin |
| Tx power | 0 to +4 dBm |
| ONU state | O5 (operation) |
| FEC uncorrectable / BIP errors | 0, or not increasing |
| Uptime | No unexplained resets. Resets with mains flickers point to inverter changeover; use a DC mini-UPS for router + ONT |

[luci-app-vsol-ddm](https://github.com/AliLostInTheDark/luci-app-vsol-ddm) and
[luci-app-hsgq-ddm](https://github.com/JaGPR/luci-app-hsgq-ddm) show all of this in LuCI.

## 4. Proxmox
```sh
pvesm status; lvs; zpool status -x                  # storage + thin-pool fill
for d in $(lsblk -dn -o NAME,TYPE | awk '$2=="disk"{print $1}'); do smartctl -H /dev/$d; done
cat /etc/pve/jobs.cfg                                # are there backup jobs at all?
qm list; pct list; for id in $(pct list | awk 'NR>1{print $1}'); do pct exec $id -- df -h /; done
systemctl --failed; apt list --upgradable | wc -l
```
| Red flag | Why |
|---|---|
| **No backup jobs** | One dead NVMe loses every VM |
| A guest disk ≥ 95 % | Services crash (e.g. a torrent container at 99.5 %) |
| Sum of guest RAM > host RAM | Swapping or OOM kills under load |
| Enterprise repos without a subscription | Updates fail silently |
| `logrotate` failing | Logs grow until the disk fills |

## 5. DNS (Pi-hole)
- Both nodes answer and block: `dig @<node> doubleclick.net` → `0.0.0.0`
- Which node owns the VIP: `ip neigh show <vip>` (compare MACs)
- Health-checked failover and sync in place: see [pihole-ha.md](pihole-ha.md)
- Broken blocklist URLs (`https:/` typos, GitHub `blob/` pages instead of raw files)

## 6. Exposure
- Every public hostname on a Cloudflare tunnel: `curl -s -o /dev/null -w '%{http_code}' https://<host>`.
  A **200 without a redirect to `*.cloudflareaccess.com`** means the admin page is public.
- Dead tunnel entries (502s) pointing at old IPs.
- Telnet/SSH with passwords on ISP boxes: change the defaults and keep them off the internet.

## 7. Dependency map
Draw what depends on what and mark single points of failure. See [architecture.md](architecture.md).

## Severity order that worked
1. Admin pages exposed to the internet · shared/known passwords · empty root passwords
2. No backups · full disks
3. Unstable WAN (sessions, power to ONT/router)
4. DNS failover without health check · unsynced Pi-holes
5. Remote access depending on one box · RAM overcommit
6. Routing hygiene (metrics, fallbacks, tracking targets)
7. Cleanup (stale leases, dead VLANs, broken tunnel entries)
