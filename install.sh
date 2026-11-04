#!/bin/sh
# Home NOC installer. Run as root on a Proxmox VE host:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/JaGPR/home-noc/main/install.sh)"
#
# With options:  bash -c "$(curl -fsSL .../install.sh)" install --dry-run
#
# It creates a Debian container, installs the NOC from a checksummed GitHub release, writes a first config, makes
# a read-only Proxmox token, and (if you say yes) sets up fan control on this host and the log collector. Everything
# else (Pi-holes, Omada, Telegram ...) is in the NOC's Settings tab afterwards. It never touches an existing container.
#
#   --dry-run          show what it would do, change nothing
#   --config FILE      answers from a file of KEY=value lines (see ANSWERS below), for repeat installs
#   --yes              take the defaults instead of asking (IP is still required)
#   --version vX.Y.Z   a specific release (default: the latest)
#   --source PATH      install from a folder or .tar.gz instead of GitHub (for development)
#   --upgrade CTID     upgrade the NOC in that container; its config, secrets and logs are kept
#
# ANSWERS: CTID CT_HOSTNAME IP GW DNS BRIDGE STORAGE DISK_GB CORES MEMORY SSH_KEY SITE_NAME ROUTER_HOST SPEED_URL
#          WITH_TOKEN WITH_NOC_HW WITH_LOGS (y/n)
set -eu

REPO="JaGPR/home-noc"
VERSION=latest
SOURCE=""
DRY=""
YES=""
UPGRADE=""
CONFIG=""

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
# a command that changes something: shown, not run, with --dry-run
run() { if [ -n "$DRY" ]; then printf '    would run: %s\n' "$*"; else "$@"; fi; }

while [ $# -gt 0 ]; do
	case "$1" in
	--dry-run) DRY=1 ;;
	--yes) YES=1 ;;
	--config) CONFIG="${2:?--config needs a file}"; shift ;;
	--version) VERSION="${2:?--version needs a tag}"; shift ;;
	--source) SOURCE="${2:?--source needs a path}"; shift ;;
	--upgrade) UPGRADE="${2:?--upgrade needs a container ID}"; shift ;;
	-h | --help) printf 'See the top of install.sh: https://github.com/%s/blob/main/install.sh\n' "$REPO"; exit 0 ;;
	*) die "unknown option $1 (see --help)" ;;
	esac
	shift
done

if [ -n "$CONFIG" ]; then
	[ -r "$CONFIG" ] || die "cannot read $CONFIG"
	# data, not code: only the known names, values taken as they are (optional surrounding quotes dropped)
	cr="$(printf '\r')"
	while IFS= read -r line || [ -n "$line" ]; do
		line="${line%"$cr"}"   # a file edited on Windows
		case "$line" in '' | '#'*) continue ;; esac
		key="${line%%=*}"
		val="${line#*=}"
		case " CTID CT_HOSTNAME IP GW DNS BRIDGE STORAGE DISK_GB CORES MEMORY SSH_KEY SITE_NAME ROUTER_HOST SPEED_URL WITH_TOKEN WITH_NOC_HW WITH_LOGS " in
		*" $key "*) ;;
		*) die "unknown setting in $CONFIG: $key" ;;
		esac
		case "$val" in \"*\" | \'*\') val="${val#?}"; val="${val%?}" ;; esac
		eval "$key=\$val"
	done < "$CONFIG"
fi

# ask VAR "question" [default]: keeps a value from --config or the environment; with --yes takes the default
ask() {
	eval "cur=\${$1:-}"
	if [ -z "$cur" ]; then
		cur="${3:-}"
		if [ -z "$YES" ]; then
			printf '%s%s: ' "$2" "${3:+ [$3]}" > /dev/tty
			read -r ans < /dev/tty || ans=""
			[ -n "$ans" ] && cur="$ans"
		fi
	fi
	eval "$1=\$cur"
}

yes_no() { case "$1" in [Yy]*) return 0 ;; *) return 1 ;; esac; }

preflight() {
	[ -n "$DRY" ] || [ "$(id -u)" = 0 ] || die "run as root"
	for c in pct pvesh pveam; do
		command -v "$c" >/dev/null || die "$c not found: run this on a Proxmox VE host"
	done
}

