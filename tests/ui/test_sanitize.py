"""Data-driven unit tests for the fixture sanitiser (tests/ui/sanitize.py).

All inputs here are invented; none of them belong to a real network.
"""
import json
import sys
import unittest
from pathlib import Path

from ddt import data, ddt, unpack

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sanitize as S  # noqa: E402

SAMPLE = Path(__file__).resolve().parent / "fixtures.sample.json"
WORDS = {"acmenet": "metro", "fastfibre": "skyline", "bobs-laptop": "laptop-1"}


@ddt
class IPv4(unittest.TestCase):
    SHARED = S.Sanitizer()

    @data(
        ("10.0.0.1", "192.0.2.1"),              # first /24 seen -> first documentation net
        ("10.0.0.250", "192.0.2.250"),          # same /24 keeps the same replacement
        ("10.0.7.1", "198.51.100.1"),           # next /24 -> next net
        ("100.64.3.9", "203.0.113.9"),
        ("172.16.5.4", "198.18.0.4"),           # pool continues in RFC 2544 space
    )
    @unpack
    def test_mapping_is_ordered_and_stable(self, src, want):
        # one Sanitizer for the whole table, as for one capture (cases run in order)
        self.assertEqual(self.SHARED.ipv4(src), want)

    @data("0.0.0.0", "1.1.1.1", "8.8.8.8", "4.2.2.2", "127.0.0.1", "255.255.255.0", "224.0.0.251")
    def test_kept(self, addr):
        self.assertEqual(S.Sanitizer().ipv4(addr), addr)

    @data("999.1.1.1", "1.2.3.400")
    def test_not_an_address_is_untouched(self, s):
        self.assertEqual(S.Sanitizer().ipv4(s), s)

    @data(("10.20.{n}.1/24", "198.18.{n}.1/24"), ("172.16.{n}.254", "198.18.{n}.254"), ("v{n}.2", "v{n}.2"))
    @unpack
    def test_subnet_template(self, src, want):
        self.assertEqual(S.Sanitizer().text(src), want)

    @data(("10.0.0.5/24", "192.0.2.5/24"), ("10.0.0.5:8080", "192.0.2.5:8080"), ("via 10.0.0.5, up", "via 192.0.2.5, up"))
    @unpack
    def test_inside_text(self, src, want):
        self.assertEqual(S.Sanitizer().text(src), want)


@ddt
class IPv6(unittest.TestCase):
    @data(
        ("2400:1111:2222:3333::44:5555/128", "2001:db8:"),
        ("2a00:aaaa:bbbb:cccc::/64", "2001:db8:"),
        ("fd12:3456:789a::10", "fd00:db8:"),
        ("fe80::21a:2bff:fe3c:4d5e", "fe80::"),
    )
    @unpack
    def test_prefix_replaced(self, src, want_prefix):
        out = S.Sanitizer().text(src)
        self.assertTrue(out.startswith(want_prefix), out)
        self.assertNotIn(src.split("/")[0], out)

    @data("fe80::21a:2bff:fe3c:4d5e", "2400:1111:2222:3333::44:5555")
    def test_interface_id_rehashed(self, src):
        # SLAAC interface IDs embed the MAC: the last group must change too
        self.assertNotEqual(src.split(":")[-1], S.Sanitizer().text(src).split(":")[-1])

    @data("2400:1111:2222:3333::44:5555/128", "2400:1111:2222:3333:4444:555:6666:7777")
    def test_length_close_to_original(self, src):
        self.assertLessEqual(abs(len(S.Sanitizer().text(src)) - len(src)), 3)

    @data("::/0", "::", "::1", "12:30:05", "00:40:44", "Uptime 1:02:03")
    def test_untouched(self, s):
        self.assertEqual(S.Sanitizer().text(s), s)

    def test_same_address_same_result(self):
        s = S.Sanitizer()
        self.assertEqual(s.text("2400:1111:2222::1"), s.text("2400:1111:2222::1"))


@ddt
class MAC(unittest.TestCase):
    @data(
        ("aa:bb:cc:11:22:33", "00:00:5e:00:53:01"),
        ("AA-BB-CC-11-22-33", "00-00-5E-00-53-01"),     # separator and case kept
        ("AA:BB:CC:11:22:33", "00:00:5E:00:53:01"),
    )
    @unpack
    def test_documentation_range(self, src, want):
        self.assertEqual(S.Sanitizer().text(src), want)

    def test_same_mac_any_spelling_same_result(self):
        s = S.Sanitizer()
        self.assertEqual(s.text("aa:bb:cc:dd:ee:ff"), s.text("AA:BB:CC:DD:EE:FF").lower())
        self.assertNotEqual(s.text("aa:bb:cc:dd:ee:ff"), s.text("aa:bb:cc:dd:ee:fe"))


