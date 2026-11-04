#!/bin/sh
# Collect the OpenWrt router's log on the NOC (UDP 514, only the router is accepted), one file per source
# in /var/log/remote, rotated daily and kept for 30 days. Run as root inside the NOC container:
#   ROUTER_HOST=192.0.2.1 sh deploy/noc-logs.sh
# Then point the router at the NOC: uci set system.@system[0].log_ip=<NOC address>; uci set system.@system[0].log_port=514
#   uci commit system; /etc/init.d/log restart
set -e
: "${ROUTER_HOST:?set ROUTER_HOST=<router address>}"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq rsyslog >/dev/null
mkdir -p /var/log/remote
cat > /etc/rsyslog.d/10-remote.conf <<EOF
# Logs sent by the OpenWrt router over UDP 514 (Home NOC). Only the router is accepted.
# A relay on the router may tag a device's log g7...: those go to their own file.
module(load="imudp")
input(type="imudp" port="514" ruleset="remote")
ruleset(name="remote") {
  if \$fromhost-ip != "$ROUTER_HOST" then stop
  if \$programname startswith "g7" then {
    action(type="omfile" file="/var/log/remote/g7.log")
    stop
  }
  action(type="omfile" file="/var/log/remote/router.log")
  stop
}
EOF
cat > /etc/logrotate.d/remote <<'EOF'
/var/log/remote/*.log {
	daily
	rotate 30
	compress
	delaycompress
	missingok
	notifempty
	postrotate
		/usr/lib/rsyslog/rsyslog-rotate
	endscript
}
EOF
rsyslogd -N1 >/dev/null
systemctl restart rsyslog
echo "Log collector ready: /var/log/remote (from $ROUTER_HOST)"
