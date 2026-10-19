# Highly available Pi-hole (two nodes, one DNS address)

Two Pi-holes share one **virtual IP (VIP)** with keepalived. Clients only ever use
the VIP. If the primary's Pi-hole stops answering, the backup takes the VIP within
a few seconds. nebula-sync keeps the backup's blocklists and rules identical.

```
clients ──DNS──► VIP 192.0.2.10
                  ├── primary  192.0.2.11  (keepalived MASTER, priority 254)
                  └── backup   192.0.2.12  (keepalived BACKUP, priority 253)
                        ▲ nebula-sync every 30 min (primary → backup)
```

Measured on a real pair: **DNS was down 2.8 s** when Pi-hole was stopped on the
primary, and **zero** when the primary rebooted (keepalived hands the VIP over on
shutdown). Hand-back after recovery had no gap.

Files are in [`pihole-ha/`](../pihole-ha).

## 1. Give each Pi-hole a fixed address
Each node needs a static IP. **Don't leave DHCP running alongside it** (see
[Pitfalls](#pitfalls)).

## 2. keepalived with a real health check (both nodes)
```sh
apt install keepalived dnsutils
install -m 755 -o root -g root pihole-ha/check_pihole.sh /etc/keepalived/check_pihole.sh
cp pihole-ha/keepalived-primary.conf.example /etc/keepalived/keepalived.conf   # backup node: keepalived-backup.conf.example
# edit interface, IPs, VIP, auth_pass
keepalived -t && systemctl enable --now keepalived
```
Without `track_script`, keepalived only knows the *machine* is up. A crashed
Pi-hole on a running VM keeps the VIP and **DNS is down for everyone**. The check
script requires `pihole-FTL` to be running *and* answering a real query.

## 3. Point DHCP at the VIP
OpenWrt: **Network → Interfaces → LAN → DHCP Server → Advanced → DHCP-Options**:
`6,192.0.2.10`. Hand out **only** the VIP. Listing the node IPs as well lets clients
skip failover.

Also set **conditional forwarding** on the primary (it syncs to the backup) so
local `.lan` names resolve: Settings → DNS →
`true,192.0.2.0/24,192.0.2.1,lan`.

## 4. Sync the backup from the primary (nebula-sync, on the backup)
1. On **both** Pi-holes create an app password: Settings → Web interface / API → *Configure app password*.
2. On the **replica**, allow app passwords to change settings (otherwise sync fails with HTTP 403):
   `pihole-FTL --config webserver.api.app_sudo true`
3. Install the binary from the [nebula-sync releases](https://github.com/lovelaze/nebula-sync/releases)
   (armv7 for a 32-bit Raspberry Pi OS, arm64/amd64 otherwise) to `/usr/local/bin/nebula-sync`.
4. Configure and start:
   ```sh
   install -m 600 pihole-ha/nebula-sync.env.example /etc/nebula-sync.env   # edit URLs + app passwords
   cp pihole-ha/nebula-sync.service /etc/systemd/system/
   mkdir -p /etc/systemd/system/nebula-sync.service.d
   cp pihole-ha/wait-for-pihole.conf /etc/systemd/system/nebula-sync.service.d/
   cp pihole-ha/pihole-nightly-gravity.cron /etc/cron.d/pihole-nightly-gravity
   systemctl daemon-reload && systemctl enable --now nebula-sync
   journalctl -u nebula-sync -n 20     # expect "Sync completed"
   ```
Make all changes on the **primary** from now on. Edits on the backup are overwritten.

Only blocklists, rules, groups, clients and local DNS records are synced. Web ports
and interface names differ per node and are deliberately excluded.

## 5. Test failover
On the primary: `systemctl stop pihole-FTL; sleep 20; systemctl start pihole-FTL`.
Watch both logs: `journalctl -u keepalived -f`. Expected: primary → FAULT, backup → MASTER
within ~4 s, then the primary takes the VIP back ~3 s after Pi-hole is healthy.

From a client, keep querying a fresh name through the VIP during the test, e.g.
`while :; do dig +short @192.0.2.10 $RANDOM.example.com; sleep 1; done`.

## 6. IPv6 DNS VIP (optional)
Give the Pi-holes an IPv6 DNS address that, like the IPv4 VIP, never changes and fails over.

1. **A stable prefix on the LAN.** A delegated ISP prefix can change on every reconnect, so put
   a ULA next to it on the router (OpenWrt), then reload the network:
   ```sh
   uci set network.globals.ula_prefix='fd00:db8:1::/48'   # generate your own random fdXX:XXXX:XXXX::/48
   uci add_list network.lan.ip6class='local'              # keep the ISP class too (e.g. 'wan_6')
   uci commit network && ubus call network reload
   ```
   Devices then get one address from each prefix: the ISP one for the internet and the ULA one for
   the LAN. A LAN with *only* a ULA makes clients prefer IPv4 for internet sites (RFC 6724), so
   keep both.
2. **The VIP in keepalived (both nodes).** Add it to the existing instance as an *excluded*
   address (see the commented block in `keepalived-*.conf.example`), test, reload:
   ```
   virtual_ipaddress_excluded {
     fd00:db8:1::10/64 dev eth0
   }
   ```
   `keepalived -t -f /etc/keepalived/keepalived.conf && systemctl reload keepalived`.
   Reload the backup first; a reload on the master keeps it master, so DNS does not drop.
3. **Advertise it** as the IPv6 DNS server (router advertisement RDNSS), once the VIP answers:
   ```sh
   uci add_list dhcp.lan.dns='fd00:db8:1::10'
   uci set dhcp.lan.dns_service='1'
   uci commit dhcp && /etc/init.d/odhcpd reload
   ```
   Until then keep `dns_service '0'`, so clients are never told about a DNS server that doesn't answer yet.
4. **Answer only local networks** now that the nodes may have public IPv6 addresses:
   `sudo pihole-FTL --config dns.listeningMode LOCAL` on the primary. nebula-sync copies it to the
   replica (`SYNC_CONFIG_DNS` excludes only `interface,port`), so setting it on the replica alone
   is undone at the next sync. Clients that reach the VIP through a Tailscale subnet router still
   work, because the subnet router translates them to its own LAN address.

Check from a client: `nslookup -type=AAAA doubleclick.net fd00:db8:1::10` must return `::`.

## Pitfalls
- **Static IP + DHCP at the same time (Ubuntu/netplan + NetworkManager).** A profile with
  `ipv4.method: auto` *and* a static address gets both, so a second, changing IP appears.
  Setting the installer's `dhcp4: false` and running `netplan apply` can make NetworkManager
  **drop the real profile and adopt the VIP as a new profile with no gateway**, which breaks
  DNS for everyone. **Before any network change on a Pi-hole node, move the VIP to the other
  node first** (raise its priority temporarily), then fix the profile with
  `nmcli con mod <profile> ipv4.method manual` and `nmcli con up <profile>`.
- **app_sudo** is required on the replica (see step 4), or every sync fails with 403.
- **Stray IPv6 routers on the LAN.** A freshly flashed OpenWrt box or an access point with its own
  ULA and `odhcpd` can advertise prefixes (and itself as DNS) next to your router, letting clients
  bypass the Pi-holes over IPv6. On access points: remove `network.globals.ula_prefix` and
  `lan.ip6assign`, set `dhcp.lan.ignore '1'` and `/etc/init.d/odhcpd disable`. To see who
  advertises: `tcpdump -i br-lan -n -e -v 'icmp6 and ip6[40] == 134'`.
- **Boot race:** without `wait-for-pihole.conf`, the first sync after boot fails until the restart timer kicks in.
- A hot Raspberry Pi throttles (`vcgencmd get_throttled`). Add a heatsink if it's the backup you rely on.
