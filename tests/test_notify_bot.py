"""The Telegram bot's data-used and speed commands (app/notify.py: /usage, /today, /speed, the "/" menu).

Offline: a fake NOC and a fake Telegram API.  Run:  python -m unittest tests/test_notify_bot.py
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import notify  # noqa: E402
import speedtab  # noqa: E402

GB = 1_000_000_000
MONTH, DAY = time.strftime("%Y-%m"), time.strftime("%Y-%m-%d")
YDAY = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
STORE = {"since": 1795000000, "months": {MONTH: {"wan1": [40 * GB, 2 * GB], "wan2": [3 * GB, 0]}},
         "days": {DAY: {"wan1": [1 * GB, 0]}, YDAY: {"wan2": [2 * GB, 0]}},
         "devices": {MONTH: {"aa:00:00:00:00:05": {"ips": ["10.0.0.5"], "w": {"wan1": [30 * GB, 0]}}}}}


class Usage:
    def __init__(self):
        self.rates, self.rates_at, self.error = {"wan1": [80e6, 4e6]}, time.time() - 5, None

    def store_copy(self):
        return json.loads(json.dumps(STORE))


class FakeNoc:
    VAR = Path(tempfile.mkdtemp())
    SITE_NAME = "Home"
    speedtab = speedtab

    def __init__(self):
        self.C = {"telegram": {"chat_id": 42}, "usage": {"caps_gb": {"isp-a": 100}},
                  "devices": [{"name": "Phone", "mac": "aa:00:00:00:00:05"}]}
        self.USAGE = Usage()

    def wan_groups(self):
        return {"isp-a": ["wan1", "wan2"]}

    def cached(self, key, ttl, fn, stale=0):
        return {"interfaces": [], "leases": []}

    def collect_router(self):
        return {}

    def tail_jsonl(self, path, n):   # newest first, like the NOC's
        return [{"mbps": {"wan1": 400}, "source": "hourly"}, {"mbps": {"wan1": 999}, "source": "together"},
                {"mbps": {"wan1": 300, "wan2": 50}, "source": "hourly"}]


def bot():
    tg = notify.Telegram(FakeNoc())
    tg.sent, tg.calls = [], []
    tg.send = lambda chat, text, **kw: tg.sent.append(text)
    return tg


def ask(tg, text, chat=42):
    tg.handle({"message": {"chat": {"id": chat}, "text": text, "date": time.time()}})
    return tg.sent[-1] if tg.sent else None


@ddt
class Commands(unittest.TestCase):
    @data(("/usage", [f"data used in {MONTH} so far", "<b>isp-a</b>: 45.0 GB", "wan1 42.0 GB · wan2 3.0 GB",
                      "isp-a: 45.0 GB of 100 GB (45%)", "• Phone: 30.0 GB"]),
          ("/usage 1999-01", ["No data counted for 1999-01"]),
          ("/usage nope", ["Usage: /usage"]),
          ("/today", ["data used today so far", "1.0 GB"]),
          ("/today yesterday", [f"data used on {YDAY}", "2.0 GB"]),
          ("/speed", ["traffic right now", "wan1: ↓80.0 Mbps ↑4.0 Mbps · tested 400 Mbps", "20% of the tested 400 Mbps"]),
          ("/speed@noc_bot", ["traffic right now"]),
          ("/help", ["/usage", "/today", "/speed"]))
    @unpack
    def test_reply(self, text, want):
        reply = ask(bot(), text)
        for w in want:
            self.assertIn(w, reply)

    def test_old_speed_reading(self):
        tg = bot()
        tg.noc.USAGE.rates_at = time.time() - 600
        self.assertIn("10m old", ask(tg, "/speed"))

    def test_unpaired_chat_gets_nothing(self):
        tg = bot()
        self.assertIsNone(ask(tg, "/usage", chat=7))

    def test_menu_lists_every_command_the_bot_answers(self):
        names = {c for c, _ in notify.COMMANDS}
        self.assertTrue({"usage", "today", "speed", "status", "help"} <= names)
        for c in names - {"help"}:
            self.assertIn("/" + c, notify.HELP)
        for c, d in notify.COMMANDS:
            self.assertTrue(c.islower() and len(c) <= 32 and 3 <= len(d) <= 256, c)


if __name__ == "__main__":
    unittest.main()
