"""Data-driven layout and behaviour tests of the NOC UI at phone, tablet and desktop sizes.

Each case renders the real page with fixture data in a browser and measures it.
"""
import sys
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402

SRV = None
PAGES = {}           # (width, height) -> page reused across tabs at that size
ALL_SIZES = {**H.PHONES, **H.TABLETS, **H.DESKTOPS}
TOUCH_MIN = 32       # px; WCAG 2.5.8 asks for 24, Apple and Google suggest 44 for primary controls


def setUpModule():
    global SRV
    SRV = H.FixtureServer().__enter__()
    try:
        H.browser()
    except unittest.SkipTest:
        SRV.__exit__(None, None, None)
        raise


def tearDownModule():
    for p in PAGES.values():
        p.context.close()
    H.close_browser()
    SRV.__exit__(None, None, None)


def page_at(size, tab):
    """The shared page for a viewport size, showing tab."""
    if size not in PAGES:
        PAGES[size] = H.open_page(SRV.url, *size)
    p = PAGES[size]
    p.evaluate("closeNav(false); document.querySelectorAll('dialog[open]').forEach(d => d.close())")
    H.show_tab(p, tab)
    return p


def fresh_page(size, tab="overview"):
    p = H.open_page(SRV.url, *size, tab)
    return p


def wait_drawer(p, open_):
    """Wait for the drawer's slide transition to finish."""
    p.wait_for_function("o => { const r = document.querySelector('#side').getBoundingClientRect();"
                        " return o ? r.left >= -0.5 : r.right <= 0.5; }", arg=open_, timeout=3000)


def cases(sizes, tabs=H.TABS):
    """(size name, (w, h), tab) for every combination."""
    return [(n, s, t) for n, s in sizes.items() for t in tabs]


@ddt
class EveryTab(unittest.TestCase):
    """Every tab at every size: nothing sticks out sideways, no script errors."""

    @data(*cases(ALL_SIZES))
    @unpack
    def test_no_horizontal_overflow(self, name, size, tab):
        r = page_at(size, tab).evaluate(H.OVERFLOW_JS)
        self.assertLessEqual(r["doc"], r["vw"], f"{name}/{tab}: page scrolls sideways")
        self.assertEqual(r["offenders"], [], f"{name}/{tab}")

    @data(*cases(ALL_SIZES))
    @unpack
    def test_content_stays_inside_cards(self, name, size, tab):
        self.assertEqual(page_at(size, tab).evaluate(H.CONTAINED_JS, ".card"), [], f"{name}/{tab}")

    @data(*cases(ALL_SIZES))
    @unpack
    def test_no_script_errors(self, name, size, tab):
        p = page_at(size, tab)
        self.assertEqual(p.js_errors, [], f"{name}/{tab}")
        self.assertNotIn("Failed to load", p.inner_text("#view"))


