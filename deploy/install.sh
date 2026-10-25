#!/bin/sh
# Install or upgrade the NOC inside a Debian 12/13 container or VM. Run as root
# from the repository root:   sh deploy/install.sh
#
# Upgrades keep /etc/noc (config, secrets, SSH key) and /var/lib/noc (audit log).
set -e

[ "$(id -u)" = "0" ] || { echo "run as root" >&2; exit 1; }
[ -f app/app.py ] || { echo "run from the repository root (app/app.py not found)" >&2; exit 1; }

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-fastapi python3-uvicorn python3-requests python3-multipart \
  openssh-client iputils-ping curl ca-certificates >/dev/null

echo "==> Installing the app to /opt/noc"
mkdir -p /opt/noc/static /etc/noc /var/lib/noc
chmod 700 /etc/noc
cp app/*.py /opt/noc/
cp app/static/*.html /opt/noc/static/
for f in /opt/noc/*.py; do python3 -m py_compile "$f"; done
printf '#!/bin/sh\nexec python3 /opt/noc/passwd.py "$@"\n' > /usr/local/sbin/noc-passwd
chmod 755 /usr/local/sbin/noc-passwd

if [ ! -f /etc/noc/config.json ]; then
	install -m 600 config/config.example.json /etc/noc/config.json
	NEED_CONFIG=1
fi

if [ ! -f /etc/noc/secrets.env ]; then
	pass="$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')"
	key="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
	sed -e "s/^NOC_ADMIN_PASS=.*/NOC_ADMIN_PASS=$pass/" -e "s/^NOC_SESSION_KEY=.*/NOC_SESSION_KEY=$key/" \
	  config/secrets.env.example > /etc/noc/secrets.env
	chmod 600 /etc/noc/secrets.env
	echo "    Generated NOC login: user 'admin', password stored in /etc/noc/secrets.env (NOC_ADMIN_PASS)"
fi

if [ ! -f /etc/noc/id_ed25519 ]; then
	ssh-keygen -q -t ed25519 -N "" -C "noc@$(hostname)" -f /etc/noc/id_ed25519
fi

echo "==> Installing the systemd service"
cp deploy/noc.service /etc/systemd/system/noc.service
systemctl daemon-reload
systemctl enable noc >/dev/null 2>&1

echo "==> Installing the hourly PPPoE speed watch (needs speedwatch.url in config.json)"
install -m 644 deploy/noc-speedwatch.cron /etc/cron.d/noc-speedwatch

if [ -n "$NEED_CONFIG" ]; then
	echo
	echo "Installed. Before starting, edit /etc/noc/config.json and /etc/noc/secrets.env,"
	echo "then run:  systemctl start noc"
else
	systemctl restart noc
	sleep 2
	systemctl is-active noc && echo "Running on http://$(hostname -I | awk '{print $1}'):8080"
fi

echo
echo "The NOC's SSH public key (add it to your OpenWrt router and access point):"
cat /etc/noc/id_ed25519.pub
