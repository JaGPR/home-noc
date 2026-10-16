#!/bin/sh
# Create the NOC container on a Proxmox VE host. Run as root on the Proxmox host.
#
#   CTID=120 IP=192.0.2.20/24 GW=192.0.2.1 DNS=192.0.2.10 SSH_KEY=/root/.ssh/id_ed25519.pub sh create-lxc.sh
#
# Every setting has a default except IP and GW, which are specific to your network.
set -e

CTID="${CTID:-$(pvesh get /cluster/nextid)}"
HOSTNAME="${HOSTNAME_CT:-noc}"
IP="${IP:?set IP=<address>/<prefix>, e.g. IP=192.0.2.20/24}"
GW="${GW:?set GW=<gateway>, e.g. GW=192.0.2.1}"
DNS="${DNS:-$GW}"
BRIDGE="${BRIDGE:-vmbr0}"
STORAGE="${STORAGE:-local-lvm}"
DISK_GB="${DISK_GB:-16}"
CORES="${CORES:-2}"
MEMORY="${MEMORY:-2048}"
TEMPLATE_STORE="${TEMPLATE_STORE:-local}"
SSH_KEY="${SSH_KEY:-}"

command -v pct >/dev/null || { echo "run this on a Proxmox VE host" >&2; exit 1; }

# Newest Debian standard template the host knows about.
pveam update >/dev/null
TEMPLATE="$(pveam available --section system | awk '/debian-1[3-9]-standard.*amd64/ {print $2}' | sort -V | tail -1)"
[ -n "$TEMPLATE" ] || TEMPLATE="$(pveam available --section system | awk '/debian-12-standard.*amd64/ {print $2}' | sort -V | tail -1)"
[ -n "$TEMPLATE" ] || { echo "no Debian template found (pveam available)" >&2; exit 1; }
pveam list "$TEMPLATE_STORE" | grep -q "$TEMPLATE" || pveam download "$TEMPLATE_STORE" "$TEMPLATE"

set -- --hostname "$HOSTNAME" --description "Home NOC command center" \
  --cores "$CORES" --memory "$MEMORY" --swap 512 --rootfs "$STORAGE:$DISK_GB" \
  --net0 "name=eth0,bridge=$BRIDGE,ip=$IP,gw=$GW,firewall=0" --nameserver "$DNS" \
  --unprivileged 1 --features nesting=1 --onboot 1 --start 1
[ -n "$SSH_KEY" ] && set -- "$@" --ssh-public-keys "$SSH_KEY"

pct create "$CTID" "$TEMPLATE_STORE:vztmpl/$TEMPLATE" "$@"
sleep 5
pct status "$CTID"
pct exec "$CTID" -- ip -4 -br addr show eth0

echo
echo "Container $CTID is up. Next: copy this repo into it and run deploy/install.sh"
echo "  pct push $CTID home-noc.tar.gz /root/home-noc.tar.gz   (or git clone inside the container)"
