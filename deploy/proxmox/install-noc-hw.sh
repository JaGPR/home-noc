#!/bin/sh
# Install noc-hw (fan control from the Home NOC) on the Proxmox host. Run as root on the host:
#   sh install-noc-hw.sh "<the NOC's /etc/noc/id_ed25519.pub line>"
# Re-running is safe: it keeps /etc/noc-hw.json and replaces the program and service.
set -e
here=$(dirname "$0")
chip=${NOC_HW_CHIP:-nct6798}

# the Nuvoton Super I/O fan driver, now and at every boot
modprobe nct6775
echo nct6775 > /etc/modules-load.d/noc-hw.conf

install -m 755 "$here/noc-hw" /usr/local/sbin/noc-hw
install -m 644 "$here/noc-hw.service" /etc/systemd/system/noc-hw.service

# first install: control the headers that have a fan spinning now (an empty header is left alone)
if [ ! -f /etc/noc-hw.json ]; then
	d=$(grep -l "^$chip$" /sys/class/hwmon/hwmon*/name | head -1 | xargs dirname)
	[ -n "$d" ] || { echo "no $chip hwmon found" >&2; exit 1; }
	chans=""
	for f in "$d"/pwm[0-9]; do
		n=${f##*pwm}
		rpm=$(cat "$d/fan${n}_input" 2>/dev/null || echo 0)
		[ "$rpm" -gt 0 ] || continue
		min=25; [ "$n" = 1 ] && min=30   # header 1 is CPU_FAN on ASUS boards
		name="Fan $n"; [ "$n" = 1 ] && name="CPU fan"
		chans="$chans${chans:+, }\"$n\": {\"name\": \"$name\", \"min_pct\": $min}"
	done
	printf '{"board_chip": "%s", "channels": {%s}, "hot_c": 85, "gpu_min_pct": 20}\n' "$chip" "$chans" > /etc/noc-hw.json
fi

# the NOC's key may run noc-hw and nothing else
if [ -n "$1" ]; then
	mkdir -p /root/.ssh
	key=$(echo "$1" | awk '{print $1, $2}')
	grep -qF "$key" /root/.ssh/authorized_keys 2>/dev/null ||
		echo "restrict,command=\"/usr/local/sbin/noc-hw ssh\" $1" >> /root/.ssh/authorized_keys
fi

systemctl daemon-reload
systemctl enable --now noc-hw.service
cat /etc/noc-hw.json
