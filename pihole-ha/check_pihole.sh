#!/bin/sh
# keepalived health check: healthy only if pihole-FTL is running AND answering DNS.
# Install on BOTH Pi-holes:  /etc/keepalived/check_pihole.sh  (root:root, 755)
# Needs `dig` (Debian/Ubuntu: apt install dnsutils / bind9-dnsutils).
systemctl is-active --quiet pihole-FTL || exit 1
dig +time=1 +tries=1 +short @127.0.0.1 pi.hole A >/dev/null 2>&1 || exit 1
exit 0
