"""Set the NOC login password from the console (the reset when Telegram is not available).

Run as root on the NOC:  noc-passwd   (installed by deploy/install.sh; or python3 /opt/noc/passwd.py)
It writes the hash to secrets.env and restarts the noc service, which signs every session out.
"""
import getpass
import os
import subprocess
import sys
from pathlib import Path

import auth

ETC = Path(os.environ.get("NOC_ETC", "/etc/noc"))


def write_env(path, updates):
    lines = path.read_text().splitlines() if path.exists() else []
    out, done = [], set()
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if k in updates:
            out.append(f"{k}={updates[k]}")
            done.add(k)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in updates.items() if k not in done]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def main():
    path = ETC / "secrets.env"
    if not os.access(path, os.W_OK):
        sys.exit(f"Cannot write {path}: run as root.")
    user = next((ln.split("=", 1)[1].strip() for ln in path.read_text().splitlines()
                 if ln.startswith("NOC_ADMIN_USER=")), "admin")
    print(f"New password for NOC user '{user}'.")
    pw = getpass.getpass("New password: ")
    why = auth.check_new(pw, user)
    if why:
        sys.exit(f"Not changed: {why}.")
    if getpass.getpass("Again: ") != pw:
        sys.exit("Not changed: the passwords do not match.")
    write_env(path, auth.updates(pw))
    print("Saved.")
    if "--no-restart" not in sys.argv:
        r = subprocess.run(["systemctl", "restart", "noc"])
        print("noc restarted: sign in with the new password." if r.returncode == 0 else "Restart noc yourself: systemctl restart noc")


if __name__ == "__main__":
    main()