@ddt
class Phones(unittest.TestCase):
    @data(*cases(H.PHONES))
    @unpack
    def test_touch_targets(self, name, size, tab):
        self.assertEqual(page_at(size, tab).evaluate(H.TOUCH_JS, TOUCH_MIN), [], f"{name}/{tab}")

    @data(*cases(H.PHONES, ["settings", "switch", "devices", "interfaces"]))
    @unpack
    def test_fields_do_not_trigger_ios_zoom(self, name, size, tab):
        self.assertEqual(page_at(size, tab).evaluate(H.SMALL_FIELDS_JS), [], f"{name}/{tab}")

    @data(*cases(H.PHONES))
    @unpack
    def test_tables_are_cards(self, name, size, tab):
        # tables with a header row become cards; short header-less lists (top domains, storage) stay rows
        for t in page_at(size, tab).evaluate(H.TABLES_JS):
            if not t["hasHead"]:
                self.assertFalse(t["cards"], f"{name}/{tab}")
                self.assertEqual(t["rowDisplay"], "table-row")
                continue
            self.assertTrue(t["cards"], f"{name}/{tab}")
            self.assertEqual(t["rowDisplay"], "block")
            self.assertTrue(t["headHidden"])
            self.assertEqual(t["unlabelled"], 0)

    @data(*H.PHONES.items())
    @unpack
    def test_header_is_one_compact_row(self, name, size):
        p = page_at(size, "interfaces")
        box = p.locator(".top").bounding_box()
        self.assertLessEqual(box["height"], 64, name)
        tops = p.evaluate("[...document.querySelectorAll('.top > *')].filter(e => e.getBoundingClientRect().width > 2)"
                          ".map(e => Math.round(e.getBoundingClientRect().top))")
        self.assertLessEqual(max(tops) - min(tops), 12, f"{name}: header items on one line")

    @data(*H.PHONES.items())
    @unpack
    def test_wan_tiles_two_per_row(self, name, size):
        cols = page_at(size, "overview").evaluate(
            "getComputedStyle(document.querySelector('.tiles')).gridTemplateColumns.split(' ').length")
        self.assertEqual(cols, 2, name)

    @data(*H.PHONES.items())
    @unpack
    def test_content_clears_bottom_bar(self, name, size):
        p = page_at(size, "audit")
        p.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        p.wait_for_timeout(100)
        last = p.evaluate("(() => { const c = [...document.querySelectorAll('#view .card')].pop().getBoundingClientRect();"
                          " return c.bottom; })()")
        bar = p.locator("#bottombar").bounding_box()
        self.assertLessEqual(last, bar["y"] + 1, f"{name}: last card hidden behind the bottom bar")
        p.evaluate("window.scrollTo(0, 0)")


