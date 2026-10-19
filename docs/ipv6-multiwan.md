# IPv6 load balancing over several WAN sessions (OpenWrt + mwan3, NAT66)

mwan3 balances IPv4 per connection because NAT hides the LAN behind whichever WAN a connection
leaves through. Plain IPv6 has no NAT: a LAN address belongs to one ISP prefix, and the ISP drops
packets that leave through a session whose prefix they don't match. To balance IPv6 the same way
as IPv4, this setup gives the LAN one prefix and masquerades IPv6 (NAT66) on every WAN.

Tested on OpenWrt 25.12 (mwan3 2.12) with several PPPoE sessions from one ISP, each getting its
own `/128` address and delegated `/64`.

## Design
- Every WAN session runs DHCPv6. **Only one** delegates its prefix to the LAN.
- The LAN also gets a **ULA**. Its addresses never change, so they suit internal services (e.g. a
  Pi-hole IPv6 VIP, see [pihole-ha.md](pihole-ha.md)). The delegated prefix is still needed:
  clients with only a ULA prefer IPv4 for internet destinations (RFC 6724).
- `masq6` on the wan zone. mwan3 gets IPv6 interfaces, members with the same weights as their
  IPv4 counterparts, the IPv6 members in the same policy, and an IPv6 catch-all rule.

What you give up: no inbound IPv6 to LAN devices. Behind CGNAT you have no inbound IPv4 either.

## Configuration (example: PPPoE sessions `wan1` … `wan3`)
```sh
for n in 1 2 3; do
  uci set network.wan$n.ipv6='1'                       # IPv6CP on the PPP link, no automatic _6 interface
  uci set network.wan${n}_6=interface
  uci set network.wan${n}_6.proto='dhcpv6'
  uci set network.wan${n}_6.device="@wan$n"
  uci set network.wan${n}_6.reqaddress='try'
  uci set network.wan${n}_6.reqprefix='auto'
  uci set network.wan${n}_6.sourcefilter='0'            # default route for any source, not just its own prefix
  uci set network.wan${n}_6.peerdns='0'
  uci set network.wan${n}_6.delegate="$([ $n = 1 ] && echo 1 || echo 0)"
  # unique DHCPv6 client ID per session (DUID-LL from the session's MAC), see pitfall 1
  mac=$(cat /sys/class/net/$(uci get network.wan$n.device)/address | tr -d :)
  uci set network.wan${n}_6.clientid="00030001$mac"
  # one default route per session with its own metric, see pitfall 2
  uci set network.v6default_wan$n=route6
  uci set network.v6default_wan$n.interface="wan${n}_6"
  uci set network.v6default_wan$n.target='::/0'
  uci set network.v6default_wan$n.metric="$((600 + n))"
  uci add_list firewall.@zone[1].network="wan${n}_6"    # the wan zone
done
uci set network.globals.ula_prefix='fd00:db8:1::/48'    # generate your own
uci set network.lan.ip6assign='64'
uci add_list network.lan.ip6class='wan1_6'
uci add_list network.lan.ip6class='local'
uci set firewall.@zone[1].masq6='1'
uci set dhcp.lan.ra='server'
uci set dhcp.lan.dhcpv6='disabled'                      # SLAAC only
uci set dhcp.lan.ra_slaac='1'
uci set dhcp.lan.dns_service='0'                        # or advertise a Pi-hole IPv6 VIP (pihole-ha.md)
```
mwan3, per session (track two targets, either answering = online):
```
config interface 'wan1_6'
	option enabled '1'
	option family 'ipv6'
	list track_ip '2606:4700:4700::1111'
	list track_ip '2001:4860:4860::8888'
	option reliability '1'

config member 'mwan1_6'
	option interface 'wan1_6'
	option metric '1'
	option weight '4'          # same weight as the IPv4 member of this session

config policy 'balanced'
	list use_member 'mwan1'
	list use_member 'mwan1_6'  # IPv4 and IPv6 members can share a policy

config rule 'policy_all_v6'
	option family 'ipv6'
	option proto 'all'
	option dest_ip '::/0'
	option use_policy 'balanced'
```
Apply with `uci commit` and `reload_config`, then `/etc/init.d/mwan3 restart` once all `_6`
interfaces are up. Changing `ipv6` on a PPPoE interface reconnects that session.

## Pitfalls (all found on a real router)
1. **Only one session gets IPv6.** Every `odhcp6c` sends the same DUID
   (`network.globals.dhcp_default_duid`), and the ISP's DHCPv6 server answers one "client" only.
   Give each `wanN_6` its own `clientid`.
2. **mwan3 marks all but one IPv6 session offline.** The default routes come from router
   advertisements with a fixed metric (512) that overrides the interface `metric`. Identical routes
   on different devices collide, the kernel keeps one, and mwan3 copies no default route into the
   other sessions' tables. A `route6` per session (`::/0`, no gateway, since PPP is point-to-point,
   and a distinct metric) fixes it.
3. **The LAN never gets an address.** Check `cat /proc/sys/net/ipv6/conf/br-lan/disable_ipv6`.
   An earlier IPv6-off setup often sets `option ipv6 '0'` on every `config device`, including the
   bridge. Remove it from the bridge only.
4. **mwan3 rules default to `family ipv4`.** Per-device rules (`src_ip 192.0.2.x`) don't cover that
   device's IPv6, which then follows the IPv6 catch-all. To keep such a device on its IPv4 policy,
   reject its IPv6 by MAC so it falls back to IPv4 at once:
   ```
   config rule 'v6_block_policy_devices'
   	option family 'ipv6'
   	option src 'lan'
   	option dest 'wan'
   	option proto 'all'
   	list src_mac '00:00:5e:00:53:01'
   	option target 'REJECT'
   ```
5. **`ping -6 -I pppoe-wanN` fails while mwan3 says online.** mwan3 rejects unmarked traffic that
   is bound to a device. Test with `mwan3 use wanN_6 ping -6 …` instead.
6. **Hardware flow offload does not take NAT66 flows** (MediaTek PPE), so IPv6 downloads use the
   CPU. Expect somewhat less than IPv4 at the top end, and watch per-core load.
7. **Counting traffic with offload on.** Offloaded flows bypass the interface counters
   (`/sys/class/net/*/statistics`). Read the switch port counters instead: `ethtool -S wan`
   (`RxBytes` / `TxBytes`).

## Check
```sh
mwan3 interfaces | grep _6            # all online
mwan3 policies                        # IPv6 members with the same split as IPv4
ip -6 addr show dev br-lan            # ULA ::1 and the delegated ::1
```
From a LAN client, open several new connections to an "what is my IP" service over IPv6
(`curl -6 https://api64.ipify.org`): the answers should cycle through the sessions' addresses.
