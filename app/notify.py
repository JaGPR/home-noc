"""Telegram alerts and bot for the NOC.

Watcher: every `check_every_s` seconds it reads devices, WAN links, Pi-holes and the
fibre ONTs, and sends a message when a check changes state and stays changed for
`confirm_checks` checks in a row, so one lost ping doesn't page you. Messages that
can't be sent (e.g. all internet links are down) wait in an outbox and go out late,
marked with the time they happened.

Bot: answers only the paired chat. Pairing: the NOC page shows a one-time code and
you send `/pair <code>` to the bot. Read commands reply at once; actions reply with
the same preview the NOC shows and run only after you press Confirm (2 min expiry).

The bot token is read from secrets.env (TELEGRAM_BOT_TOKEN) and never logged.
"""
import concurrent.futures as cf
import html
import secrets
import threading
import time

import requests

import backups
import diagnose

DEFAULTS = {"check_every_s": 30, "confirm_checks": 2, "ont_stale_s": 180,
            "backup_alerts": True, "backup_success": True, "backup_store_warn_pct": 85, "backup_long_h": 4}
PAIR_TTL = 600
ACTION_TTL = 120

HELP = """<b>NOC bot</b>
/status – everything at a glance
/wan – internet links and groups
/dns – Pi-holes, ad-blocking, VIP
/optics – fibre ONT readings
/devices – devices that are down (/devices all)
/problems – what is currently not OK
/backups – Proxmox backup jobs and each guest's latest backup
/diagnose &lt;link|group&gt; – why a link is down, layer by layer

<b>Actions</b> (preview first, then Confirm)
/restart &lt;link|group&gt; – e.g. /restart wan3, or a group name from /wan
/pause &lt;minutes&gt; – pause ad-blocking (0 = until /resume)
/resume – resume ad-blocking"""

e = html.escape


def ago(seconds):
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


