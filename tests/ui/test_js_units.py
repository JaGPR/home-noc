"""Data-driven unit tests of the page's JavaScript functions, run in a real browser."""
import sys
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402

SRV = PAGE = None


def setUpModule():
    global SRV, PAGE
    SRV = H.FixtureServer().__enter__()
    try:
        PAGE = H.open_page(SRV.url, 375, 740)
    except unittest.SkipTest:
        SRV.__exit__(None, None, None)
        raise


def tearDownModule():
    if PAGE:
        PAGE.context.close()
    H.close_browser()
    SRV.__exit__(None, None, None)


def js(expr, arg=None):
    return PAGE.evaluate(expr, arg)


@ddt
class Helpers(unittest.TestCase):
    @data(
        ("192.0.2.5", ["192.0.2.5", ""]),
        ("192.0.2.5:443", ["192.0.2.5", "443"]),
        ("2001:db8::5", ["2001:db8::5", ""]),
        ("[2001:db8::5]:443", ["2001:db8::5", "443"]),
        ("[2001:db8::5]", ["2001:db8::5", ""]),
        ("", ["", ""]),
        (None, ["", ""]),
    )
    @unpack
    def test_splitTarget(self, target, want):
        self.assertEqual(js("t => splitTarget(t)", target), want)

    @data(
        ("192.0.2.1", "192.0.2.1"),
        ("2001:db8::1", "2001:db8::1"),
        ("2001:db8::1/64", "2001:db8::1"),
        ("2001:db8:864a:16f0::844:43a3", "2001:db8…::844:43a3"),
        ("2001:db8:c4ac:f492:e121:485:4a9f:634b", "2001:db8…4a9f:634b"),
        (None, ""),
    )
    @unpack
    def test_shortAddr(self, addr, want):
        self.assertEqual(js("a => shortAddr(a)", addr), want)

    @data(("192.0.2.1", True), ("2001:db8::1", True), ("fe80::1", True), ("host", False),
          ("1.2.3", False), ("abc:def", False), ("", False))
    @unpack
    def test_isIP(self, value, want):
        self.assertIs(js("v => isIP(v)", value), want)

    @data(("<a href=\"x\">&'", "&lt;a href=&quot;x&quot;&gt;&amp;&#39;"), (None, ""), ("plain", "plain"))
    @unpack
    def test_esc(self, value, want):
        self.assertEqual(js("v => esc(v)", value), want)

    def test_esc_number(self):
        self.assertEqual(js("esc(5)"), "5")

    @data((None, "–"), (59, "0m"), (3600, "1h 0m"), (7260, "2h 1m"), (90061, "1d 1h"))
    @unpack
    def test_dur(self, seconds, want):
        self.assertEqual(js("s => dur(s)", seconds), want)

    @data((None, "–"), (0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"), (1536, "1.5 KB"),
          (1073741824, "1.0 GB"), (2 * 1024 ** 5, "2048.0 TB"))
    @unpack
    def test_bytes(self, b, want):
        self.assertEqual(js("b => bytes(b)", b), want)


@ddt
class Navigation(unittest.TestCase):
    @data(("overview", "overview"), ("router", "router"), ("switch", "switch"), ("alerts", "alerts"),
          ("dns", "more"), ("interfaces", "more"), ("settings", "more"), ("audit", "more"))
    @unpack
    def test_navTarget(self, tab, want):
        self.assertEqual(js("t => navTarget(t)", tab), want)

    @data(*H.TABS)
    def test_syncNav_marks_exactly_one_button(self, tab):
        on = js("""t => { syncNav(t); return [...document.querySelectorAll('#bottombar button')]
                  .filter(b => b.classList.contains('on')).map(b => [b.dataset.go || 'more', b.getAttribute('aria-current')]); }""", tab)
        self.assertEqual(on, [[js("t => navTarget(t)", tab), "page"]])

    def tearDown(self):
        js("syncNav(tab)")      # leave the page as it was


# A table built from HTML rows: (rows html, cardify result, labels per body row)
CARDIFY = [
    ("<tr><th>Name</th><th>IP</th></tr><tr><td>a</td><td>b</td></tr>", True, [["Name", "IP"]]),
    ("<tr><th>A</th><th>B</th><th>C</th></tr><tr><td colspan=\"3\">none</td></tr>", True, [[""]]),
    ("<tr><th colspan=\"2\">X</th><th>Y</th></tr><tr><td>1</td><td>2</td><td>3</td></tr>", True, [["X", "X", "Y"]]),
    ("<tr><th>A</th></tr><tr><td>1</td><td>2</td></tr>", True, [["A", ""]]),
    ("<tr><th> Spaced </th></tr><tr><td>1</td></tr><tr><td>2</td></tr>", True, [["Spaced"], ["Spaced"]]),
    ("<tr><th></th><th>B</th></tr><tr><td><button>x</button></td><td>2</td></tr>", True, [["", "B"]]),
    ("<tr><td>no header</td></tr>", False, [[None]]),
    ("<tr><th>K</th><td>key-value row</td></tr>", False, [[None]]),
]

CARDIFY_JS = """
html => {
  const t = document.createElement('table'); t.innerHTML = html; document.body.append(t);
  const ok = cardify(t);
  const rows = [...t.rows].filter(r => !r.classList.contains('thead-row') && [...r.cells].some(c => c.tagName === 'TD'));
  const labels = rows.map(r => [...r.cells].filter(c => c.tagName === 'TD').map(c => c.dataset.label ?? null));
  const again = (cardify(t), rows.map(r => [...r.cells].filter(c => c.tagName === 'TD').map(c => c.dataset.label ?? null)));
  const res = {ok, labels, again, cls: t.classList.contains('cards'), heads: t.querySelectorAll('tr.thead-row').length};
  t.remove(); return res;
}
"""


@ddt
class Cardify(unittest.TestCase):
    @data(
        ("<tr><th>A</th><th colspan=\"2\">B</th><th></th></tr>", ["A", "B", "B", ""]),
        ("<tr><th>One</th></tr>", ["One"]),
        ("<tr><td>x</td><td colspan=\"3\">y</td></tr>", ["x", "y", "y", "y"]),
    )
    @unpack
    def test_colLabels(self, row_html, want):
        got = js("""h => { const t = document.createElement('table'); t.innerHTML = h;
                    const r = colLabels(t.rows[0]); return r; }""", row_html)
        self.assertEqual(got, want)

    @data(*CARDIFY)
    @unpack
    def test_cardify(self, html, ok, labels):
        r = js(CARDIFY_JS, html)
        self.assertIs(r["ok"], ok)
        self.assertEqual(r["labels"], labels)
        self.assertEqual(r["again"], labels, "cardify must be idempotent")
        self.assertIs(r["cls"], ok)
        self.assertEqual(r["heads"], 1 if ok else 0)

    def test_view_tables_are_labelled_automatically(self):
        # the MutationObserver labels tables drawn into #view at any time
        got = js("""async () => { const v = document.querySelector('#view'), before = v.innerHTML;
          v.insertAdjacentHTML('beforeend', '<table id="t-auto"><tr><th>Col</th></tr><tr><td>1</td></tr></table>');
          await new Promise(r => setTimeout(r, 0));
          const t = document.querySelector('#t-auto'), res = [t.classList.contains('cards'), t.rows[1].cells[0].dataset.label];
          v.innerHTML = before; return res; }""")
        self.assertEqual(got, [True, "Col"])

    def test_opt_out(self):
        got = js("""async () => { const v = document.querySelector('#view'), before = v.innerHTML;
          v.insertAdjacentHTML('beforeend', '<table id="t-no" class="no-cards"><tr><th>Col</th></tr><tr><td>1</td></tr></table>');
          await new Promise(r => setTimeout(r, 0));
          const res = document.querySelector('#t-no').classList.contains('cards'); v.innerHTML = before; return res; }""")
        self.assertFalse(got)


@ddt
class Drawer(unittest.TestCase):
    STATE = """() => ({open: document.body.classList.contains('nav-open'), inert: document.querySelector('#side').inert,
      menu: document.querySelector('#menu').getAttribute('aria-expanded'), more: document.querySelector('#more').getAttribute('aria-expanded'),
      focus: document.activeElement && (document.activeElement.id || document.activeElement.dataset.t || document.activeElement.tagName)})"""

    def tearDown(self):
        js("closeNav(false)")

    def test_open(self):
        js("openNav()")
        s = js(self.STATE)
        self.assertEqual((s["open"], s["inert"], s["menu"], s["more"]), (True, False, "true", "true"))
        self.assertEqual(s["focus"], js("tab"), "focus moves to the current section in the drawer")

    @data((True, "menu"), (False, None))
    @unpack
    def test_close_and_focus(self, restore, focus):
        js("openNav()")
        js("r => { document.activeElement.blur(); closeNav(r); }", restore)
        s = js(self.STATE)
        self.assertEqual((s["open"], s["inert"], s["menu"], s["more"]), (False, True, "false", "false"))
        if focus:
            self.assertEqual(s["focus"], focus)

    def test_close_when_closed_is_a_no_op(self):
        js("document.querySelector('#refresh').focus(); closeNav(true)")
        self.assertEqual(js(self.STATE)["focus"], "refresh")


if __name__ == "__main__":
    unittest.main()
