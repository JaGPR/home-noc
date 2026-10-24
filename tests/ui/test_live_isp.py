"""Integration tests of Settings -> ISP boxes on a running NOC (read-only).

Skipped unless NOC_URL and NOC_SESSION (a valid "noc" session cookie) are set, e.g.
  NOC_URL=http://192.0.2.20:8080 NOC_SESSION=... python -m unittest tests/ui/test_live_isp.py
Mint a session on the NOC host with: python3 -c "import app; print(app.make_session(app.E['NOC_ADMIN_USER']))"

Only reads and previews: removal is previewed for every ISP box but never confirmed.
"""
import json
import os
import sys
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402

URL = os.environ.get("NOC_URL", "").rstrip("/")
SID = os.environ.get("NOC_SESSION", "")


def api(path, body=None):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Cookie": f"noc={SID}", "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


@unittest.skipUnless(URL and SID, "set NOC_URL and NOC_SESSION to test a running NOC")
class LiveIspBoxes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.boxes = [d for d in api("/api/devices")["devices"] if d["group"] == "isp"]
        cls.paths = api("/api/settings")["wan"]["paths"]

    def used_by(self, name):
        return sorted(g for g, p in self.paths.items() if (p or {}).get("isp_device") == name)

    def test_card_matches_api(self):
        for w in (1280, 390):
            with self.subTest(width=w):
                ctx = H.browser().new_context(viewport={"width": w, "height": 900}, is_mobile=w < 768, has_touch=w < 768)
                ctx.add_cookies([{"name": "noc", "value": SID, "url": URL}])
                page = ctx.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                try:
                    page.goto(URL + "/")
                    page.wait_for_function("document.querySelector('#view').children.length > 0", timeout=30000)
                    H.show_tab(page, "settings")
                    names = page.evaluate("[...document.querySelectorAll('#isp-table tr')].slice(1).map(r => r.cells[0].innerText.trim())")
                    self.assertEqual(names, [b["name"] for b in self.boxes] or ["No ISP boxes yet. Add the modem, ONT or fibre stick of each ISP to track it."])
                    self.assertEqual(errors, [])
                    over = page.evaluate(H.CONTAINED_JS, "#isp-card")
                    self.assertEqual(over, [], "nothing sticks out of the card")
                finally:
                    ctx.close()

    def test_remove_preview_names_groups(self):
        for b in self.boxes:
            with self.subTest(box=b["name"]):
                p = api("/api/actions/preview", {"kind": "device_delete", "params": {"original_name": b["name"]}})
                self.assertEqual(p["summary"], f"Stop tracking {b['name']}")
                groups = self.used_by(b["name"])
                note = [d for d in p["detail"] if d.startswith("WAN diagnosis no longer checks it")]
                self.assertEqual(note, [f"WAN diagnosis no longer checks it for: {', '.join(groups)} (ISP box cleared)"] if groups else [])

    @classmethod
    def tearDownClass(cls):
        H.close_browser()


if __name__ == "__main__":
    unittest.main()