@ddt
class MobileNavigation(unittest.TestCase):
    """Drawer and bottom bar at phone and tablet sizes (fresh page per case)."""

    def tearDown(self):
        if getattr(self, "p", None):
            self.p.context.close()

    @data(*{**H.PHONES, **H.TABLETS}.items())
    @unpack
    def test_closed_state(self, name, size):
        self.p = p = fresh_page(size)
        self.assertTrue(p.is_visible("#menu"))
        self.assertTrue(p.is_visible("#bottombar"))
        self.assertTrue(p.evaluate("document.querySelector('#side').inert"), "closed drawer is inert")
        side = p.locator("#side").bounding_box()
        self.assertLessEqual(side["x"] + side["width"], 0, "closed drawer is off screen")
        labels = p.locator("#bottombar button span").all_inner_texts()
        self.assertEqual(labels, ["Overview", "Router", "Switch", "Alerts", "More"])

    @data(*H.PHONES.items())
    @unpack
    def test_drawer_lists_every_section(self, name, size):
        self.p = p = fresh_page(size)
        p.click("#menu")
        wait_drawer(p, open_=True)
        side = p.locator("#side").bounding_box()
        self.assertGreaterEqual(side["x"], -1)
        self.assertLessEqual(side["x"] + side["width"], size[0])
        self.assertEqual(p.get_attribute("#menu", "aria-expanded"), "true")
        for t in H.TABS:
            b = p.locator(f"#tabs [data-t='{t}']")
            b.scroll_into_view_if_needed()
            self.assertTrue(b.is_visible(), t)
            self.assertGreater(len(b.inner_text().strip()), 1, f"{t} has a visible label")
            self.assertGreaterEqual(b.bounding_box()["height"], 44)

    @data(*cases({"iphone-se": H.PHONES["iphone-se"]}, ["dns", "interfaces", "settings", "audit", "router"]))
    @unpack
    def test_choose_section_in_drawer(self, name, size, tab):
        self.p = p = fresh_page(size)
        p.click("#menu")
        p.locator(f"#tabs [data-t='{tab}']").click()
        H.settle(p)
        self.assertFalse(p.evaluate("document.body.classList.contains('nav-open')"), "drawer closes after a choice")
        self.assertEqual(p.inner_text("#page-title"), H_TITLES[tab])
        want = tab if tab in ("overview", "router", "switch", "alerts") else "more"
        on = p.evaluate("[...document.querySelectorAll('#bottombar button.on')].map(b => b.dataset.go || 'more')")
        self.assertEqual(on, [want])

    @data(("Escape key", "key"), ("tap outside", "scrim"), ("menu button", "menu"), ("More button", "more"))
    @unpack
    def test_ways_to_close(self, how, kind):
        self.p = p = fresh_page(H.PHONES["iphone-se"])
        p.click("#menu")
        wait_drawer(p, open_=True)
        if kind == "key":
            p.keyboard.press("Escape")
        elif kind == "scrim":
            p.mouse.click(H.PHONES["iphone-se"][0] - 10, 300)
        else:
            p.evaluate(f"document.querySelector('#{kind}').click()")
        self.assertFalse(p.evaluate("document.body.classList.contains('nav-open')"), how)
        self.assertTrue(p.evaluate("document.querySelector('#side').inert"))
        self.assertEqual(p.get_attribute("#menu", "aria-expanded"), "false")

    def test_escape_returns_focus_to_menu(self):
        self.p = p = fresh_page(H.PHONES["iphone-se"])
        p.click("#menu")
        p.keyboard.press("Escape")
        self.assertEqual(p.evaluate("document.activeElement.id"), "menu")

    @data("overview", "router", "switch", "alerts")
    def test_bottom_bar_navigates(self, tab):
        self.p = p = fresh_page(H.PHONES["iphone-se"], "dns")
        p.click(f"#bottombar [data-go='{tab}']")
        H.settle(p)
        self.assertEqual(p.inner_text("#page-title"), H_TITLES[tab])
        self.assertEqual(p.get_attribute(f"#bottombar [data-go='{tab}']", "aria-current"), "page")

    def test_more_opens_drawer(self):
        self.p = p = fresh_page(H.PHONES["iphone-se"])
        p.click("#more")
        self.assertTrue(p.evaluate("document.body.classList.contains('nav-open')"))
        self.assertEqual(p.get_attribute("#more", "aria-expanded"), "true")

    @data((0, True), (3, False))
    @unpack
    def test_alert_badges(self, problems, hidden):
        self.p = p = fresh_page(H.PHONES["iphone-se"])
        p.evaluate("n => setHealth({problems: Array.from({length: n}, (_, i) => ({text: 'p' + i}))})", problems)
        got = p.evaluate("[...document.querySelectorAll('[data-nb=alerts]')].map(b => [b.hidden, b.textContent])")
        self.assertEqual(len(got), 2, "sidebar and bottom bar both have a badge")
        for h, text in got:
            self.assertIs(h, hidden)
            self.assertEqual(text, str(problems))

    def test_rotating_to_desktop_closes_drawer(self):
        self.p = p = fresh_page(H.PHONES["iphone-se"])
        p.click("#menu")
        p.set_viewport_size({"width": 1280, "height": 800})
        p.wait_for_timeout(300)
        self.assertFalse(p.evaluate("document.body.classList.contains('nav-open')"))
        self.assertFalse(p.evaluate("document.querySelector('#side').inert"), "sidebar usable on desktop")


H_TITLES = {"overview": "Overview", "dns": "DNS", "switch": "Switch", "router": "Router", "interfaces": "Interfaces",
            "netdevs": "Network devices", "mwan": "MultiWAN", "wifi": "Wi-Fi", "devices": "Devices", "proxmox": "Proxmox",
            "alerts": "Alerts", "settings": "Settings", "audit": "Audit log"}


