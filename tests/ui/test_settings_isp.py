"""Data-driven browser tests of the Settings -> ISP boxes card."""
import json
import sys
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402

SRV = None
SAMPLE = json.loads(H.FIXTURES.read_text(encoding="utf-8"))
BOXES = [d for d in SAMPLE["devices"]["devices"] if d["group"] == "isp"]
USED_BY = {}
for g, p in SAMPLE["settings"]["wan"]["paths"].items():
    if p.get("isp_device"):
        USED_BY.setdefault(p["isp_device"], []).append(g)


def setUpModule():
    global SRV
    SRV = H.FixtureServer().__enter__()
    try:
        H.browser()
    except unittest.SkipTest:
        SRV.__exit__(None, None, None)
        raise


def tearDownModule():
    H.close_browser()
    SRV.__exit__(None, None, None)


def rows(page):
    return page.evaluate("""[...document.querySelectorAll('#isp-table tr')].slice(1).map(r => ({
        name: r.cells[0].innerText.trim(), tracked: r.cells[1]?.innerText.trim(),
        status: r.cells[2]?.innerText.trim(), used: r.cells[3]?.innerText.trim(),
        buttons: [...r.querySelectorAll('button')].map(b => b.innerText.trim())}))""")


@ddt
class IspBoxes(unittest.TestCase):
    def setUp(self):
        self.page = H.open_page(SRV.url, 1280, 900, "settings")
        self.posts = []
        self.page.on("request", lambda r: self.posts.append(r.post_data_json) if r.method == "POST" else None)

    def tearDown(self):
        self.page.context.close()

    def test_lists_every_isp_box(self):
        self.assertEqual([r["name"] for r in rows(self.page)], [b["name"] for b in BOXES])

    @data(*BOXES)
    def test_row(self, box):
        r = next(x for x in rows(self.page) if x["name"] == box["name"])
        self.assertEqual(r["tracked"], box.get("target") or "MAC " + box.get("mac", ""))
        self.assertEqual(r["status"], "up" if box.get("up") else "down")
        self.assertEqual(r["used"], " ".join(USED_BY.get(box["name"], [])) or "none")
        self.assertEqual(r["buttons"], ["Edit", "Remove"])

    def test_card_sits_above_wan_groups(self):
        order = self.page.evaluate("[...document.querySelectorAll('#view .card')].map(c => c.id).filter(Boolean)")
        self.assertLess(order.index("isp-card"), order.index("wan-card"))

    def test_add_opens_dialog_in_isp_group(self):
        self.page.click("#isp-add")
        self.page.wait_for_selector("#ddlg[open]")
        self.assertEqual(self.page.input_value("#ddlg-group"), "isp")
        self.assertEqual(self.page.inner_text("#ddlg-title"), "Track a device")
        self.assertEqual(self.page.input_value("#ddlg-name"), "")

    @data(*[b["name"] for b in BOXES])
    def test_edit_opens_dialog_for_box(self, name):
        self.page.click(f"#isp-table button[data-ispedit='{name}']")
        self.page.wait_for_selector("#ddlg[open]")
        self.assertEqual(self.page.inner_text("#ddlg-title"), f"Edit {name}")
        self.assertEqual(self.page.input_value("#ddlg-name"), name)

    @data(*[b["name"] for b in BOXES])
    def test_remove_previews_device_delete(self, name):
        self.page.click(f"#isp-table button[data-ispdel='{name}']")
        self.page.wait_for_selector("#dlg[open]")
        self.assertIn({"kind": "device_delete", "params": {"original_name": name}}, self.posts)

    def test_add_then_preview_sends_isp_group(self):
        self.page.click("#isp-add")
        self.page.fill("#ddlg-name", "Cable modem")
        self.page.select_option("#ddlg-mode", "ip")
        self.page.fill("#ddlg-ip", "192.0.2.60")
        self.page.click("#ddlg-ok")
        self.page.wait_for_selector("#dlg[open]")
        sent = [p for p in self.posts if p and p.get("kind") == "device_add"]
        self.assertEqual(sent[0]["params"]["group"], "isp")
        self.assertEqual(sent[0]["params"]["name"], "Cable modem")


class EmptyState(unittest.TestCase):
    def test_no_boxes_message(self):
        data_ = json.loads(json.dumps(SAMPLE))
        for d in data_["devices"]["devices"]:
            if d["group"] == "isp":
                d["group"] = "other"
        path = Path(H.FIXTURES).with_name("fixtures.noisp.tmp.json")
        path.write_text(json.dumps(data_), encoding="utf-8")
        try:
            with H.FixtureServer(path) as srv:
                page = H.open_page(srv.url, 1280, 900, "settings")
                try:
                    self.assertIn("No ISP boxes yet", page.inner_text("#isp-table"))
                finally:
                    page.context.close()
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
