"""Proxmox backup (vzdump) watcher for the Telegram alerts.

Reads Proxmox with the NOC's read-only API token (PVEAuditor is enough):
- a vzdump task that finishes -> one message with every guest's result, size and time
  (failures always; successes unless telegram.backup_success is false),
- a task still running after `backup_long_h` hours -> one alert (e.g. hung on a dead NFS mount),
- a scheduled job whose start time passed with no backup task -> "missed",
- backup storage of an enabled job unavailable or above `backup_store_warn_pct` -> a normal
  debounced check, so it alerts and recovers like any other.
"""
import html
import json
import re
import time
import urllib.parse

e = html.escape

START = re.compile(r"Starting Backup of VM (\d+) \((qemu|lxc)\)")
NAME = re.compile(r"(?:VM|CT) Name: (.+)$")
SIZE = re.compile(r"archive file size: (\S+)")
DONE = re.compile(r"Finished Backup of VM (\d+) \((\d+):(\d+):(\d+)\)")
FAIL = re.compile(r"ERROR: Backup of VM (\d+) failed - (.+)$")
POLL_S = 60
MISSED_GRACE_S = 1800


def dur(seconds):
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {s % 3600 // 60}m"


def parse_log(lines):
    """vzdump task log -> per-guest results plus job-level errors and warnings."""
    guests, cur, errors, warns = [], None, [], []
    for line in lines:
        if m := START.search(line):
            cur = {"vmid": m.group(1), "kind": m.group(2), "name": "", "size": None, "took": None, "error": None}
            guests.append(cur)
        elif m := FAIL.search(line):
            g = next((g for g in guests if g["vmid"] == m.group(1)), None)
            if g:
                g["error"] = m.group(2).strip()
            else:
                errors.append(line.strip())
        elif m := DONE.search(line):
            g = next((g for g in guests if g["vmid"] == m.group(1)), cur)
            if g:
                g["took"] = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
        elif cur and (m := NAME.search(line)):
            cur["name"] = m.group(1).strip()
        elif cur and (m := SIZE.search(line)):
            cur["size"] = m.group(1)
        elif line.startswith("ERROR:"):
            errors.append(line.strip())
        elif line.startswith("WARN"):
            warns.append(line.strip())
    return {"guests": guests, "errors": errors, "warnings": warns}


class Watcher:
    def __init__(self, noc):
        self.noc = noc
        self.seen = None        # finished vzdump UPIDs already reported
        self.long_sent = set()  # running UPIDs already reported as slow
        self.next_run = {}      # job id -> next-run last seen
        self.due = []           # [(job id, scheduled ts)] waiting for a task to start
        self.last_poll = 0.0
        self.parsed = {}        # finished UPID -> parse_log() result (a finished log never changes)
        self.state_path = noc.VAR / "backup_watch.json"

    @property
    def node(self):
        return self.noc.PVE.get("node")

    def tasks(self):
        return self.noc.pve(f"/nodes/{self.node}/tasks?typefilter=vzdump&limit=30")

    def task_log(self, upid):
        rows = self.noc.pve(f"/nodes/{self.node}/tasks/{urllib.parse.quote(upid, safe='')}/log?start=0&limit=50000")
        return [r.get("t", "") for r in rows]

    def parsed_log(self, upid):
        if upid not in self.parsed:
            self.parsed[upid] = parse_log(self.task_log(upid))
            while len(self.parsed) > 200:
                self.parsed.pop(next(iter(self.parsed)))
        return self.parsed[upid]

    def jobs(self):
        return [j for j in self.noc.pve("/cluster/backup") if j.get("type", "vzdump") == "vzdump"]

    def storages(self):
        return {s["storage"]: s for s in self.noc.pve(f"/nodes/{self.node}/storage")}

    def job_storages(self, jobs):
        return sorted({j["storage"] for j in jobs if j.get("enabled", 1) and j.get("storage")})

    # ------------------------------------------------------------ watcher
    def _load_last_end(self):
        try:
            return float(json.loads(self.state_path.read_text()).get("last_end", 0))
        except (OSError, ValueError):
            return 0.0

    def _save_last_end(self, ts):
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({"last_end": ts}))
        except OSError:
            pass

    def poll(self, cfg, site):
        """Messages to send now (list of HTML strings). Called from the watch loop."""
        now = time.time()
        if now - self.last_poll < POLL_S:
            return []
        self.last_poll = now
        tasks, jobs, out = self.tasks(), self.jobs(), []
        finished = [t for t in tasks if t.get("endtime")]
        if self.seen is None:
            # Report what finished while the NOC was not running (up to 6 h back), nothing older.
            last = max(self._load_last_end(), now - 6 * 3600)
            self.seen = {t["upid"] for t in finished if t["endtime"] <= last}
            for j in jobs:
                self.next_run[j["id"]] = j.get("next-run")
        for t in sorted(finished, key=lambda t: t["endtime"]):
            if t["upid"] in self.seen:
                continue
            self.seen.add(t["upid"])
            self.long_sent.discard(t["upid"])
            msg = self.finished_message(t, cfg, site, jobs)
            if msg:
                out.append(msg)
        if finished:
            self._save_last_end(max(t["endtime"] for t in finished))
        self.seen &= {t["upid"] for t in tasks}  # forget tasks that dropped out of the list

        long_s = float(cfg["backup_long_h"]) * 3600
        for t in tasks:
            if not t.get("endtime") and now - t.get("starttime", now) > long_s and t["upid"] not in self.long_sent:
                self.long_sent.add(t["upid"])
                out.append(f"⏳ <b>{e(site)}: backup still running</b> after {dur(now - t['starttime'])}"
                           f" (started {time.strftime('%H:%M', time.localtime(t['starttime']))}).\n"
                           "It may be stuck, e.g. on an unreachable NFS share. Check Proxmox → Tasks.")

        for j in jobs:
            nr, prev = j.get("next-run"), self.next_run.get(j["id"])
            if j.get("enabled", 1) and prev and nr and nr > prev:
                self.due.append((j["id"], prev, j.get("schedule", "")))
            self.next_run[j["id"]] = nr
        keep = []
        for jid, at, sched in self.due:
            if any(t.get("starttime", 0) >= at - 300 for t in tasks):
                continue
            if now > at + MISSED_GRACE_S:
                out.append(f"🔴 <b>{e(site)}: backup missed</b>\nJob {e(jid)} ({e(sched)}) was due at "
                           f"{time.strftime('%H:%M', time.localtime(at))} but no backup started.")
                continue
            keep.append((jid, at, sched))
        self.due = keep
        return out

    def finished_message(self, t, cfg, site, jobs):
        status = t.get("status") or "unknown"
        try:
            p = self.parsed_log(t["upid"])
        except Exception as ex:
            p = {"guests": [], "errors": [f"could not read the task log: {ex}"], "warnings": []}
        failed = [g for g in p["guests"] if g["error"] or g["took"] is None]
        bad = status not in ("OK",) and not status.startswith("WARNINGS")
        if bad or failed:
            icon, title = "❌", "backup FAILED" if not p["guests"] or len(failed) == len(p["guests"]) else f"backup: {len(failed)} of {len(p['guests'])} failed"
        elif status.startswith("WARNINGS") or p["errors"]:
            icon, title = "⚠️", "backup finished with warnings"
        else:
            if not cfg.get("backup_success", True):
                return None
            icon, title = "✅", "backup finished"
        took = t["endtime"] - t.get("starttime", t["endtime"])
        n_ok = len(p["guests"]) - len(failed)
        lines = [f"{icon} <b>{e(site)}: {title}</b>"
                 + (f" · {n_ok}/{len(p['guests'])} guests" if p["guests"] else "") + f" · {dur(took)}"]
        for g in p["guests"]:
            label = f"{g['vmid']} {g['name']}".strip()
            if g in failed:
                lines.append(f"❌ {e(label)}: {e(g['error'] or 'did not finish')}")
            else:
                lines.append(f"✅ {e(label)} · {e(g['size'] or '?')} · {dur(g['took'])}")
        if not p["guests"] and bad:
            lines.append(f"Status: {e(status)}")
        lines += [f"• {e(x)}" for x in p["errors"][:5]]
        if p["warnings"]:
            lines.append("<i>" + "\n".join(e(w) for w in p["warnings"][:3]) + "</i>")
        try:
            st = self.storages()
            for s in self.job_storages(jobs):
                if s in st and st[s].get("total"):
                    lines.append(f"💾 {e(s)}: {st[s]['used'] / 1e9:.0f} of {st[s]['total'] / 1e9:.0f} GB used "
                                 f"({100 * st[s]['used'] / st[s]['total']:.0f}%)")
        except Exception:
            pass
        return "\n".join(lines)

    def store_readings(self, cfg):
        """Debounced checks: every storage an enabled backup job writes to is available and not full."""
        warn = float(cfg["backup_store_warn_pct"])
        jobs, out = self.jobs(), {}
        if not jobs:
            return out
        st = self.storages()
        for s in self.job_storages(jobs):
            info = st.get(s)
            if not info or not info.get("active"):
                out[f"backup:store:{s}"] = (False, f"Backup storage {s} is not available")
                continue
            pct = 100 * info.get("used", 0) / info["total"] if info.get("total") else 0
            out[f"backup:store:{s}"] = (pct < warn, f"Backup storage {s}: {pct:.0f}% used (warn at {warn:.0f}%)")
        return out

    # ------------------------------------------------------------ /backups
    def overview(self, site):
        jobs, now = self.jobs(), time.time()
        if not jobs:
            return "No backup jobs in Proxmox (Datacenter → Backup)."
        names = {}
        for kind in ("qemu", "lxc"):
            for g in self.noc.pve(f"/nodes/{self.node}/{kind}"):
                names[str(g["vmid"])] = g.get("name") or f"{kind} {g['vmid']}"
        # Latest good backup per guest, from the backup task logs: a read-only (auditor) token
        # can't list backup files, but it can read tasks.
        latest = {}
        for t in sorted((t for t in self.tasks() if t.get("endtime")), key=lambda t: -t["endtime"]):
            try:
                guests = self.parsed_log(t["upid"])["guests"]
            except Exception:
                continue
            for g in guests:
                if not g["error"] and g["took"] is not None and g["vmid"] not in latest:
                    latest[g["vmid"]] = {"ctime": t["endtime"], "size": g["size"]}
        lines = [f"<b>{e(site)}: backups</b>"]
        covered = set()
        for j in jobs:
            if j.get("all"):
                ids = set(names) - set(str(j.get("exclude") or "").replace(" ", "").split(","))
            else:
                ids = {v for v in str(j.get("vmid") or "").replace(" ", "").split(",") if v}
            covered |= ids
            nr = j.get("next-run")
            state = "disabled" if not j.get("enabled", 1) else (f"next {time.strftime('%a %H:%M', time.localtime(nr))}" if nr else "no next run")
            keep = ", ".join(f"{k.replace('keep-', '')} {v}" for k, v in (j.get("prune-backups") or {}).items()) or "keep all"
            lines.append(f"🗓 {e(j.get('schedule', '?'))} → {e(j.get('storage', '?'))} · {len(ids)} guests · {state} · {e(keep)}")
        for v in sorted(names, key=int):
            b = latest.get(v)
            if b:
                age = now - b["ctime"]
                icon = "🟢" if age < 36 * 3600 else "🟠"
                lines.append(f"{icon} {e(v)} {e(names[v])} · {self.noc_ago(age)} ago · {e(b['size'] or '?')}")
            else:
                lines.append(f"🔴 {e(v)} {e(names[v])} · no recent backup" + ("" if v in covered else " (not in any job)"))
        try:
            st = self.storages()
            for s in self.job_storages(jobs):
                if s in st and st[s].get("total"):
                    lines.append(f"💾 {e(s)}: {st[s]['used'] / 1e9:.0f} of {st[s]['total'] / 1e9:.0f} GB used")
        except Exception:
            pass
        return "\n".join(lines)

    @staticmethod
    def noc_ago(s):
        s = int(max(0, s))
        if s < 5400:
            return f"{s // 60}m"
        if s < 172800:
            return f"{s // 3600}h"
        return f"{s // 86400}d"