class Telegram:
    def __init__(self, noc):
        self.noc = noc
        self.states = {}        # check key -> {"ok", "since", "streak", "text"}
        self.diag = {}          # WAN interface -> latest diagnosis while it is down
        self.vip = None         # {"owner", "cand", "streak"}
        self.mgmt_down = {}     # device check key -> first down reading (telegram.mgmt_grace)
        self.outbox = []        # [{"text", "ts"}] waiting for the internet / Telegram
        self.pending = {}       # action id -> {"plan", "kind", "exp", "chat"}
        self.pairing = None     # (code, expires)
        self.token_cache = (0.0, "")
        self.bot_name = None
        self.last_check = None
        self.watch_error = None
        self.bot_error = None
        self.started = False
        self.log_path = noc.VAR / "alerts.jsonl"
        self.backups = backups.Watcher(noc)
        self.backup_error = None

    # ------------------------------------------------------------ config / API
    def cfg(self):
        return {**DEFAULTS, **(self.noc.C.get("telegram") or {})}

    def chat_id(self):
        return (self.noc.C.get("telegram") or {}).get("chat_id")

    def token(self):
        """Re-read secrets.env every 30 s so adding the token needs no restart."""
        t, tok = self.token_cache
        if time.time() - t > 30:
            try:
                tok = self.noc.load_env(self.noc.ETC / "secrets.env").get("TELEGRAM_BOT_TOKEN", "")
            except OSError:
                tok = ""
            self.token_cache = (time.time(), tok)
        return tok

    def api(self, method, http_timeout=15, **params):
        tok = self.token()
        if not tok:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in secrets.env")
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/{method}", json=params, timeout=http_timeout)
            d = r.json()
        except Exception as ex:  # the URL contains the token: never let it reach a log
            raise RuntimeError(str(ex).replace(tok, "<token>")) from None
        if not d.get("ok"):
            raise RuntimeError(f"Telegram: {d.get('description', 'error')}")
        return d["result"]

    def send(self, chat, text, **kw):
        return self.api("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                        disable_web_page_preview=True, **kw)

    def devices(self):
        d = self.noc.cached("ping", 10, self.noc.collect_ping)
        if isinstance(d, dict):  # collector failed
            raise RuntimeError(d.get("error", "device checks failed"))
        return d

    def queue(self, text):
        if not self.chat_id():
            return  # nobody to tell yet; state is still tracked for the NOC page
        self.outbox.append({"text": text, "ts": time.time()})
        del self.outbox[:-100]

    def flush(self):
        chat = self.chat_id()
        if not chat or not self.token():
            return
        while self.outbox:
            m = self.outbox[0]
            late = time.time() - m["ts"] > 120
            text = m["text"] + (f"\n<i>(delayed: happened at {time.strftime('%H:%M', time.localtime(m['ts']))})</i>" if late else "")
            try:
                self.send(chat, text)
            except Exception:
                return  # keep it; retry next round
            self.outbox.pop(0)

    # ------------------------------------------------------------ checks
    def evaluate(self):
        """Current state of every check: {key: (ok, text)}, plus the DNS VIP owner."""
        noc, out, vip = self.noc, {}, None
        try:
            for d in self.devices():
                out[f"device:{d['name']}"] = (bool(d["up"]), f"{d['name']} ({d['ip']})")
        except Exception as ex:
            out["source:devices"] = (False, f"Device checks failed: {ex}")

        r = noc.cached("router", 15, noc.collect_router)
        if not isinstance(r, dict) or "error" in r:
            out["source:router"] = (False, f"Router not reachable over SSH: {(r or {}).get('error', '?')}")
        else:
            out["source:router"] = (True, "Router reachable over SSH again")
            ifs = {i["name"]: i for i in r.get("interfaces", [])}
            wans = sorted(noc.wan_ifaces())
            online = 0
            for w in wans:
                i, m = ifs.get(w), (r.get("mwan") or {}).get(w)
                if m:
                    ok, how = m.get("status") == "online", m.get("status") or "unknown"
                elif i:
                    ok, how = bool(i.get("up")), "up" if i.get("up") else "down"
                else:
                    ok, how = False, "interface missing"
                g = noc.wan_group_of(w)
                ip = f", {i['ip'].split('/')[0]}" if ok and i and i.get("ip") else ""
                out[f"wan:{w}"] = (ok, f"{w}{f' [{g}]' if g else ''}: {how}{ip}")
                online += ok
            if wans:
                out["wan:ALL"] = (online > 0, f"Internet: {online}/{len(wans)} links online")
            for o in r.get("onts", []):
                self.ont_checks(o, out)

        try:
            d = noc.cached("dns", 10, noc.collect_dns)
            for n in d.get("nodes", []):
                out[f"dns:{n['name']}"] = (bool(n["ok"]), f"Pi-hole {n['name']}" + ("" if n["ok"] else f": {n.get('error')}"))
                if n["ok"]:
                    timer = f", back on in {int(n['timer']) // 60 + 1} min" if n.get("timer") else ""
                    out[f"block:{n['name']}"] = (n.get("blocking") == "enabled", f"Ad-blocking on {n['name']}: {n.get('blocking')}{timer}")
            vip = d.get("vip_owner")
        except Exception as ex:
            out["source:dns"] = (False, f"DNS checks failed: {ex}")

        if self.cfg()["backup_alerts"] and noc.PVE.get("url"):
            r = noc.cached("backup_store", 60, lambda: self.backups.store_readings(self.cfg()))
            if isinstance(r.get("error"), str):  # Proxmox API failed: report it, don't page for it
                self.backup_error = r["error"]
            else:
                out.update(r)
        self.apply_grace(out)
        return out, vip

    def apply_grace(self, out):
        """Some ISP boxes drop their management address for a minute or two now and then while
        traffic keeps flowing. For devices in telegram.mgmt_grace ({name: seconds}) a down reading
        counts as OK until it has lasted that long, unless their WAN group is fully down or the
        fibre (the group's ONT) is not in O5 / has an alarm."""
        grace, now = self.cfg().get("mgmt_grace") or {}, time.time()
        paths = self.noc.ROUTER.get("wan_paths") or {}
        for name, secs in grace.items():
            key = f"device:{name}"
            if key not in out:
                continue
            if out[key][0]:
                self.mgmt_down.pop(key, None)
                continue
            since = self.mgmt_down.setdefault(key, now)
            serious, onts = False, []
            for g, path in paths.items():
                if path.get("isp_device") != name:
                    continue
                wans = [f"wan:{w}" for w in self.noc.wan_groups().get(g, [])]
                serious |= bool(wans) and all(w in out and not out[w][0] for w in wans)
                if path.get("ont"):
                    link = f"ont:{path['ont']}:link"
                    # while the box is unreachable its poller has no fresh reading: use the last known state
                    fibre_ok = out[link][0] if link in out else (self.states.get(link) or {}).get("ok", True)
                    serious |= not fibre_ok
                    onts.append(path["ont"])
            if not serious and now - since < float(secs):
                out[key] = (True, out[key][1])  # held: management-only blip
                for o in onts:  # the poller can't reach it either; that is the same blip
                    if f"ont:{o}:poll" in out:
                        out[f"ont:{o}:poll"] = (True, out[f"ont:{o}:poll"][1])

    def ont_checks(self, o, out):
        n, lim = o["name"], o.get("limits") or {}
        age = o.get("age_s")
        fresh = bool(o.get("connected")) and age is not None and age <= self.cfg()["ont_stale_s"]
        out[f"ont:{n}:poll"] = (fresh, f"{n}: " + ("reporting again" if fresh else
                                                     f"no reading from its router poller for {ago(age) if age is not None else '?'}"))
        if not fresh:
            return
        bad_alarms = [k.upper() for k, v in (o.get("alarms") or {}).items() if v and not str(v).lower().startswith("clear")]
        state = o.get("state_code")
        link_ok = not bad_alarms and (state in (None, "O5"))
        out[f"ont:{n}:link"] = (link_ok, f"{n}: {o.get('state') or state or '?'}" + (f", alarms {', '.join(bad_alarms)}" if bad_alarms else ""))

        def rng(key, label, val, lo, hi, unit):
            if val is None or lo is None or hi is None:
                return
            out[f"ont:{n}:{key}"] = (lo <= val <= hi, f"{n} {label} {val} {unit} (normal {lo} to {hi})")
        rng("rx", "RX power", o.get("rx_dbm"), lim.get("rx_pwr_low_warn"), lim.get("rx_pwr_high_warn"), "dBm")
        rng("tx", "TX power", o.get("tx_dbm"), lim.get("tx_pwr_low_warn"), lim.get("tx_pwr_high_warn"), "dBm")
        rng("temp", "temperature", o.get("temp_c"), lim.get("temp_low_warn"), lim.get("temp_high_warn"), "°C")

    def step(self, readings, first):
        """Debounce: a check flips only after `confirm_checks` consecutive readings disagree."""
        need, now, changes = max(1, int(self.cfg()["confirm_checks"])), time.time(), []
        for k, (ok, text) in readings.items():
            s = self.states.get(k)
            if s is None:
                # At startup take things as they are; checks added later start as OK so a
                # new problem still goes through the normal confirmation.
                self.states[k] = {"ok": ok if first else True, "since": now, "streak": 0, "text": text}
                s = self.states[k]
                if first:
                    continue
            s["text"] = text
            if ok == s["ok"]:
                s["streak"] = 0
                continue
            s["streak"] += 1
            if s["streak"] >= need:
                changes.append({"key": k, "ok": ok, "text": text, "lasted": now - s["since"]})
                s.update(ok=ok, since=now, streak=0)
        return changes

    def vip_step(self, owner, first):
        need = max(1, int(self.cfg()["confirm_checks"]))
        if owner is None:
            return None
        if self.vip is None or first:
            self.vip = {"owner": owner, "cand": None, "streak": 0}
            return None
        v = self.vip
        if owner == v["owner"]:
            v.update(cand=None, streak=0)
            return None
        v["streak"] = v["streak"] + 1 if owner == v["cand"] else 1
        v["cand"] = owner
        if v["streak"] >= need:
            old = v["owner"]
            v.update(owner=owner, cand=None, streak=0)
            return old, owner
        return None

    def format_changes(self, changes, vip_move, diag=None):
        site = e(self.noc.SITE_NAME)
        lines = [f"<b>{site}</b> · {time.strftime('%H:%M')}"]
        allwan = next((c for c in changes if c["key"] == "wan:ALL"), None)
        if allwan and not allwan["ok"]:
            lines.append("🚨 <b>ALL INTERNET LINKS ARE DOWN</b>")
        down = [c for c in changes if not c["ok"] and c["key"] != "wan:ALL"]
        up = [c for c in changes if c["ok"] and c["key"] != "wan:ALL"]
        if down:
            lines.append("🔴 <b>Problem</b>")
            lines += [f"• {e(c['text'])}" for c in down]
        if up:
            lines.append("🟢 <b>Recovered</b>")
            lines += [f"• {e(c['text'])} <i>(was bad for {ago(c['lasted'])})</i>" for c in up]
        if allwan and allwan["ok"]:
            lines.append(f"🌐 Internet is back after {ago(allwan['lasted'])}")
        if diag:
            lines.append("🔎 <b>Why</b>")
            lines += self.format_causes(diag)
        if vip_move:
            lines.append(f"🔁 DNS VIP {e(str(self.noc.DNS_VIP))} moved: {e(vip_move[0])} → <b>{e(vip_move[1])}</b>")
        return "\n".join(lines)

    def log(self, changes, vip_move):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entries = [{"ts": ts, "key": c["key"], "ok": c["ok"], "text": c["text"], "lasted_s": int(c["lasted"])}
                   for c in changes]
        if vip_move:
            entries.append({"ts": ts, "key": "vip", "ok": True, "text": f"DNS VIP moved: {vip_move[0]} -> {vip_move[1]}"})
        try:
            self.noc.append_jsonl(self.log_path, entries)
        except OSError:
            pass

    def problems(self):
        now = time.time()
        return [{"key": k, "text": s["text"], "for_s": int(now - s["since"]),
                 "cause": (self.diag.get(k[4:]) or {}).get("verdict") if k.startswith("wan:") else None}
                for k, s in sorted(self.states.items()) if not s["ok"]]

    # ------------------------------------------------------------ diagnosis
    def run_diagnoses(self, ifaces):
        """Diagnose WANs in parallel; remembers each result until the link recovers."""
        ifaces = [i for i in dict.fromkeys(ifaces) if i]
        if not ifaces:
            return {}
        out = {}
        with cf.ThreadPoolExecutor(min(6, len(ifaces))) as ex:
            for i, res in zip(ifaces, ex.map(self.safe_diagnose, ifaces)):
                out[i] = self.diag[i] = res
        return out

    def safe_diagnose(self, iface):
        try:
            return diagnose.diagnose(self.noc, iface)
        except Exception as ex:
            return {"iface": iface, "group": self.noc.wan_group_of(iface), "steps": [], "layer": None,
                    "verdict": f"diagnosis failed: {ex}", "ts": time.time()}

    def format_causes(self, diag):
        """One line per distinct cause; links sharing a cause are merged into one line."""
        by_cause = {}
        for d in diag.values():
            by_cause.setdefault(d["verdict"], []).append(d)
        lines = []
        for verdict, ds in by_cause.items():
            names = ", ".join(d["iface"] for d in ds)
            groups = ", ".join(sorted({d["group"] for d in ds if d.get("group")}))
            _, trail = diagnose.short(ds[0])
            lines.append(f"• <b>{e(names)}</b>" + (f" [{e(groups)}]" if groups else "") + f": {e(verdict)}"
                         + (f"\n  <i>{e(trail)}</i>" if trail else ""))
        return lines

    def format_diag(self, d):
        def icon(s):
            return "✅" if s["ok"] else "❔" if s["ok"] is None else "▫️" if s.get("consequence") else "❌"
        head = f"🔎 <b>{e(d['iface'])}</b>" + (f" [{e(d['group'])}]" if d.get("group") else "")
        lines = [head, f"<b>{'Cause' if d.get('layer') else 'Result'}:</b> {e(d['verdict'])}"]
        lines += [f"{icon(s)} {e(s['layer'])}: {e(s['detail'])}" + (" <i>(follows from the cause)</i>" if s.get("consequence") else "")
                  for s in d["steps"]]
        return "\n".join(lines)

    def watch_loop(self):
        first = True
        while True:
            t0 = time.time()
            try:
                readings, vip = self.evaluate()
                changes = self.step(readings, first)
                vip_move = self.vip_step(vip, first)
                down_wans = [c["key"][4:] for c in changes
                             if not c["ok"] and c["key"].startswith("wan:") and c["key"] != "wan:ALL"]
                for c in changes:
                    if c["ok"] and c["key"].startswith("wan:"):
                        self.diag.pop(c["key"][4:], None)
                if first:
                    self.run_diagnoses([k[4:] for k, st in self.states.items()
                                        if k.startswith("wan:") and k != "wan:ALL" and not st["ok"]])
                    bad = self.problems()
                    self.queue(f"<b>{e(self.noc.SITE_NAME)}</b>: monitoring started, {len(self.states)} checks. " +
                               ("Not OK right now:\n" + "\n".join(f"• {e(p['text'])}" + (f"\n  ↳ {e(p['cause'])}" if p.get("cause") else "") for p in bad) if bad else "Everything is OK ✅"))
                elif changes or vip_move:
                    self.queue(self.format_changes(changes, vip_move, self.run_diagnoses(down_wans)))
                    self.log(changes, vip_move)
                self.last_check, self.watch_error, first = time.time(), None, False
            except Exception as ex:
                self.watch_error = f"{type(ex).__name__}: {ex}"
            self.backup_step()
            self.flush()
            time.sleep(max(5, int(self.cfg()["check_every_s"]) - (time.time() - t0)))

    def backup_step(self):
        """Finished / stuck / missed Proxmox backups (see backups.py)."""
        if not (self.cfg()["backup_alerts"] and self.noc.PVE.get("url")):
            return
        try:
            for text in self.backups.poll(self.cfg(), self.noc.SITE_NAME):
                self.queue(text)
                self.log([{"key": "backup", "ok": not text.startswith(("❌", "🔴", "⏳")),
                           "text": text.split("\n")[0].replace("<b>", "").replace("</b>", ""), "lasted": 0}], None)
            self.backup_error = None
        except Exception as ex:
            self.backup_error = f"{type(ex).__name__}: {ex}"

    # ------------------------------------------------------------ bot
    def new_pairing_code(self):
        code = f"{secrets.randbelow(10 ** 6):06d}"
        self.pairing = (code, time.time() + PAIR_TTL)
        return code

    def bot_loop(self):
        offset = None
        while True:
            if not self.token():
                time.sleep(30)
                continue
            try:
                if not self.bot_name:
                    self.bot_name = self.api("getMe").get("username")
                updates = self.api("getUpdates", http_timeout=40, offset=offset, timeout=25,
                                   allowed_updates=["message", "callback_query"])
                self.bot_error = None
                for u in updates:
                    offset = u["update_id"] + 1
                    try:
                        self.handle(u)
                    except Exception as ex:
                        self.bot_error = f"{type(ex).__name__}: {ex}"
            except Exception as ex:
                self.bot_error = str(ex)
                time.sleep(10)

    def handle(self, u):
        if "callback_query" in u:
            return self.on_button(u["callback_query"])
        m = u.get("message") or {}
        chat, text = (m.get("chat") or {}).get("id"), (m.get("text") or "").strip()
        if not chat or not text.startswith("/") or time.time() - m.get("date", 0) > 300:
            return
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.split("@")[0].lower(), arg.strip()
        if cmd == "/pair":
            return self.try_pair(m, arg)
        if chat != self.chat_id():
            return  # not paired: stay silent
        handlers = {"/start": lambda a: HELP, "/help": lambda a: HELP, "/status": self.cmd_status,
                    "/wan": self.cmd_wan, "/dns": self.cmd_dns, "/optics": self.cmd_optics,
                    "/devices": self.cmd_devices, "/problems": self.cmd_problems,
                    "/backups": lambda a: self.backups.overview(self.noc.SITE_NAME)}
        if cmd == "/restart":
            return self.cmd_restart(chat, arg)
        if cmd == "/diagnose":
            return self.cmd_diagnose(chat, arg)
        if cmd == "/pause":
            if not arg.isdigit():
                return self.send(chat, "Usage: /pause &lt;minutes&gt;  (0 = until /resume)")
            return self.propose(chat, "dns_blocking", {"enable": False, "minutes": int(arg)})
        if cmd == "/resume":
            return self.propose(chat, "dns_blocking", {"enable": True})
        h = handlers.get(cmd)
        try:
            reply = h(arg) if h else "Unknown command.\n\n" + HELP
        except Exception as ex:
            reply = f"Could not read that: {e(str(ex))}"
        self.send(chat, reply)

    def try_pair(self, m, code):
        chat = m["chat"]["id"]
        p = self.pairing
        if (m["chat"].get("type") != "private" or not p or time.time() > p[1]
                or not secrets.compare_digest(code, p[0])):
            return self.send(chat, "That pairing code is wrong or expired. Get a new one from the NOC page (Alerts tab).")
        self.pairing = None
        self.noc.C.setdefault("telegram", {})["chat_id"] = chat
        self.noc.save_config()
        who = m.get("from", {}).get("username") or m.get("from", {}).get("first_name", "")
        self.noc.audit_entry("telegram", f"chat {chat}", "telegram_pair", f"Paired Telegram chat ({who})", {"ok": True})
        self.send(chat, "✅ Paired. Alerts from the NOC will come here.\n\n" + HELP)

    # ---- read commands
    def overview(self):
        noc = self.noc
        r = noc.cached("router", 15, noc.collect_router)
        return r if isinstance(r, dict) and "error" not in r else None

    def cmd_status(self, _):
        noc = self.noc
        lines = [f"<b>{e(noc.SITE_NAME)}</b> · {time.strftime('%H:%M')}"]
        r = self.overview()
        if r:
            wans = sorted(noc.wan_ifaces())
            off = [w for w in wans if (r.get("mwan") or {}).get(w, {}).get("status") != "online"]
            lines.append(f"🌐 Internet {len(wans) - len(off)}/{len(wans)} online" + (f" · offline: {e(', '.join(off))}" if off else ""))
            for o in r.get("onts", []):
                lines.append(f"💡 {e(o['name'])}: RX {o.get('rx_dbm')} dBm, {o.get('temp_c')} °C")
        else:
            lines.append("⚠️ Router not reachable over SSH")
        devs = self.devices()
        down = [d["name"] for d in devs if not d["up"]]
        lines.append(f"🖥 Devices {len(devs) - len(down)}/{len(devs)} up" + (f" · down: {e(', '.join(down))}" if down else ""))
        d = noc.cached("dns", 10, noc.collect_dns)
        blk = {n["name"]: n.get("blocking") for n in d.get("nodes", []) if n.get("ok")}
        lines.append(f"🛡 DNS VIP on {e(str(d.get('vip_owner')))} · blocking: " +
                     (", ".join(f"{e(k)} {e(str(v))}" for k, v in blk.items()) or "no Pi-hole answering"))
        p = self.problems()
        lines.append(f"\n{len(p)} problem(s) open" + (" – /problems" if p else " ✅"))
        return "\n".join(lines)

    def cmd_problems(self, _):
        p = self.problems()
        return "Nothing is wrong right now ✅" if not p else "<b>Not OK</b>\n" + "\n".join(
            f"• {e(x['text'])} <i>({ago(x['for_s'])})</i>" + (f"\n  ↳ {e(x['cause'])}" if x.get("cause") else "") for x in p)

    def cmd_wan(self, _):
        noc, r = self.noc, self.overview()
        if not r:
            return "Router not reachable over SSH."
        ifs = {i["name"]: i for i in r.get("interfaces", [])}
        lines = ["<b>Internet links</b>"]
        for w in sorted(noc.wan_ifaces()):
            m, i = (r.get("mwan") or {}).get(w, {}), ifs.get(w, {})
            ok = m.get("status") == "online"
            lines.append(f"{'🟢' if ok else '🔴'} <b>{e(w)}</b> {e(m.get('status') or ('up' if i.get('up') else 'down'))}"
                         f" · {e((i.get('ip') or '–').split('/')[0])}" + (f" · {m.get('latency')} ms" if m.get("latency") is not None else "")
                         + ("" if ok else f" → /diagnose {e(w)}"))
        groups = noc.wan_groups()
        if groups:
            lines.append("\n<b>Groups</b> (/restart &lt;group&gt;)")
            lines += [f"• {e(g)}: {e(', '.join(m))}" for g, m in groups.items()]
        return "\n".join(lines)

    def cmd_dns(self, _):
        d = self.noc.cached("dns", 10, self.noc.collect_dns)
        lines = [f"<b>DNS</b> · VIP {e(str(d.get('vip')))} on <b>{e(str(d.get('vip_owner')))}</b>"]
        for n in d.get("nodes", []):
            if n.get("ok"):
                lines.append(f"{'🟢' if n.get('blocking') == 'enabled' else '🟠'} {e(n['name'])}: blocking {e(str(n.get('blocking')))}"
                             f" · {n.get('queries') or 0:,} queries, {n.get('percent')}% blocked")
            else:
                lines.append(f"🔴 {e(n['name'])}: {e(str(n.get('error')))}")
        return "\n".join(lines)

    def cmd_optics(self, _):
        r = self.overview()
        if not r:
            return "Router not reachable over SSH."
        lines = ["<b>Fibre</b>"]
        for o in r.get("onts", []):
            lim = o.get("limits") or {}
            lines.append(f"<b>{e(o['name'])}</b> · {e(o.get('state') or '?')}\n"
                         f"  RX {o.get('rx_dbm')} dBm (normal {lim.get('rx_pwr_low_warn')} to {lim.get('rx_pwr_high_warn')})\n"
                         f"  TX {o.get('tx_dbm')} dBm · {o.get('temp_c')} °C · reading {ago(o['age_s']) if o.get('age_s') is not None else '?'} old")
        return "\n".join(lines) if len(lines) > 1 else "No ONTs configured."

    def cmd_devices(self, arg):
        devs = self.devices()
        show = devs if arg.lower() == "all" else [d for d in devs if not d["up"]]
        if not show:
            return f"All {len(devs)} devices are up ✅ (/devices all to list them)"
        return "\n".join(f"{'🟢' if d['up'] else '🔴'} {e(d['name'])} · {e(d['ip'])}" + (f" · {d['ms']} ms" if d.get("ms") else "")
                         for d in show)

    def cmd_diagnose(self, chat, arg):
        noc, a = self.noc, arg.strip()
        groups = {g.lower(): m for g, m in noc.wan_groups().items()}
        targets = groups.get(a.lower()) or ([a] if a in noc.wan_ifaces() else None)
        if not targets:
            opts = ", ".join(list(noc.wan_groups()) + sorted(noc.wan_ifaces()))
            return self.send(chat, f"Usage: /diagnose &lt;link or group&gt;\nAvailable: {e(opts)}")
        self.send(chat, f"🔎 Checking {e(', '.join(targets))}…")
        res = self.run_diagnoses(targets)
        if len(res) > 1 and len({d["verdict"] for d in res.values()}) == 1:
            first = next(iter(res.values()))
            self.send(chat, self.format_diag({**first, "iface": ", ".join(res)}))
        else:
            for d in res.values():
                self.send(chat, self.format_diag(d))

    # ---- actions
    def cmd_restart(self, chat, arg):
        noc, a = self.noc, arg.strip()
        groups = {g.lower(): g for g in noc.wan_groups()}
        if a.lower() in groups:
            return self.propose(chat, "wan_group_restart", {"group": groups[a.lower()]})
        if a in noc.wan_ifaces():
            return self.propose(chat, "wan_restart", {"iface": a})
        opts = ", ".join(list(noc.wan_groups()) + sorted(noc.wan_ifaces()))
        self.send(chat, f"Usage: /restart &lt;link or group&gt;\nAvailable: {e(opts)}")

    def propose(self, chat, kind, params):
        try:
            summary, detail, word, fn, refresh = self.noc.plan_action(kind, params)
        except Exception as ex:
            return self.send(chat, f"Can't do that: {e(str(ex))}")
        if word:  # protected actions need the typed confirmation of the web UI
            return self.send(chat, "That change is protected; do it from the NOC page.")
        now = time.time()
        for k in [k for k, v in self.pending.items() if v["exp"] < now]:
            self.pending.pop(k)
        aid = secrets.token_hex(8)
        self.pending[aid] = {"kind": kind, "summary": summary, "detail": detail, "fn": fn, "refresh": refresh,
                             "exp": now + ACTION_TTL, "chat": chat}
        text = f"<b>{e(summary)}</b>\n" + "\n".join(f"• {e(d)}" for d in detail) + f"\n\n<i>Expires in {ACTION_TTL // 60} min.</i>"
        self.send(chat, text, reply_markup={"inline_keyboard": [[
            {"text": "✅ Confirm", "callback_data": f"ok:{aid}"}, {"text": "✖ Cancel", "callback_data": f"no:{aid}"}]]})

    def on_button(self, q):
        msg = q.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        verb, _, aid = (q.get("data") or "").partition(":")
        if chat != self.chat_id():
            return self.api("answerCallbackQuery", callback_query_id=q["id"], text="Not authorised")
        a = self.pending.pop(aid, None)
        edit = lambda t: self.api("editMessageText", chat_id=chat, message_id=msg.get("message_id"), text=t, parse_mode="HTML")
        if not a or a["exp"] < time.time() or a["chat"] != chat:
            self.api("answerCallbackQuery", callback_query_id=q["id"], text="Expired: send the command again")
            return edit("<s>" + e((msg.get("text") or "").split("\n")[0]) + "</s>\nExpired.")
        if verb != "ok":
            self.api("answerCallbackQuery", callback_query_id=q["id"], text="Cancelled")
            return edit(f"<s>{e(a['summary'])}</s>\nCancelled.")
        self.api("answerCallbackQuery", callback_query_id=q["id"], text="Running…")
        edit(f"⏳ {e(a['summary'])}…")
        who = f"telegram chat {chat}"
        try:
            result = a["fn"]()
            self.noc.invalidate(*a["refresh"])
            self.noc.audit_entry("telegram", who, a["kind"], a["summary"] + " | " + "; ".join(a["detail"]),
                                 {"ok": True, "result": result})
            edit(f"✅ <b>Done:</b> {e(a['summary'])}")
        except Exception as ex:
            self.noc.audit_entry("telegram", who, a["kind"], a["summary"], {"ok": False, "error": str(ex)})
            edit(f"❌ <b>Failed:</b> {e(a['summary'])}\n{e(str(ex))}")

    # ------------------------------------------------------------ lifecycle / UI
    def start(self):
        if self.started:
            return
        self.started = True
        threading.Thread(target=self.watch_loop, name="noc-watch", daemon=True).start()
        threading.Thread(target=self.bot_loop, name="noc-bot", daemon=True).start()

    def status(self):
        recent = self.noc.tail_jsonl(self.log_path, 50)
        return {"token_set": bool(self.token()), "paired": bool(self.chat_id()), "bot": self.bot_name,
                "running": self.started, "last_check": self.last_check, "checks": len(self.states),
                "watch_error": self.watch_error, "bot_error": self.bot_error, "backup_error": self.backup_error,
                "outbox": len(self.outbox),
                "settings": self.cfg() | {"chat_id": None}, "problems": self.problems(), "recent": recent}