@ddt
class Desktop(unittest.TestCase):
    """The desktop layout is unchanged: sidebar with labels, no mobile controls, tables as tables."""

    @data(*H.DESKTOPS.items())
    @unpack
    def test_sidebar_layout(self, name, size):
        p = page_at(size, "overview")
        self.assertFalse(p.is_visible("#menu"))
        self.assertFalse(p.is_visible("#bottombar"))
        self.assertFalse(p.is_visible("#scrim"))
        self.assertFalse(p.evaluate("document.querySelector('#side').inert"))
        side = p.locator("#side").bounding_box()
        self.assertEqual((side["x"], side["width"]), (0, 232))
        self.assertTrue(p.locator("#tabs [data-t='settings'] span").is_visible())

    @data(*cases({**H.DESKTOPS, **H.TABLETS}, ["switch", "interfaces", "audit", "router"]))
    @unpack
    def test_tables_stay_tables(self, name, size, tab):
        for t in page_at(size, tab).evaluate(H.TABLES_JS):
            self.assertEqual(t["rowDisplay"], "table-row", f"{name}/{tab}")


# (tab to show first, JS that opens the dialog, dialog selector)
DIALOGS = [
    ("overview", "act('test', {})", "#dlg"),
    ("devices", "editDevice(DV.devices[0].name)", "#ddlg"),
    ("devices", "editDevice(null)", "#ddlg"),
    ("switch", "editNetwork(null)", "#ndlg"),
    ("switch", "editVlans('port', SW.ports[0].port)", "#vdlg"),
    ("switch", "editVlans('lag', SW.lags[0].lag)", "#vdlg"),
    ("interfaces", "ifaceDialog(CACHE.get('/api/openwrt'), null)", "#fdlg"),
    ("netdevs", "deviceDialog(CACHE.get('/api/openwrt'), null)", "#fdlg"),
    ("overview", "diagnoseWan('wan1')", "#xdlg"),
]


@ddt
class Dialogs(unittest.TestCase):
    @data(*[(n, s, *d) for n, s in H.PHONES.items() for d in DIALOGS])
    @unpack
    def test_dialog_fits_phone(self, name, size, tab, opener, sel):
        p = page_at(size, tab)
        p.evaluate(opener)
        p.wait_for_selector(f"{sel}[open]")
        p.wait_for_timeout(250)
        try:
            box = p.locator(sel).bounding_box()
            self.assertGreaterEqual(box["x"], -1, f"{name}: {sel} left edge")
            self.assertLessEqual(box["x"] + box["width"], size[0] + 1, f"{name}: {sel} right edge")
            self.assertLessEqual(box["y"] + box["height"], size[1] + 1, f"{name}: {sel} taller than the screen")
            self.assertEqual(p.evaluate(H.CONTAINED_JS, "dialog[open]"), [], f"{name}: {sel} content")
            self.assertEqual(p.evaluate(H.SMALL_FIELDS_JS), [], f"{name}: {sel} fields")
            for b in p.locator(f"{sel} .row button:visible").all():
                b.scroll_into_view_if_needed()
                bb = b.bounding_box()
                self.assertTrue(0 <= bb["y"] and bb["y"] + bb["height"] <= size[1] + 1, f"{name}: {sel} button reachable")
                self.assertGreaterEqual(bb["height"], TOUCH_MIN)
        finally:
            p.evaluate("document.querySelectorAll('dialog[open]').forEach(d => d.close())")


@ddt
class LoginPage(unittest.TestCase):
    @data(*ALL_SIZES.items())
    @unpack
    def test_login_fits(self, name, size):
        ctx = H.browser().new_context(viewport={"width": size[0], "height": size[1]}, is_mobile=size[0] < 768, has_touch=size[0] < 768)
        p = ctx.new_page()
        try:
            p.goto(SRV.url + "/login")
            r = p.evaluate(H.OVERFLOW_JS)
            self.assertLessEqual(r["doc"], r["vw"], name)
            self.assertEqual(r["offenders"], [], name)
            form = p.locator("form").first.bounding_box()
            self.assertLessEqual(form["y"] + form["height"], size[1], f"{name}: sign-in form fits without scrolling")
            if size[0] <= 600:
                self.assertEqual(p.evaluate(H.SMALL_FIELDS_JS), [], name)
                self.assertGreaterEqual(p.locator("button[type=submit]").first.bounding_box()["height"], 44)
        finally:
            ctx.close()


if __name__ == "__main__":
    unittest.main()
