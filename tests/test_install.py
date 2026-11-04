"""Data-driven tests of the single-script installer (install.sh), against stub Proxmox commands that log every call,
plus deploy/first-config.py.  Run:  python -m unittest tests/test_install.py

The integration test runs it for real on a Proxmox host, creating a throw-away container and deleting it afterwards:
set PVE_HOST (SSH as root with a key), TEST_IP (a free address/prefix) and TEST_CTID (a free ID), optionally TEST_GW.
It installs from this working copy (--source) and touches nothing else except one read-only API token, also removed.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
_spec = importlib.util.spec_from_file_location("first_config", ROOT / "deploy" / "first-config.py")
FC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(FC)

STUB = r"""#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB_LOG"
"""
PCT = STUB + r"""
case "$1" in
status) case " $EXISTING $(cat "$STUB_LOG.created" 2>/dev/null | tr '\n' ' ') " in *" $2 "*) echo "status: running"; exit 0 ;; esac; exit 2 ;;
create) echo "$2" >> "$STUB_LOG.created" ;;
exec)
	case "$*" in
	*id_ed25519.pub*) echo "ssh-ed25519 AAAATEST noc@test" ;;
	*NOC_ADMIN_PASS*) echo "generated-pass" ;;
	*healthz*) [ -z "$NOC_DOWN" ] || exit 7 ;;
	*PVE_TOKEN=*) cat >> "$STUB_LOG.token" ;;
	esac ;;
esac
exit 0
"""
STUBS = {
    "pct": PCT,
    "pvesh": STUB + 'echo 321\n',
    "pveam": STUB + 'case "$1" in available) echo "system debian-13-standard_13.6-1_amd64.tar.zst" ;; list) echo "local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst" ;; esac\n',
    "pveum": STUB + r"""case "$*" in
*"user list"*) echo '[{"userid":"noc@pve"}]' ;;
*"token list"*) echo '[]' ;;
*"token add"*) echo '{"value":"secret-value"}' ;;
esac
""",
    "ping": STUB + '[ -n "$PING_OK" ]\n',
    "hostname": 'if [ "$1" = -I ]; then echo "192.0.2.6 "; else echo pve-test; fi\n',
    "ip": 'echo "default via 192.0.2.1 dev vmbr0"\n',
    "id": 'echo 0\n',
    "sleep": 'exit 0\n',
    "curl": STUB + r"""