# Fetch the code into $WORK/home-noc.tar.gz (top folder home-noc/): GitHub with its checksum, or --source.
fetch() {
	WORK="$(mktemp -d)"
	trap 'rm -rf "$WORK"' EXIT
	PKG="$WORK/home-noc.tar.gz"
	if [ -d "$SOURCE" ]; then
		say "Packing $SOURCE"
		tar czf "$PKG" -C "$SOURCE" --exclude=.git --exclude=__pycache__ --exclude=tests/fixtures.json \
			--exclude=tests/ui/sanitize.local.json --transform 's,^\.,home-noc,' .
	elif [ -n "$SOURCE" ]; then
		cp "$SOURCE" "$PKG"
	else
		# one tag for both files: "latest" could otherwise change between them, and GitHub's latest/download
		# links answer 404 now and then for a while after a release
		get="curl -fsSL --retry 4 --retry-delay 2 --retry-all-errors"
		if [ "$VERSION" = latest ]; then
			tag="$($get -I -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest")" || die "cannot reach GitHub"
			VERSION="${tag##*/}"
			case "$VERSION" in v[0-9]*) ;; *) die "no release found on GitHub ($tag)" ;; esac
		fi
		url="https://github.com/$REPO/releases/download/$VERSION"
		say "Downloading $url/home-noc.tar.gz"
		$get -o "$PKG" "$url/home-noc.tar.gz" || die "download failed (is there a release $VERSION?)"
		$get -o "$PKG.sha256" "$url/home-noc.tar.gz.sha256" || die "checksum download failed"
		(cd "$WORK" && sha256sum -c home-noc.tar.gz.sha256 >/dev/null) || die "checksum mismatch: not installing this download"
		say "Checksum OK"
	fi
	mkdir "$WORK/src"
	tar xzf "$PKG" -C "$WORK/src" --strip-components=1
}

push_code() {
	run pct push "$1" "$PKG" /root/home-noc.tar.gz
	run pct exec "$1" -- sh -c 'rm -rf /root/home-noc && mkdir /root/home-noc && tar xzf /root/home-noc.tar.gz -C /root/home-noc --strip-components=1'
}

upgrade() {
	pct status "$UPGRADE" >/dev/null 2>&1 || die "no container $UPGRADE here"
	say "Upgrading the NOC in container $UPGRADE (config, secrets and logs are kept)"
	if [ -n "$DRY" ]; then
		PKG="<the release>"
	else
		fetch
	fi
	push_code "$UPGRADE"
	run pct exec "$UPGRADE" -- sh -c 'cd /root/home-noc && sh deploy/install.sh'
	if [ -x /usr/local/sbin/noc-hw ]; then
		say "Updating fan control on this host"
		run install -m 755 "${WORK:-.}/src/deploy/proxmox/noc-hw" /usr/local/sbin/noc-hw
		run systemctl restart noc-hw
	fi
	say "Done"
}

host_ip() { hostname -I 2>/dev/null | awk '{print $1}'; }

