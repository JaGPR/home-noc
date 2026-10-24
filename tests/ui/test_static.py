"""Data-driven checks of the page source for the mobile support (no browser needed)."""
import re
import sys
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402

INDEX = (H.STATIC / "index.html").read_text(encoding="utf-8")
LOGIN = (H.STATIC / "login.html").read_text(encoding="utf-8")
PAGES = {"index": INDEX, "login": LOGIN}


def meta(html, name):
    m = re.search(rf'<meta name="{re.escape(name)}" content="([^"]*)"', html)
    return m.group(1) if m else None


@ddt
class Head(unittest.TestCase):
    @data(*PAGES)
    def test_viewport_fits_notched_screens(self, page):
        v = meta(PAGES[page], "viewport")
        self.assertIn("width=device-width", v)
        self.assertIn("viewport-fit=cover", v)
        self.assertNotIn("user-scalable=no", v)          # pinch zoom must stay possible
        self.assertNotIn("maximum-scale", v)

    @data(*PAGES)
    def test_theme_color_for_both_schemes(self, page):
        found = re.findall(r'<meta name="theme-color" content="#[0-9a-f]{6}" media="\(prefers-color-scheme: (light|dark)\)">', PAGES[page])
        self.assertEqual(sorted(found), ["dark", "light"])

    @data(("mobile-web-app-capable", "yes"), ("apple-mobile-web-app-capable", "yes"), ("apple-mobile-web-app-title", "NOC"))
    @unpack
    def test_home_screen_meta(self, name, value):
        self.assertEqual(meta(INDEX, name), value)


@ddt
class Login(unittest.TestCase):
    @data(('autocapitalize', 'none'), ('autocorrect', 'off'), ('spellcheck', 'false'), ('autocomplete', 'username'))
    @unpack
    def test_user_field_not_changed_by_phone_keyboards(self, attr, value):
        # phone keyboards would otherwise send "Admin" for "admin"
        tag = re.search(r'<input id="u"[^>]*>', LOGIN).group(0)
        self.assertIn(f'{attr}="{value}"', tag)

    def test_code_field_uses_number_keyboard(self):
        self.assertRegex(LOGIN, r'<input id="r-code"[^>]*inputmode="numeric"')


@ddt
class Markup(unittest.TestCase):
    @data("#menu", "#scrim", "#bottombar", "#side", "#more")
    def test_mobile_elements_exist(self, sel):
        self.assertRegex(INDEX, rf'id="{sel[1:]}"')

    def test_bottom_bar_targets_are_real_tabs(self):
        for t in re.findall(r'data-go="([a-z]+)"', INDEX):
            self.assertIn(t, H.TABS)

    @data(('id="menu"', 'aria-controls="side"'), ('id="menu"', 'aria-expanded="false"'), ('id="more"', 'aria-controls="side"'))
    @unpack
    def test_drawer_controls_are_labelled(self, element, attr):
        tag = re.search(rf"<button[^>]*{element}[^>]*>", INDEX).group(0)
        self.assertIn(attr, tag)

    @data("#e7eefc", "#1b2a47")
    def test_no_light_only_colours(self, colour):
        # theme variables, not fixed colours, so dark mode works for every theme choice
        self.assertNotIn(colour, INDEX)

    @data((r"@media \(max-width: 900px\)", 1), (r"@media \(max-width: 600px\)", 1))
    @unpack
    def test_breakpoints(self, pattern, at_least):
        self.assertGreaterEqual(len(re.findall(pattern, INDEX)), at_least)


if __name__ == "__main__":
    unittest.main()
