#!/bin/sh
# Create a READ-ONLY Proxmox API token for the NOC. Run as root on the Proxmox host.
# Prints the token line to paste into /etc/noc/secrets.env as PVE_TOKEN=...
set -e
USER="${PVE_USER:-noc@pve}"
TOKEN="${PVE_TOKEN_ID:-overview}"

pveum user list --output-format json | grep -q "\"$USER\"" || pveum user add "$USER" --comment "Home NOC (read-only)"
# PVEAuditor = view everything, change nothing.
pveum acl modify / --users "$USER" --roles PVEAuditor
pveum user token list "$USER" --output-format json | grep -q "\"$TOKEN\"" && pveum user token remove "$USER" "$TOKEN"
secret="$(pveum user token add "$USER" "$TOKEN" --privsep 0 --comment "Home NOC read-only" --output-format json \
  | sed -n 's/.*"value" *: *"\([^"]*\)".*/\1/p')"
[ -n "$secret" ] || { echo "token creation failed" >&2; exit 1; }
echo
echo "Add this line to /etc/noc/secrets.env on the NOC (shown once):"
echo "PVE_TOKEN=$USER!$TOKEN=$secret"
