#!/bin/sh
# Install noc-hw (fans and RGB lighting from the Home NOC) on the Proxmox host. Run as root on the host:
#   sh install-noc-hw.sh "<the NOC's /etc/noc/id_ed25519.pub line>"
# Lighting (optional): download the OpenRGB x86_64 AppImage from https://openrgb.org/releases.html and
#   NOC_HW_OPENRGB=/root/OpenRGB_<version>_x86_64.AppImage sh install-noc-hw.sh ...
# (extracted to /opt/openrgb; the AppImage carries its own Qt, so nothing else is installed).
# Re-running is safe: it keeps /etc/noc-hw.json and replaces the program and services.
set -e
here=$(dirname "$0")
chip=${NOC_HW_CHIP:-nct6798}

# the Nuvoton Super I/O fan driver and /dev/i2c-* (RAM and GPU lighting), now and at every boot
modprobe nct6775
modprobe i2c-dev
printf 'nct6775\ni2c-dev\n' > /etc/modules-load.d/noc-hw.conf

if [ -n "$NOC_HW_OPENRGB" ]; then
	tmp=$(mktemp -d)
	(cd "$tmp" && chmod +x "$NOC_HW_OPENRGB" && "$NOC_HW_OPENRGB" --appimage-extract >/dev/null)
	rm -rf /opt/openrgb && mv "$tmp/squashfs-root" /opt/openrgb && rmdir "$tmp"
fi

install -m 755 "$here/noc-hw" /usr/local/sbin/noc-hw
install -m 644 "$here/noc-hw.service" /etc/systemd/system/noc-hw.service
install -m 644 "$here/noc-hw-lights.service" /etc/systemd/system/noc-hw-lights.service

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
systemctl enable noc-hw-lights.service
cat /etc/noc-hw.json
