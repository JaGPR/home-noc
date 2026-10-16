# Daily publish task

A Claude Code scheduled task (`home-noc-daily-publish`) keeps this repo in step with the
live NOC every morning: it copies the live app files into the repo, runs a privacy scan and
the smoke test, then commits, pushes and checks CI. It never changes any network device.

To set it up again on a new PC, create a scheduled task with the prompt below and fill in
the placeholders:

| Placeholder | Meaning |
|---|---|
| `<repo>` | Local clone of this repo (Git Bash path, e.g. `/c/Users/<you>/code/home-noc`) |
| `<noc-host>` | The NOC container, reached as `root@<noc-host>` with key auth |
| `<patterns-file>` | Private file with one extended regex per line: your LAN prefixes, MAC OUIs, hostnames, ISP names, domains, token fragments. Keep it **outside** the repo (in your private backup) |

The real config (`/etc/noc/config.json`) and secrets (`/etc/noc/secrets.env`) are never
copied into the repo; keep them in a private backup (see the end of this page).

## Prompt

```
Publish the latest Home NOC to GitHub so that the repo always has the newest version each
morning. You are authorised to commit and push on your own, provided the safety gates below
pass. Never change any network device, router, switch, Pi-hole or Proxmox setting: this task
only syncs files, tests, commits and pushes.

Facts
- Local repo: <repo> (origin on GitHub, branch main). Git identity and credentials are
  configured. Push non-interactively: GCM_INTERACTIVE=never GIT_TERMINAL_PROMPT=0 git push
- The live NOC: ssh -o BatchMode=yes root@<noc-host>. Live app files: /opt/noc/app.py,
  notify.py, diagnose.py, settings.py, openwrt.py and /opt/noc/static/*.html.
  /etc/noc/config.json and /etc/noc/secrets.env must NEVER be copied into the repo.
- The PC has no FastAPI; run the smoke test inside the container.

Steps
1. cd <repo> && git fetch -q origin && git status --short. If behind origin, git pull --rebase;
   on conflict stop and report.
2. Sync live -> repo for app.py, notify.py, diagnose.py, settings.py, openwrt.py,
   static/index.html, static/login.html (live: ssh root@<noc-host> cat /opt/noc/<file>;
   repo: app/<file>). Identical: nothing. Different and repo copy clean
   (git diff --quiet -- app/<file>): copy live version in (LF endings). Different and repo
   copy has uncommitted edits: do not overwrite; stop and report the conflict.
3. Privacy gate: grep -rnIiE -f <patterns-file> --exclude-dir=.git .
   Allowed hits only: variable names like PVE_TOKEN= / AccessToken= in code and templates,
   and the author name in LICENSE. git ls-files must contain no secrets.env or real
   config.json. Any other hit: do NOT commit or push; report the exact lines.
4. Test gate: tar --force-local -czf /tmp/hn.tgz app tests config; stream it to the
   container (ssh root@<noc-host> 'rm -rf /tmp/hn && mkdir /tmp/hn && tar --no-same-owner
   -xzf - -C /tmp/hn' < /tmp/hn.tgz), run cd /tmp/hn && python3 tests/smoke_test.py, then
   rm -rf /tmp/hn. Also ast.parse the five app/*.py files and sh -n deploy/*.sh and
   pihole-ha/*.sh locally. Any failure: do NOT push; report the output.
5. No changes in git status --short: report "Already up to date" and stop.
6. If app.py gained a feature (new tab, action kind, endpoint), add one concise line to the
   README Features table and, if relevant, docs/deployment.md. Re-run the privacy gate.
7. git add -A; commit with a plain-words subject summarising the day's changes, a short
   bullet body from the diff, and a Co-Authored-By: Claude <noreply@anthropic.com> trailer.
   Push.
8. Confirm CI: poll https://api.github.com/repos/<owner>/home-noc/actions/runs?per_page=5
   for the run matching the new commit (every ~15 s, max ~10 min), then list each job's
   conclusion. If the API answers 404 (private repo), report "CI not checked" with the
   Actions link; do not authenticate.

Report (short): files synced; gates pass/fail; commit hash + subject, push result, CI per
job with link; anything needing attention.
```

## Private backup

Keep these outside the repo, somewhere private (password manager, encrypted drive):

```bash
mkdir -p ~/Documents/home-noc-private-backup && scp root@<noc-host>:/etc/noc/config.json root@<noc-host>:/etc/noc/secrets.env ~/Documents/home-noc-private-backup/
```

Also keep `<patterns-file>` there. To restore, build the container with `deploy/`
([deployment.md](deployment.md)), then copy both files back to `/etc/noc/` and restart the
`noc` service.
