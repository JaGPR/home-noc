"""Run the NOC web UI against fixture data in a real browser.

FixtureServer serves app/static/*.html and answers /api/* from a fixtures file
(tests/ui/fixtures.sample.json by default), so the UI renders exactly as it would
against a live NOC, with no FastAPI, network or credentials.

Browser tests use Playwright. It drives Playwright's own Chromium when installed
(python -m playwright install chromium) and otherwise an installed Chrome or Edge.
browser() raises unittest.SkipTest when none is available.
"""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "static"
FIXTURES = Path(os.environ.get("NOC_UI_FIXTURES", Path(__file__).resolve().parent / "fixtures.sample.json"))

# Phone, tablet and desktop widths the layout must work at (CSS pixels).
PHONES = {"iphone-se-1": (320, 568), "android-small": (360, 740), "iphone-se": (375, 667),
          "iphone-15": (393, 852), "pixel": (412, 915)}
TABLETS = {"ipad-portrait": (768, 1024)}
DESKTOPS = {"laptop": (1280, 800)}
TABS = ["overview", "dns", "switch", "router", "interfaces", "netdevs", "mwan", "wifi", "devices",
        "proxmox", "alerts", "settings", "audit"]

# Answer for the preview/confirm flow, so dialogs can be opened in tests.
PREVIEW = {"id": "test", "summary": "Test change", "detail": ["one line of detail"], "confirm_word": None}


def api_name(path):
    """'/api/openwrt' -> 'openwrt'; anything else -> None."""
    parts = urlsplit(path).path.strip("/").split("/")
    return parts[1] if len(parts) == 2 and parts[0] == "api" and parts[1] else None


class FixtureServer:
    """HTTP server on 127.0.0.1 with a free port; use as a context manager."""

    def __init__(self, fixtures=FIXTURES):
        self.data = json.loads(Path(fixtures).read_text(encoding="utf-8"))
        data = self.data

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body, ctype):
                raw = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                path = urlsplit(self.path).path
                if path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                if path == "/login":
                    return self._send(200, (STATIC / "login.html").read_bytes(), "text/html; charset=utf-8")
                name = api_name(path)
                if name in data:
                    return self._send(200, json.dumps(data[name]), "application/json")
                if name == "diagnose":
                    return self._send(200, json.dumps({"target": "x", "steps": [], "verdict": "ok"}), "application/json")
                self._send(404, json.dumps({"error": "not found"}), "application/json")

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/api/actions/preview":
                    return self._send(200, json.dumps(PREVIEW), "application/json")
                self._send(200, json.dumps({"ok": True, "result": {}}), "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


_PW = None
_BROWSER = None


def browser():
    """A shared Playwright browser (started once per test run)."""
    global _PW, _BROWSER
    if _BROWSER:
        return _BROWSER
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise unittest.SkipTest("playwright is not installed (pip install playwright)")
    _PW = sync_playwright().start()
    for kw in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            _BROWSER = _PW.chromium.launch(**kw)
            return _BROWSER
        except Exception:
            continue
    _PW.stop()
    _PW = None
    raise unittest.SkipTest("no Chromium, Chrome or Edge available for Playwright")


def close_browser():
    global _PW, _BROWSER
    if _BROWSER:
        _BROWSER.close()
    if _PW:
        _PW.stop()
    _PW = _BROWSER = None


def open_page(base_url, width, height, tab="overview", theme=None):
    """A page at the given viewport with the UI loaded and the tab drawn."""
    ctx = browser().new_context(viewport={"width": width, "height": height}, has_touch=width < 768,
                                is_mobile=width < 768, device_scale_factor=2 if width < 768 else 1)
    if theme:
        ctx.add_init_script(f"try {{ localStorage.setItem('noc-theme', '{theme}') }} catch (e) {{}}")
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base_url + "/")
    page.wait_for_function("document.querySelector('#view').children.length > 0")
    if tab != "overview":
        show_tab(page, tab)
    settle(page)
    page.js_errors = errors
    return page


def show_tab(page, tab):
    page.evaluate("t => document.querySelector(`#tabs [data-t='${t}']`).click()", tab)
    page.wait_for_function("t => document.querySelector('#page-title').textContent.length > 0 && "
                           "document.querySelector(`#tabs [data-t='${t}']`).classList.contains('on')", arg=tab)
    settle(page)