@ddt
class OtherText(unittest.TestCase):
    @data(
        ("0123456789abcdef0123456789abcdef", 32),       # controller ID
        ("abcdef0123456789abcdef01", 24),               # object ID
        ("00030001aabbcc112233", 20),                   # DHCPv6 client ID (contains a MAC)
    )
    @unpack
    def test_hex_ids_rehashed_same_length(self, src, n):
        out = S.Sanitizer().text(src)
        self.assertNotEqual(out, src)
        self.assertEqual(len(out), n)

    @data("bond.1015", "20260509", "V1.1.8", "wan1_6", "HE160", "1000F", "deadbeef")
    def test_ordinary_tokens(self, s):
        # 'deadbeef' has no digit, so it is a word, not an ID
        self.assertEqual(S.Sanitizer().text(s), s)

    @data(
        ("someone_wifi@isp.example.in", "user@example.net"),
        ("login a.b@c.co.uk now", "login user@example.net now"),
    )
    @unpack
    def test_email(self, src, want):
        self.assertEqual(S.Sanitizer().text(src), want)

    @data(
        ("chat 1234567890", "chat 100200300"),
        ("chat -1001234567890", "chat 100200300"),
        ("Paired Telegram chat (Some_User_123)", "Paired Telegram chat (noc_operator)"),
        ("chat 42", "chat 42"),                      # short numbers are not chat IDs
    )
    @unpack
    def test_telegram_identity(self, src, want):
        self.assertEqual(S.Sanitizer().text(src), want)

    @data(
        ("AcmeNet", "Metro"), ("acmenet-v6", "metro-v6"), ("FASTFIBRE", "SKYLINE"),
        ("acmenet_6", "metro_6"), ("ONT (AcmeNet)", "ONT (Metro)"), ("Bobs-Laptop", "Laptop-1"),
    )
    @unpack
    def test_words(self, src, want):
        self.assertEqual(S.Sanitizer(WORDS).text(src), want)

    @data("acmenetwork", "subacmenet")              # only whole words
    def test_words_only_whole(self, s):
        self.assertEqual(S.Sanitizer(WORDS).text(s), s)

    @data("telemetry.sdk.tracker.example.org", "a.b.example.org", "f" * 56)
    def test_domains_same_length(self, d):
        out = S.Sanitizer().text(d, key="domain")
        self.assertTrue(out.endswith(".example.com"))
        self.assertEqual(len(out), max(len(d), len("dd.example.com")))


@ddt
class Leaks(unittest.TestCase):
    @data("10.0.0.1", "100.64.0.1", "2400:1111:2222::1", "aa:bb:cc:dd:ee:ff", "bob@isp.example.in", "fd12:3456::1")
    def test_real_looking_values_reported(self, value):
        self.assertTrue(S.leaks({"x": value}), value)

    @data("192.0.2.1", "198.51.100.7/24", "203.0.113.9:80", "198.19.4.4", "2001:db8:1::5", "fd00:db8:1::5",
          "fe80::1:2", "00:00:5e:00:53:0a", "user@example.net", "8.8.8.8", "::/0", "12:30:05")
    def test_documentation_values_pass(self, value):
        self.assertEqual(S.leaks({"x": value}), [])

    @data(("secret-host", [r"secret-\w+"], (), "secret-host"), ("hello acmenet", (), ("acmenet",), "acmenet"))
    @unpack
    def test_patterns_and_words(self, value, patterns, words, hit):
        self.assertEqual(S.leaks({"x": value}, patterns=patterns, words=words), [hit])

    def test_keys_are_checked(self):
        self.assertEqual(S.leaks({"10.0.0.1": 1}), ["10.0.0.1"])

    def test_original_id_still_present(self):
        original = {"id": "0123456789abcdef0123"}
        self.assertEqual(S.leaks({"id": "0123456789abcdef0123"}, original), ["0123456789abcdef0123"])


class Documents(unittest.TestCase):
    CAPTURE = {
        "acmenet": ["wan1", "wan2"],
        "devices": [{"name": "Pi (AcmeNet)", "ip": "10.0.0.9", "mac": "aa:bb:cc:35:87:96", "up": True, "ms": 1.5}],
        "top": [{"domain": "ads.tracker.example.org", "count": 3}],
        "ip6": "2400:1111:2222:3333::44:5555/128",
        "controller": "0123456789abcdef0123456789abcdef",
    }

    def test_keys_and_values_rewritten_types_kept(self):
        out = S.sanitize(self.CAPTURE, WORDS)
        self.assertIn("metro", out)
        dev = out["devices"][0]
        self.assertEqual((dev["up"], dev["ms"], dev["name"]), (True, 1.5, "Pi (Metro)"))

    def test_no_leaks_after_sanitising(self):
        self.assertEqual(S.leaks(S.sanitize(self.CAPTURE, WORDS), self.CAPTURE, words=WORDS), [])


class CommittedSample(unittest.TestCase):
    """The committed sample must hold only documentation data."""

    @classmethod
    def setUpClass(cls):
        cls.sample = json.loads(SAMPLE.read_text(encoding="utf-8"))

    def test_sample_is_documentation_data_only(self):
        self.assertEqual(S.leaks(self.sample), [])

    def test_sample_has_every_endpoint(self):
        for k in ("overview", "router", "ap", "dns", "switch", "proxmox", "devices", "openwrt",
                  "settings", "alerts", "audit", "meta"):
            self.assertIn(k, self.sample)

    def test_local_words_and_capture_when_present(self):
        words, patterns = S.load_local()
        real = Path(__file__).resolve().parents[1] / "fixtures.json"
        original = json.loads(real.read_text(encoding="utf-8")) if real.exists() else None
        if not words and original is None:
            self.skipTest("no local word list or capture on this machine")
        self.assertEqual(S.leaks(self.sample, original, patterns, words), [])


if __name__ == "__main__":
    unittest.main()
