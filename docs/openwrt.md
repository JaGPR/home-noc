# Router (OpenWrt) and MultiWAN editing

The NOC edits the OpenWrt router's `network`, `firewall`, `dhcp` and `mwan3` configs over
SSH (`app/openwrt.py`). It covers what LuCI's Network → Devices / Interfaces and the
MultiWAN Manager pages do for a multi-WAN home router.

## Safety: apply with automatic rollback

Every change goes through preview → confirm, like the rest of the NOC, and is applied
the way LuCI's "apply with rollback" works:

1. **No mixing with LuCI.** If `network`, `firewall`, `dhcp` or `mwan3` has unsaved
   (staged) changes, for example from an open LuCI session, the NOC refuses. Staged
   changes are shared, so committing would also save someone else's half-finished edit.
2. **Backup.** The router copies the four configs to `/tmp/noc-rb-<id>/`.
3. **All or nothing.** The change is one `uci batch`. `uci batch` exits 0 even when a
   line fails, so the NOC judges it by its error output. On any error (other than
   deleting an option that isn't set), everything is reverted and nothing is applied.
4. **Apply.** Only the four configs are committed, then `reload_config` reloads the
   services whose config changed.
5. **Watchdog.** A detached timer (`setsid`) restores the backup after **90 s** unless
   confirmed.
6. **Verify.** The NOC reconnects and waits until every WAN that was online before the
   change is online again (the interface being changed is allowed to take its time).
   Then it confirms. If that doesn't happen within ~45 s, it rolls back at once.

A change that cuts the router off from the NOC therefore undoes itself. Every action is
in the audit log. PPPoE passwords are write-only: they are never read back, shown in
previews, or logged.

## Interfaces and Network devices pages

| Page | You can |
|---|---|
| Interfaces | Diagnose, restart; edit (protocol, device, address, gateway, DNS, metric, default route, peer DNS, IPv6, bring up on boot, firewall zone, DHCP server), Delete (also removes its zone membership, DHCP section, mwan3 interface/member and policy membership), **+ Create interface** |
| Network devices | **+ Create device** (VLAN 802.1q or MAC VLAN), edit MAC / IPv6 / MTU, delete unused devices (in-use ones are refused) |

### New interface wizard

One form, applied as one change:

1. **Device:** create a VLAN device on the trunk (e.g. `bond.200`) or a MAC VLAN, or
   use an existing device. Optional MAC address, IPv6, MTU.
2. **Interface:** static, DHCP client, PPPoE or unmanaged, with the matching options.
3. **Firewall and DHCP:** zone (`lan` / `wan` / none), DHCP server with start, size and
   lease time for static networks.
4. **MultiWAN:** for `wan` interfaces, add to mwan3: tracking IPs, reliability, ping
   interval and timeout, failures/successes until down/up, a member (metric, weight) and
   the policies to join.

### New network (VLAN): switch and router in one step

**Switch → + New network** asks for everything on one form, and nothing happens until
you confirm the preview:

| Part | Choose |
|---|---|
| Switch (Omada) | name, VLAN ID, IGMP snooping, **carry it to the router** (tags it on the switch↔router LAG; on by default, without it the router never sees the VLAN) |
| DHCP | the router (gateway, prefix, range, lease, DNS for clients), an upstream device, or nobody. Addresses are suggested from `new_network_defaults.subnet_template` |
| Router step 1: device | switch on to create `<trunk>.<VLAN>` (optional MAC, IPv6) |
| Router step 2: interface | switch on to create the interface: name, protocol (the router DHCP choice gives a static gateway plus a DHCP server handing out your DNS), metric, zone; or use an existing device |
| Router step 3: MultiWAN | only for the `wan` zone: tracking, member metric/weight, policies |

Any combination works (switch only, plus device, up to the interface, or all of it).
**Cancel** at any point and nothing is created anywhere. On confirm, the switch part runs
first (network, then LAG tag), then the router part with automatic rollback. **If the
router part fails, the LAG tag is removed and the new Omada network is deleted again**,
so nothing is left half-done.

### Several MAC VLANs at once (e.g. one PPPoE session each)

**Network devices → + MAC VLANs** (or the button on a device's row) creates several MAC
VLAN devices on one device in one change:

- **How many** (1-16), a device name prefix and first number (e.g. `wanmac6`, `wanmac7`),
  a **fixed random MAC for each** (recommended: ISPs often bind a PPPoE session to the
  MAC) or the kernel default, the MAC VLAN mode, IPv6.
- **An interface on each** (optional): PPPoE (one username/password for all), DHCP
  client, static or unmanaged. Interface name prefix and first number, LuCI's gateway
  options, and the firewall zone.
- **Per device** a table with the device name, interface name and **gateway metric**
  (counting up from the first, editable per row), plus **address and gateway** for
  static interfaces.
- **MultiWAN** for the `wan` zone: one tracked interface and member per device,
  added to the chosen policies.

### Deleting a network (VLAN)

**Delete** on a network checks the router for setup on that VLAN: the `<trunk>.<VLAN>`
device, devices built on it (e.g. MAC VLANs), interfaces on those, and their mwan3
tracking/members. If there is any, it asks with three separate options, **all off by
default**, so only what you tick is removed:

| Option | Removes |
|---|---|
| Device | the VLAN device(s) |
| Interface | the interface(s), with their firewall-zone and DHCP entries |
| MultiWAN (mwan3) | tracking, members and their policy entries (warns if a policy is left empty) |

Ticking **Device** also ticks Interface (a device can't go while interfaces use it), and
Interface also ticks MultiWAN (mwan3 can't track a deleted interface). **MultiWAN alone**
is allowed: stop tracking but keep the interface. The router part runs **first**, with
automatic rollback; if it fails, the switch VLAN is left untouched.

The sidebar's **Router (OpenWrt)** group has **Interfaces** (+ Create interface),
**Network devices** (+ Create device) and **MultiWAN** as their own pages.

## MultiWAN tab

| Section | You can |
|---|---|
| Interfaces | **+ Add interface** (any router interface not yet tracked, optionally with a member already in chosen policies); edit tracking (enabled, tracking IPs, reliability, pings, timeout, intervals, failures/successes until down/up, state at boot); delete (also its members and their policy entries, warns if a policy is left empty) |
| Members | Create, edit (interface, metric, weight), delete (also removed from policies) |
| Policies | Create, edit (members, last resort), delete (refused while a rule uses it) |
| Rules | Create, edit (source/destination address and port, protocol, sticky + timeout, policy), delete, move up/down (first match wins) |