def settle(page):
    """Wait until no load is in flight and animations have finished."""
    page.wait_for_function("!document.body.classList.contains('busy')", timeout=10000)
    page.wait_for_timeout(350)


# Elements that stick out of the viewport sideways, ignoring content inside a
# horizontal scroller (that is the intended way to show a wide table on a phone).
OVERFLOW_JS = """
() => {
  const vw = document.documentElement.clientWidth;
  const scrolls = el => { for (let p = el.parentElement; p; p = p.parentElement) {
      const s = getComputedStyle(p); if (/(auto|scroll|hidden)/.test(s.overflowX) && p.scrollWidth > p.clientWidth + 1) return true; }
    return false; };
  const drawerClosed = matchMedia('(max-width: 900px)').matches && !document.body.classList.contains('nav-open');
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('dialog:not([open])') || el.closest('#tabs') || (drawerClosed && el.closest('.side'))) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if ((r.right > vw + 1 || r.left < -1) && !scrolls(el)) {
      out.push({tag: el.tagName.toLowerCase(), cls: el.className && String(el.className).slice(0, 40),
                id: el.id, right: Math.round(r.right), text: (el.textContent || '').trim().slice(0, 40)});
    }
  }
  return {vw, doc: document.documentElement.scrollWidth, offenders: out.slice(0, 12), count: out.length};
}
"""

# Elements that stick out of the card (or open dialog) they sit in, outside any scroller.
CONTAINED_JS = """
(sel) => {
  const scrolls = (el, stop) => { for (let p = el.parentElement; p && p !== stop; p = p.parentElement) {
      const s = getComputedStyle(p); if (/(auto|scroll)/.test(s.overflowX) && p.scrollWidth > p.clientWidth + 1) return true; }
    return false; };
  const out = [];
  for (const box of document.querySelectorAll(sel)) {
    const b = box.getBoundingClientRect();
    for (const el of box.querySelectorAll('*')) {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height || getComputedStyle(el).position === 'fixed') continue;
      if ((r.right > b.right + 1 || r.left < b.left - 1) && !scrolls(el, box))
        out.push({tag: el.tagName.toLowerCase(), cls: String(el.className || '').slice(0, 40), text: (el.textContent || el.value || '').trim().slice(0, 30),
                  over: Math.round(Math.max(r.right - b.right, b.left - r.left))});
    }
  }
  return out.slice(0, 10);
}
"""

# Interactive controls in the main area, header and bottom bar smaller than min px tall or wide.
TOUCH_JS = """
(min) => {
  const out = [];
  const sel = 'button, select, input:not([type=checkbox]):not([type=radio]):not([type=hidden]), .tile, .seg label';
  for (const el of document.querySelectorAll(`#view :is(${sel}), .top :is(${sel}), #bottombar :is(${sel})`)) {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (!r.width || !r.height || s.visibility === 'hidden' || el.closest('.seg') && el.tagName === 'INPUT') continue;
    if (r.height < min || r.width < min)
      out.push({tag: el.tagName.toLowerCase(), cls: String(el.className || '').slice(0, 30), text: (el.textContent || el.value || '').trim().slice(0, 24),
                w: Math.round(r.width), h: Math.round(r.height)});
  }
  return out.slice(0, 10);
}
"""

# Text fields whose font is smaller than 16px (iOS zooms the page into those on focus).
SMALL_FIELDS_JS = """
() => [...document.querySelectorAll('input:not([type=checkbox]):not([type=radio]):not([type=hidden]), select, textarea')]
  .filter(el => el.getBoundingClientRect().width && parseFloat(getComputedStyle(el).fontSize) < 16)
  .map(el => ({id: el.id, type: el.type, size: getComputedStyle(el).fontSize})).slice(0, 10)
"""

# State of every table in the view: card layout and labelling.
TABLES_JS = """
() => [...document.querySelectorAll('#view table')].map(t => {
  const head = t.querySelector('tr.thead-row');
  const cells = [...t.querySelectorAll('tr:not(.thead-row) > td')];
  return {hasHead: [...t.rows].some(r => r.cells.length && [...r.cells].every(c => c.tagName === 'TH')),
          cards: t.classList.contains('cards'), rowDisplay: getComputedStyle(t.rows[t.rows.length - 1]).display,
          headHidden: head ? getComputedStyle(head).display === 'none' : null,
          unlabelled: cells.filter(c => c.dataset.label === undefined).length, cells: cells.length};
})
"""