out=""
while [ $# -gt 0 ]; do [ "$1" = -o ] && { out="$2"; shift; }; shift; done
[ -n "$CURL_FAIL" ] && exit 22
case "$out" in
*.sha256) printf '%s  home-noc.tar.gz\n' "$CURL_SUM" > "$out" ;;
*) cp "$CURL_PKG" "$out" ;;
esac
""",
}
ANSWERS = "IP=192.0.2.20/24\nGW=192.0.2.1\nCTID=150\n"


def package(tmp):
    """A release-shaped archive holding the deploy scripts the installer runs on the host."""
    p = Path(tmp) / "home-noc.tar.gz"
    with tarfile.open(p, "w:gz") as t:
        for f in ("deploy/create-lxc.sh", "deploy/proxmox-token.sh", "deploy/first-config.py", "deploy/install.sh"):
            t.add(ROOT / f, arcname=f"home-noc/{f}")
    return p


class Run:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        self.log = self.dir / "log"
        self.log.write_text("")
        (self.dir / "bin").mkdir()
        for name, body in STUBS.items():
            f = self.dir / "bin" / name
            f.write_bytes((body if body.startswith("#!") else "#!/bin/sh\n" + body).encode())
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.pkg = package(self.dir)
        self.env = {**os.environ, "PATH": str(self.dir / "bin") + os.pathsep + os.environ["PATH"], "STUB_LOG": self.log.as_posix(),
                    "EXISTING": "", "NOC_DOWN": "", "PING_OK": "", "CURL_FAIL": "", "CURL_PKG": self.pkg.as_posix(),
                    "CURL_SUM": hashlib.sha256(self.pkg.read_bytes()).hexdigest()}
        for k in ("IP", "GW", "DNS", "CTID"):
            self.env.pop(k, None)

    def __call__(self, *args, answers=ANSWERS, **env):
        cfg = self.dir / "answers"
        cfg.write_bytes(answers.encode())   # exactly these bytes, on Windows too
        r = subprocess.run(["sh", INSTALL.as_posix(), "--config", cfg.as_posix(), "--yes", *args], capture_output=True, text=True,
                           env={**self.env, **env}, stdin=subprocess.DEVNULL, timeout=60)
        self.out = r.stdout + r.stderr
        self.code = r.returncode
        return self

    def calls(self, prog="pct"):
        return [ln for ln in self.log.read_text().splitlines() if ln.startswith(prog + " ")]

    def changes(self):
        """Commands that would change something on the host (reads like status and nextid don't count)."""
        return [c for c in self.log.read_text().splitlines()
                if c.split()[0] in ("pct", "pveum", "pveam") and c.split()[1] not in ("status", "available", "list", "update")]

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


@ddt
class Install(unittest.TestCase):
    def setUp(self):
        self.r = Run()
        self.addCleanup(self.r.close)

    def test_full_install_order(self):
        r = self.r()
        self.assertEqual(r.code, 0, r.out)
        calls = r.calls()
        idx = lambda text: next(i for i, c in enumerate(calls) if text in c)
        self.assertLess(idx("create 150"), idx("push 150"))
        self.assertLess(idx("push 150"), idx("deploy/install.sh"))
        self.assertLess(idx("deploy/install.sh"), idx("first-config.py"))
        self.assertLess(idx("first-config.py"), idx("systemctl start noc"), "the config exists before the NOC starts")
        self.assertIn("NOC_NO_START=1", calls[idx("deploy/install.sh")])
        self.assertIn("http://192.0.2.20:8080", r.out)
        self.assertIn("admin / generated-pass", r.out)
        self.assertIn("echo 'ssh-ed25519 AAAATEST noc@test' >> /etc/dropbear/authorized_keys", r.out)

    def test_answers_reach_the_container(self):
        r = self.r(answers=ANSWERS + "CT_HOSTNAME=mynoc\nBRIDGE=vmbr1\nSTORAGE=fast\nDNS=192.0.2.53\nCORES=4\nMEMORY=4096\nDISK_GB=32\n")
        create = next(c for c in r.calls() if c.startswith("pct create"))
        for want in ("--hostname mynoc", "bridge=vmbr1", "ip=192.0.2.20/24", "gw=192.0.2.1", "--rootfs fast:32", "--nameserver 192.0.2.53", "--cores 4", "--memory 4096", "--unprivileged 1"):
            self.assertIn(want, create)

    def test_defaults(self):
        r = self.r(answers="IP=192.0.2.20/24\n")
        self.assertEqual(r.code, 0, r.out)
        create = next(c for c in r.calls() if c.startswith("pct create"))
        self.assertTrue(create.startswith("pct create 321 "), "next free ID from Proxmox")
        self.assertIn("gw=192.0.2.1", create, "gateway from this host's default route")
        self.assertIn("--hostname noc", create)

    def test_token_goes_in_by_stdin_not_arguments(self):
        r = self.r()
        self.assertEqual(Path(str(r.log) + ".token").read_text().strip(), "noc@pve!noc-150=secret-value")
        self.assertNotIn("secret-value", r.log.read_text(), "a secret on a command line is visible to everyone on the host")
        self.assertNotIn("secret-value", r.out)
        self.assertFalse([c for c in r.calls("pveum") if "token remove" in c], "an existing token is never revoked")
        self.assertTrue([c for c in r.calls("pveum") if "token add noc@pve noc-150" in c], "each container gets its own token")

    @data(("WITH_TOKEN=n", False), ("WITH_TOKEN=y", True))
    @unpack
    def test_token_optional(self, answer, made):
        r = self.r(answers=ANSWERS + answer + "\n")
        self.assertEqual(bool(r.calls("pveum")), made)

    def test_logs_optional(self):
        self.assertFalse([c for c in self.r().calls() if "noc-logs.sh" in c])
        r = self.r(answers=ANSWERS.replace("150", "151") + "WITH_LOGS=y\n")
        self.assertTrue([c for c in r.calls() if "ROUTER_HOST=192.0.2.1" in c and "noc-logs.sh" in c])
        self.assertIn("log_ip=192.0.2.20", r.out)

    @data(
        # (answers, env, message): refused before anything changes
        ("GW=192.0.2.1\n", {}, "address must look like"),
        ("IP=192.0.2.20\nGW=192.0.2.1\n", {}, "address must look like"),
        ("IP=not-an-ip/24\n", {}, "address must look like"),
        ("IP=192.0.2.20/24\nCTID=abc\n", {}, "must be a number"),
        (ANSWERS, {"EXISTING": "150"}, "container 150 already exists"),
        (ANSWERS, {"PING_OK": "1"}, "192.0.2.20 already answers"),
    )
    @unpack
    def test_refuses(self, answers, env, msg):
        r = self.r(answers=answers, **env)
        self.assertNotEqual(r.code, 0)
        self.assertIn(msg, r.out)
        self.assertEqual(r.changes(), [], "nothing may change on a refusal")

    @data(('SITE_NAME=Two words', 'Two words'), ('SITE_NAME="Two words"', 'Two words'), ("SITE_NAME='Two words'", 'Two words'),
          ('SITE_NAME=a;touch hacked', 'a;touch hacked'), ('SITE_NAME=$(touch hacked)', '$(touch hacked)'), ('SITE_NAME=Name\r', 'Name'))
    @unpack
    def test_answers_are_data_not_code(self, line, want):
        r = self.r(answers=ANSWERS + "# a comment\n\n" + line + "\n")
        self.assertEqual(r.code, 0, r.out)
        cfg = next(c for c in r.calls() if "first-config.py" in c)
        self.assertIn(f"SITE_NAME={want} ROUTER_HOST", cfg)
        self.assertFalse((Path.cwd() / "hacked").exists())

    def test_unknown_setting_refused(self):
        r = self.r(answers=ANSWERS + "PATH=/evil\n")
        self.assertIn("unknown setting", r.out)
        self.assertEqual(r.changes(), [])

    def test_dry_run_changes_nothing(self):
        r = self.r("--dry-run", answers=ANSWERS + "WITH_NOC_HW=y\nWITH_LOGS=y\n")
        self.assertEqual(r.code, 0, r.out)
        self.assertEqual(r.changes(), [])
        for want in ("would run: env CTID=150", "would run: pct push 150", "would run: deploy/proxmox/install-noc-hw.sh", "noc-logs.sh", "nothing was changed"):
            self.assertIn(want, r.out)
        self.assertFalse(r.calls("curl"), "a dry run downloads nothing")

    def test_download_with_good_checksum(self):
        r = self.r()
        self.assertEqual(r.code, 0, r.out)
        self.assertIn("Checksum OK", r.out)
        self.assertIn("releases/latest/download/home-noc.tar.gz", r.calls("curl")[0])

    def test_version_pins_the_release(self):
        self.assertIn("releases/download/v9.9.9/home-noc.tar.gz", self.r("--version", "v9.9.9").calls("curl")[0])

    def test_bad_checksum_installs_nothing(self):
        r = self.r(CURL_SUM="0" * 64)
        self.assertNotEqual(r.code, 0)
        self.assertIn("checksum mismatch", r.out)
        self.assertEqual(r.changes(), [])

    def test_download_failure(self):
        r = self.r(CURL_FAIL="1")
        self.assertNotEqual(r.code, 0)
        self.assertIn("download failed", r.out)
        self.assertEqual(r.changes(), [])

    def test_source_folder_skips_download(self):
        r = self.r("--source", ROOT.as_posix())
        self.assertEqual(r.code, 0, r.out)
        self.assertFalse(r.calls("curl")[:0] + [c for c in r.calls("curl") if "github.com" in c])

    def test_noc_that_does_not_start_is_reported(self):
        r = self.r(NOC_DOWN="1")
        self.assertNotEqual(r.code, 0)
        self.assertIn("did not answer", r.out)
        self.assertNotIn("is running", r.out)

    def test_upgrade(self):
        r = self.r("--upgrade", "150", answers="", EXISTING="150")
        self.assertEqual(r.code, 0, r.out)
        self.assertFalse([c for c in r.calls() if c.startswith("pct create")], "no new container")
        self.assertTrue([c for c in r.calls() if "deploy/install.sh" in c and "NOC_NO_START" not in c], "restarts the NOC itself")
        self.assertFalse([c for c in r.calls() if "first-config" in c], "the config is kept")
        self.assertFalse(r.calls("pveum"), "the token is kept")

    def test_upgrade_needs_the_container(self):
        r = self.r("--upgrade", "150", answers="")
        self.assertNotEqual(r.code, 0)
        self.assertIn("no container 150", r.out)
        self.assertEqual(r.changes(), [])

    def test_unknown_option(self):
        r = self.r("--wipe-everything")
        self.assertNotEqual(r.code, 0)
        self.assertEqual(r.changes(), [])


@ddt
class FirstConfig(unittest.TestCase):
    ENV = {"SITE_NAME": "Lab", "ROUTER_HOST": "192.0.2.1", "PVE_HOST": "192.0.2.6", "PVE_NODE": "pve", "HW_HOST": "192.0.2.6", "SPEED_URL": "http://192.0.2.9/f"}

    def test_full(self):
        c = FC.build(self.ENV)
        self.assertEqual((c["site_name"], c["router"], c["proxmox"], c["hardware"], c["speedwatch"]["url"]),
                         ("Lab", {"host": "192.0.2.1"}, {"url": "https://192.0.2.6:8006", "node": "pve"}, {"host": "192.0.2.6"}, "http://192.0.2.9/f"))
        self.assertEqual([d["name"] for d in c["devices"]], ["Router", "Proxmox"])

    @data("HW_HOST", "SPEED_URL")
    def test_optional_blocks_left_out(self, key):
        section = {"HW_HOST": "hardware", "SPEED_URL": "speedwatch"}[key]
        self.assertNotIn(section, FC.build({**self.ENV, key: ""}))

    def test_nothing_known(self):
        c = FC.build({})
        self.assertEqual((c["site_name"], c["devices"], c["router"], c["proxmox"]), ("Home NOC", [], {}, {}))

    def test_keeps_an_edited_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text('{"site_name": "mine"}')
            FC.main({**self.ENV, "NOC_ETC": d})
            self.assertEqual(p.read_text(), '{"site_name": "mine"}')

    def test_replaces_the_untouched_example(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            p.write_text((ROOT / "config" / "config.example.json").read_text())
            FC.main({**self.ENV, "NOC_ETC": d})
            self.assertEqual(json.loads(p.read_text())["site_name"], "Lab")


@unittest.skipUnless(os.environ.get("PVE_HOST") and os.environ.get("TEST_IP") and os.environ.get("TEST_CTID"), "set PVE_HOST, TEST_IP, TEST_CTID")
class Live(unittest.TestCase):
    """For real: a throw-away container on the Proxmox host, checked over HTTP, then removed with its token."""

    def ssh(self, cmd, timeout=600, stdin=None):
        return subprocess.run(["ssh", "-o", "BatchMode=yes", f"root@{os.environ['PVE_HOST']}", cmd], capture_output=True, text=True,
                              timeout=timeout, input=stdin)

    def test_fresh_install_and_upgrade(self):
        ct, ip = os.environ["TEST_CTID"], os.environ["TEST_IP"]
        self.assertNotEqual(self.ssh(f"pct status {ct}").returncode, 0, f"container {ct} exists: refusing to touch it")
        src = f"/root/noc-install-test-{ct}"
        tar = subprocess.Popen(["tar", "cf", "-", "--exclude=.git", "--exclude=__pycache__", "--exclude=tests/fixtures.json", "--exclude=tests/ui/sanitize.local.json", "."],
                               cwd=ROOT, stdout=subprocess.PIPE)
        subprocess.run(["ssh", "-o", "BatchMode=yes", f"root@{os.environ['PVE_HOST']}", f"rm -rf {src}; mkdir {src}; tar xf - --no-same-owner -C {src}"], stdin=tar.stdout, check=True)
        tar.wait()
        answers = f"IP={ip}\nCTID={ct}\nCT_HOSTNAME=noc-test\nSITE_NAME=Install test\nWITH_TOKEN=y\nWITH_NOC_HW=n\nWITH_LOGS=y\nSPEED_URL=\n"
        answers += f"GW={os.environ.get('TEST_GW', '')}\n" if os.environ.get("TEST_GW") else ""
        try:
            self.ssh(f"cat > {src}/answers", stdin=answers)
            r = self.ssh(f"sh {src}/install.sh --yes --config {src}/answers --source {src}")
            self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-1500:])
            self.assertIn(f"http://{ip.split('/')[0]}:8080", r.stdout)
            check = self.ssh(f"""pct exec {ct} -- sh -c '
                systemctl is-active noc rsyslog cron;
                curl -fs -m 5 http://127.0.0.1:8080/healthz;
                echo; curl -s -o /dev/null -w "%{{http_code}}\\n" -m 5 http://127.0.0.1:8080/api/overview;
                stat -c %a /etc/noc/secrets.env /etc/noc/config.json;
                python3 -c "import json;c=json.load(open(\\"/etc/noc/config.json\\"));print(c[\\"site_name\\"],c[\\"proxmox\\"][\\"node\\"])";
                grep -c "^PVE_TOKEN=noc@pve!noc-{ct}=" /etc/noc/secrets.env; ls /etc/cron.d/noc-speedwatch'""")
            lines = check.stdout.split("\n")
            self.assertEqual(lines[:3], ["active", "active", "active"], check.stdout + check.stderr)
            self.assertIn('{"ok":true}', check.stdout.replace(" ", ""))
            self.assertIn("401", lines, "the API needs a login")
            self.assertIn("600", lines)
            self.assertIn("Install test", check.stdout)
            self.assertIn("1", lines, "the token is stored")
            # upgrade keeps the config and comes back up
            self.ssh(f"pct exec {ct} -- sh -c 'echo marker > /var/lib/noc/keep; sed -i s/Install.test/Edited/ /etc/noc/config.json'")
            r = self.ssh(f"sh {src}/install.sh --yes --upgrade {ct} --source {src}")
            self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-1500:])
            after = self.ssh(f"pct exec {ct} -- sh -c 'sleep 3; systemctl is-active noc; cat /var/lib/noc/keep; grep -c Edited /etc/noc/config.json'")
            self.assertEqual(after.stdout.split(), ["active", "marker", "1"], after.stdout + after.stderr)
        finally:
            self.ssh(f"pct stop {ct} 2>/dev/null; pct destroy {ct} --purge 2>/dev/null; pveum user token remove noc@pve noc-{ct} 2>/dev/null; rm -rf {src}", timeout=120)
        self.assertNotEqual(self.ssh(f"pct status {ct}").returncode, 0, "the test container must be gone")


if __name__ == "__main__":
    unittest.main()
