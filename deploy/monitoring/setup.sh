#!/bin/sh
# Prometheus + Grafana for the Home NOC's /metrics, in a fresh Debian 13 container or VM (run as root).
#
#   sh setup.sh http://<noc-address>:8080 < token-file
#
# The token is NOC_METRICS_TOKEN from the NOC's /etc/noc/secrets.env, read from standard input so it never
# appears on a command line. Grafana listens on port 3000; its admin password is written to
# /root/grafana-admin-password (only root can read it). Prometheus keeps 2 years of readings.
set -eu

NOC_URL="${1:?usage: sh setup.sh http://<noc-address>:8080 < token-file}"
HERE="$(cd "$(dirname "$0")" && pwd)"
case "$NOC_URL" in http://*|https://*) ;; *) echo "NOC URL must start with http:// or https://" >&2; exit 1 ;; esac
SCHEME="${NOC_URL%%://*}"
HOSTPORT="${NOC_URL#*://}"
HOSTPORT="${HOSTPORT%%/*}"

TOKEN="$(cat)"
[ -n "$TOKEN" ] || { echo "no token on standard input" >&2; exit 1; }

echo "==> Packages (Prometheus from Debian, Grafana from its own repository)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends prometheus curl gnupg ca-certificates
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor --yes -o /etc/apt/keyrings/grafana.gpg
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" > /etc/apt/sources.list.d/grafana.list
apt-get update -q
apt-get install -y -q grafana

echo "==> Prometheus: scrape the NOC every 30 s, keep 2 years"
umask 077
printf '%s' "$TOKEN" > /etc/prometheus/noc.token
chown prometheus:prometheus /etc/prometheus/noc.token
umask 022
cat > /etc/prometheus/prometheus.yml <<EOF
global:
  scrape_interval: 30s
  evaluation_interval: 30s
scrape_configs:
  - job_name: home-noc
    scheme: $SCHEME
    metrics_path: /metrics
    authorization:
      credentials_file: /etc/prometheus/noc.token
    static_configs:
      - targets: ["$HOSTPORT"]
  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]
EOF
promtool check config /etc/prometheus/prometheus.yml
echo 'ARGS="--storage.tsdb.retention.time=2y"' > /etc/default/prometheus

echo "==> Grafana: Prometheus data source and the Home NOC dashboard"
install -d /etc/grafana/provisioning/datasources /etc/grafana/provisioning/dashboards /var/lib/grafana/dashboards
cat > /etc/grafana/provisioning/datasources/home-noc.yml <<'EOF'
apiVersion: 1
datasources:
  - name: Prometheus (Home NOC)
    uid: noc-prom
    type: prometheus
    access: proxy
    url: http://localhost:9090
    isDefault: true
    jsonData:
      timeInterval: 30s
EOF
cat > /etc/grafana/provisioning/dashboards/home-noc.yml <<'EOF'
apiVersion: 1
providers:
  - name: home-noc
    folder: Home NOC
    type: file
    allowUiUpdates: true
    options:
      path: /var/lib/grafana/dashboards
EOF
install -m 0644 "$HERE/home-noc-dashboard.json" /var/lib/grafana/dashboards/home-noc-dashboard.json
chown -R grafana:grafana /var/lib/grafana/dashboards

if [ ! -s /root/grafana-admin-password ]; then
  ( umask 077; head -c 18 /dev/urandom | base64 | tr -d '/+=' > /root/grafana-admin-password )
fi

systemctl daemon-reload
systemctl enable --now prometheus grafana-server
systemctl restart prometheus
# Set the admin password once Grafana has created its database
for _ in $(seq 1 30); do curl -fsS http://localhost:3000/api/health >/dev/null 2>&1 && break; sleep 2; done
grafana cli --homepath /usr/share/grafana --config /etc/grafana/grafana.ini admin reset-admin-password "$(cat /root/grafana-admin-password)" >/dev/null
echo "==> Done: Grafana on port 3000 (user admin, password in /root/grafana-admin-password)"