fresh() {
	ask CTID "Container ID" "$(pvesh get /cluster/nextid 2>/dev/null || echo 120)"
	ask CT_HOSTNAME "Container hostname" "noc"
	ask IP "Container address, as address/prefix (e.g. 192.0.2.20/24)"
	gw="$(ip route show default 2>/dev/null | awk '{print $3; exit}')"
	ask GW "Gateway" "$gw"
	ask DNS "DNS server" "$GW"
	ask BRIDGE "Network bridge" "vmbr0"
	ask STORAGE "Container storage" "local-lvm"
	ask SITE_NAME "Site name" "Home NOC"
	ask ROUTER_HOST "OpenWrt router address (blank: none)" "$GW"
	ask SPEED_URL "Speed test file, a URL of 100 MB or more (blank: none)" ""
	ask WITH_TOKEN "Create a read-only Proxmox API token for the NOC? (y/n)" "y"
	ask WITH_NOC_HW "Set up fan and lighting control on this host? (y/n)" "n"
	ask WITH_LOGS "Collect the router's logs on the NOC? (y/n)" "n"

	case "$CTID" in '' | *[!0-9]*) die "container ID must be a number" ;; esac
	printf '%s' "$IP" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$' || die "address must look like 192.0.2.20/24, not '$IP'"
	[ -n "$GW" ] || die "no gateway"
	pct status "$CTID" >/dev/null 2>&1 && die "container $CTID already exists: pick another ID (to upgrade it: --upgrade $CTID)"
	ping -c1 -W1 "${IP%/*}" >/dev/null 2>&1 && die "${IP%/*} already answers on the network: pick a free address"

	node="$(hostname)"
	me="$(host_ip)"
	say "Plan: container $CTID ($CT_HOSTNAME) at $IP on $BRIDGE, storage $STORAGE, node $node"
	if [ -n "$DRY" ]; then
		PKG="<the release>"
		WORK="<work>"
	else
		fetch
	fi

	say "Creating the container"
	run env CTID="$CTID" HOSTNAME_CT="$CT_HOSTNAME" IP="$IP" GW="$GW" DNS="$DNS" BRIDGE="$BRIDGE" STORAGE="$STORAGE" \
		DISK_GB="${DISK_GB:-16}" CORES="${CORES:-2}" MEMORY="${MEMORY:-2048}" SSH_KEY="${SSH_KEY:-}" \
		sh "$WORK/src/deploy/create-lxc.sh"

	say "Waiting for its network"
	if [ -z "$DRY" ]; then
		i=0
		until pct exec "$CTID" -- sh -c 'getent hosts deb.debian.org >/dev/null' 2>/dev/null; do
			i=$((i + 1))
			[ "$i" -lt 30 ] || die "the container has no network or DNS after 60 s (container $CTID is left for you to inspect)"
			sleep 2
		done
	fi

	say "Installing the NOC"
	push_code "$CTID"
	run pct exec "$CTID" -- sh -c 'cd /root/home-noc && NOC_NO_START=1 sh deploy/install.sh'

	say "Writing the first config"
	run pct exec "$CTID" -- env SITE_NAME="$SITE_NAME" ROUTER_HOST="$ROUTER_HOST" PVE_HOST="$me" PVE_NODE="$node" SPEED_URL="$SPEED_URL" \
		HW_HOST="$(yes_no "$WITH_NOC_HW" && echo "$me" || true)" python3 /root/home-noc/deploy/first-config.py

	if yes_no "$WITH_TOKEN"; then
		say "Creating a read-only Proxmox token (noc@pve!noc-$CTID)"
		if [ -z "$DRY" ]; then
			tok="$(PVE_TOKEN_ID="noc-$CTID" sh "$WORK/src/deploy/proxmox-token.sh" | sed -n 's/^PVE_TOKEN=//p')"
			[ -n "$tok" ] || die "token creation failed"
			printf '%s\n' "$tok" | pct exec "$CTID" -- sh -c 'read -r t; sed -i "s|^PVE_TOKEN=.*|PVE_TOKEN=$t|" /etc/noc/secrets.env'
		else
			printf '    would run: deploy/proxmox-token.sh, and store the token in the container\n'
		fi
	fi

	if yes_no "$WITH_NOC_HW"; then
		say "Setting up fan and lighting control on this host"
		if [ -z "$DRY" ]; then
			pub="$(pct exec "$CTID" -- cat /etc/noc/id_ed25519.pub)"
			if sh "$WORK/src/deploy/proxmox/install-noc-hw.sh" "$pub"; then
				pct exec "$CTID" -- sh -c "ssh-keyscan -T 5 -t ed25519 $me >> /etc/noc/known_hosts 2>/dev/null"
			else
				say "Fan control could not be set up on this machine (no supported fan chip); the NOC works without it"
			fi
		else
			printf '    would run: deploy/proxmox/install-noc-hw.sh with the NOC key\n'
		fi
	fi

	if yes_no "$WITH_LOGS"; then
		say "Setting up the log collector"
		run pct exec "$CTID" -- env ROUTER_HOST="$ROUTER_HOST" sh /root/home-noc/deploy/noc-logs.sh
	fi

	say "Starting the NOC"
	run pct exec "$CTID" -- systemctl start noc
	[ -n "$DRY" ] && { say "Dry run: nothing was changed"; return 0; }
	i=0
	until pct exec "$CTID" -- curl -fs -m 3 http://127.0.0.1:8080/healthz >/dev/null 2>&1; do
		i=$((i + 1))
		[ "$i" -lt 20 ] || die "the NOC did not answer; see: pct exec $CTID -- journalctl -u noc -n 30"
		sleep 2
	done
	pass="$(pct exec "$CTID" -- sed -n 's/^NOC_ADMIN_PASS=//p' /etc/noc/secrets.env)"
	pub="$(pct exec "$CTID" -- cat /etc/noc/id_ed25519.pub)"
	printf '\n'
	say "The Home NOC is running: http://${IP%/*}:8080"
	printf '    Login: admin / %s  (change it in Settings)\n\n' "$pass"
	if [ -n "$ROUTER_HOST" ]; then
		printf 'Let it read your OpenWrt router: run this on the router:\n'
		printf "    echo '%s' >> /etc/dropbear/authorized_keys\n" "$pub"
		printf "Then trust the router from here:\n    pct exec %s -- sh -c 'ssh-keyscan -T 5 %s >> /etc/noc/known_hosts'\n\n" "$CTID" "$ROUTER_HOST"
	fi
	if yes_no "$WITH_LOGS" && [ -n "$ROUTER_HOST" ]; then
		printf 'Send the router log here: on the router:\n'
		printf "    uci set system.@system[0].log_ip=%s; uci set system.@system[0].log_port=514; uci commit system; /etc/init.d/log restart\n\n" "${IP%/*}"
	fi
	printf 'Everything else (Pi-holes, Omada, Telegram, WAN groups) is in the NOC: Settings tab.\n'
}

preflight
if [ -n "$UPGRADE" ]; then upgrade; else fresh; fi
