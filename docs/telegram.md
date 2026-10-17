# Telegram alerts and bot

The NOC runs a background watcher and an optional Telegram bot in the same process as
the web UI (`app/notify.py`). Nothing is sent until you add a bot token and pair a chat.

## What is watched

Every `check_every_s` seconds (default 30):

| Check | Problem when |
|---|---|
| Devices (Devices tab) | ping / TCP check fails |
| Each monitored WAN | mwan3 status is not `online` (or the interface is down) |
| All WANs | no monitored WAN is online: sent as **ALL INTERNET LINKS ARE DOWN** |
| Router | the NOC cannot reach it over SSH (WAN and fibre checks pause meanwhile) |
| Each Pi-hole | its API does not answer |
| Ad-blocking | blocking is not `enabled` on a Pi-hole (includes deliberate pauses, with the time left) |
| DNS VIP | moves from one Pi-hole to another (sent as a "moved" event) |
| Each fibre ONT | no fresh reading from its router poller, LOS/LOF/other alarm, not in O5, or RX/TX power or temperature outside the **ONT's own warning limits** |

A check must disagree with its last reported state `confirm_checks` times in a row
(default 2, so about a minute) before a message goes out, in both directions, so one
lost ping or a reading on the edge of a limit doesn't page you. Several changes found in the
same round arrive as one message. If Telegram can't be reached (for example every WAN
is down), messages wait in an outbox and are sent later, marked with the time they
happened.

Monitored WANs are `router.wan_interfaces` plus every member of a WAN group.

## Setting it up

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. On the NOC host, add it to the secrets file (it is never written to config.json, logs or the UI):
   ```sh
   nano /etc/noc/secrets.env      # add: TELEGRAM_BOT_TOKEN=<token>
   ```
   The NOC re-reads the file within a minute. No restart is needed.
3. In the NOC, open **Alerts** → **Pair**. Send the `/pair 123456` it shows to your bot
   within 10 minutes. That private chat becomes the only one the bot answers; other
   chats are ignored.
4. Press **Send test**.

## Commands

| Command | Does |
|---|---|
| `/status` | Internet links, devices, DNS, fibre at a glance |
| `/wan` | Every monitored WAN with status, IP, latency; the WAN groups |
| `/dns` | Each Pi-hole, blocking state, VIP owner |
| `/optics` | Each ONT: RX/TX power vs its limits, temperature, reading age |
| `/devices` | Devices that are down (`/devices all` lists every device) |
| `/problems` | Every check that is currently not OK |
| `/restart <link\|group>` | Restart one WAN (`/restart wan3`) or a whole group (`/restart isp-a`) |
| `/pause <minutes>` / `/resume` | Pause ad-blocking on all Pi-holes (`0` = until resumed) / resume it |

Actions reply with the same preview the web UI shows and run only after you press
**Confirm** (the button expires after 2 minutes). They are written to the audit log with
`via: telegram chat <id>`. Changes the web UI protects with a typed confirmation are
refused over Telegram.

## WAN groups

Groups name sets of router interfaces, e.g. `isp-a = wan1…wan4`. Edit them in
**Alerts → WAN groups** (each interface can be in one group; saving goes through
preview → confirm), or in `config.json`:

```json
"router": { "wan_groups": { "isp-a": ["wan1", "wan2"], "isp-b": ["wan5"] } }
```

A group restart runs `ifup` on each member, 3 s apart; mwan3 moves traffic to the
other links meanwhile.

## Settings

`config.json` → `"telegram"` (all optional):

| Key | Default | Meaning |
|---|---|---|
| `check_every_s` | 30 | seconds between checks |
| `confirm_checks` | 2 | consecutive readings needed before alerting |
| `ont_stale_s` | 180 | an ONT reading older than this counts as "not reporting" |
| `mgmt_grace` | – | `{"<device>": seconds}`: for an ISP box that briefly drops its management address, alert only once it has been down that long, or at once if its WAN group is fully down or its fibre is not OK. Its ONT poller staleness is held with it |
| `chat_id` | – | set by pairing; remove (or press **Unpair**) to stop messages |

Alert history is kept in `/var/lib/noc/alerts.jsonl` and shown in the Alerts tab.

## Why is a link down? (diagnosis)

When a WAN alert fires, the NOC walks that link's path and adds the cause to the
message. `/diagnose <link|group>` in Telegram and the **Diagnose** button on the Router
tab run the same check on demand. Layers, in the order traffic depends on them; the
first one that fails is the cause, and later layers are marked as following from it:

| Layer | Checks | Example causes |
|---|---|---|
| Router config | interface exists, not `disabled`, not stopped by hand | "disabled in the router config", "stopped at 14:02:10 (ifdown / LuCI Stop)" |
| Router link | device chain down to the cable: VLAN → bond and each LAG member, or macvlan → port | "router↔switch LAG has no link on any cable", "no cable link on router port wan" |
| Switch (Omada) | LAG and the ISP-facing port enabled, linked, carrying the WAN's VLAN | "no link on switch port 8: SFP stick unplugged or dead", "VLAN 100 is not on port 8" |
| ISP box | the ONT/ONU answers on its management address | "doesn't answer: powered off or hung" |
| Fibre | LOS/LOF, OLT registration (O5), RX above the alarm level | "no light on the fibre (LOS)", "not registered with the ISP (O3)" |
| ISP session | DHCP lease / PPPoE session, with pppd's own exit reason | "ISP's DHCP isn't answering", "ISP rejected the PPPoE login" |
| Beyond the ISP | the mwan3 track IP (and gateway) answer through this link | "gateway answers but 8.8.8.8 doesn't" |

Which switch port, ISP box (a tracked device name) and ONT (an `ont_caches` name)
belong to a link is set per WAN group in `config.json`:

```json
"router": { "wan_paths": {
  "isp-a": { "switch_port": 3, "isp_device": "Fibre ONT", "ont": "Fibre ONT" },
  "isp-b": { "isp_device": "Cable modem" } } }
```

The router side (VLAN, bond, ports) is discovered from the router itself. Pings for the
last layer go through the link with `mwan3 use`, like mwan3track. A gateway that ignores
ping is not treated as a fault.
