"""Turn API responses captured from a live NOC into fixtures that are safe to publish.

The UI tests need realistic data: long IPv6 addresses, dozens of WAN links, long
device names. A capture from a real network has all of that, plus things that must
never reach a public repository. This module rewrites every string in a capture:

  IPv4         -> documentation and benchmarking ranges (RFC 5737, RFC 2544),
                  keeping the host part so subnets still line up
  IPv6         -> 2001:db8::/32 (fd00:db8::/32 for ULA), interface IDs rehashed
                  because SLAAC addresses embed the MAC
  MAC          -> 00:00:5e:00:53:xx (RFC 7042), keeping case and separator
  long hex IDs -> other hex of the same length (controller IDs, DHCP client IDs)
  e-mail       -> user@example.net
  Telegram     -> chat IDs and user names replaced
  DNS domains  -> names under example.com of the same length
  words        -> your own list (ISP names, host names), see LOCAL below

Every mapping is deterministic, so one address maps to the same fake one wherever
it appears. leaks() reports anything that is not documentation data; the command
refuses to write a file while it finds any.

Names that identify you (ISPs, host names) must not be written in this file, which is
public. Put them in tests/ui/sanitize.local.json (git-ignored):
  {"words": {"myisp": "metro", "my-laptop": "laptop-1"},
   "patterns": ["regex that must never appear", "..."]}

Usage:  python tests/ui/sanitize.py tests/fixtures.json tests/ui/fixtures.sample.json
"""
import hashlib
import ipaddress
import json
import re
import sys
from pathlib import Path

LOCAL = Path(__file__).resolve().parent / "sanitize.local.json"

# Kept as they are: well-known public resolvers and loopback.
KEEP_V4 = {"0.0.0.0", "1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9", "4.2.2.2", "127.0.0.1"}
# /24 networks handed out in order of first appearance.
V4_POOL = (["192.0.2", "198.51.100", "203.0.113"] + [f"198.18.{i}" for i in range(256)]
           + [f"198.19.{i}" for i in range(256)])
DOC_V4 = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "198.18.0.0/15")]
DOC_V6_PREFIXES = ("2001:db8:", "fd00:db8:", "fe80:")
DOC_MAC_PREFIX = "00:00:5e:00:53:"
DOC_MAIL_DOMAINS = ("example.net", "example.com", "example.org")

MAC_RE = re.compile(r"\b[0-9a-f]{2}([:-])[0-9a-f]{2}(?:\1[0-9a-f]{2}){4}\b", re.I)
V4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
# Candidate IPv6: hex groups and colons; ipv6() decides whether it really is one.
V6_RE = re.compile(r"(?<![0-9a-z:])((?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4})(?![0-9a-z:])", re.I)
HEX_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{8,}\b", re.I)
MAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Telegram identities recorded by pairing: "chat 1234567", "Paired Telegram chat (some_user)".
CHAT_ID_RE = re.compile(r"\bchat -?\d{5,}\b")
CHAT_NAME_RE = re.compile(r"(Telegram chat \()[^)]*(\))")
# Address templates such as "10.0.{n}.1/24" (new-network subnet template).
V4_TEMPLATE_RE = re.compile(r"(?<![\d.])\d{1,3}\.\d{1,3}\.(?=\{n\})")
DOMAIN_KEYS = {"domain"}


def _digest(value, n):
    """n hex characters derived from value (stable across runs)."""
    out, i = "", 0
    while len(out) < n:
        out += hashlib.sha256(f"{i}:{value}".encode()).hexdigest()
        i += 1
    return out[:n]


def _is_v6(addr):
    """True for text that is an IPv6 address worth rewriting (not a time or a MAC)."""
    if "::" not in addr and addr.count(":") < 3:
        return False
    groups = addr.split(":")
    if any(len(g) > 4 for g in groups) or MAC_RE.fullmatch(addr):
        return False
    return not all(g in ("", "0", "1") for g in groups)     # "::", "::1", "::/0"


def _case_like(original, new):
    if original.isupper():
        return new.upper()
    if original[:1].isupper():
        return new[:1].upper() + new[1:]
    return new


def load_local():
    """(words, patterns) from sanitize.local.json, or empty ones."""
    if not LOCAL.exists():
        return {}, []
    d = json.loads(LOCAL.read_text(encoding="utf-8"))
    return {k.lower(): v for k, v in d.get("words", {}).items()}, d.get("patterns", [])


class Sanitizer:
    def __init__(self, words=None):
        self.words = {k.lower(): v for k, v in (words or {}).items()}
        self.nets = {}     # original /24 prefix -> replacement prefix
        self.macs = {}     # lower-case original -> serial number

    def ipv4(self, addr):
        try:
            ip = ipaddress.IPv4Address(addr)
        except ValueError:
            return addr                       # not an address (e.g. a version number)
        if addr in KEEP_V4 or addr.startswith("255.") or ip.is_multicast:
            return addr
        a, b, c, d = addr.split(".")
        prefix = f"{a}.{b}.{c}"
        if prefix not in self.nets:
            self.nets[prefix] = V4_POOL[len(self.nets)]
        return f"{self.nets[prefix]}.{d}"

    def ipv6(self, addr):
        if not _is_v6(addr):
            return addr
        groups = addr.split(":")
        first = groups[0].lower()
        out = []
        for i, g in enumerate(groups):
            if not g:
                out.append(g)
            elif i == 0:
                out.append("fe80" if first == "fe80" else "fd00" if first.startswith("fd") else "2001")
            elif i == 1 and first != "fe80":
                out.append("db8")
            else:
                out.append(_digest(f"{first}:{g}:{i}", len(g)))
        return ":".join(out)

    def mac(self, m):
        sep = m.group(1)
        key = m.group(0).lower().replace("-", ":")
        if key not in self.macs:
            self.macs[key] = len(self.macs) + 1
        new = sep.join(["00", "00", "5e", "00", "53", f"{self.macs[key] % 256:02x}"])
        return new.upper() if m.group(0).isupper() else new

    @staticmethod
    def domain(name):
        """A name under example.com with the same length (at least 'dd.example.com')."""
        label = max(2, len(name) - len(".example.com"))
        return "d" + _digest(name, label - 1) + ".example.com"

    def text(self, s, key=None):
        if not s:
            return s
        if key in DOMAIN_KEYS:
            return self.domain(s)
        s = MAIL_RE.sub("user@example.net", s)
        s = CHAT_ID_RE.sub("chat 100200300", s)
        s = CHAT_NAME_RE.sub(r"\1noc_operator\2", s)
        s = MAC_RE.sub(self.mac, s)
        s = V6_RE.sub(lambda m: self.ipv6(m.group(1)), s)
        s = V4_RE.sub(lambda m: self.ipv4(m.group(1)), s)
        s = HEX_RE.sub(lambda m: _digest(m.group(0).lower(), len(m.group(0))), s)
        s = V4_TEMPLATE_RE.sub("198.18.", s)
        for word, new in self.words.items():
            s = re.sub(rf"(?i)(?<![a-z]){re.escape(word)}(?![a-z])", lambda m: _case_like(m.group(0), new), s)
        return s

    def walk(self, obj, key=None):
        if isinstance(obj, dict):
            return {self.text(k): self.walk(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.walk(v, key) for v in obj]
        if isinstance(obj, str):
            return self.text(obj, key)
        return obj


def sanitize(data, words=None):
    return Sanitizer(words).walk(data)


def _strings(obj, key=None):
    """(key, text) for every dict key and string value."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield None, k
            yield from _strings(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, key)
    elif isinstance(obj, str):
        yield key, obj


def not_documentation(s):
    """Addresses, MACs and e-mails in s that are outside the documentation ranges."""
    bad = []
    for m in V4_RE.finditer(s):
        a = m.group(1)
        try:
            ip = ipaddress.IPv4Address(a)
        except ValueError:
            continue
        if a in KEEP_V4 or a.startswith("255.") or ip.is_multicast or any(ip in n for n in DOC_V4):
            continue
        bad.append(a)
    bad += [m.group(0) for m in MAC_RE.finditer(s) if not m.group(0).lower().replace("-", ":").startswith(DOC_MAC_PREFIX)]
    for m in V6_RE.finditer(s):
        a = m.group(1)
        if _is_v6(a) and not a.lower().startswith(DOC_V6_PREFIXES) and not a.startswith("::"):
            bad.append(a)
    bad += [m.group(0) for m in MAIL_RE.finditer(s) if not m.group(0).lower().endswith(DOC_MAIL_DOMAINS)]
    return bad


def sensitive_values(data):
    """Every value in a capture that must not survive sanitising."""
    found = set()
    for key, s in _strings(data):
        found.update(v.lower() for v in not_documentation(s))
        found.update(m.group(0).lower() for m in HEX_RE.finditer(s))
        if key in DOMAIN_KEYS:
            found.add(s.lower())
    return found


def leaks(clean, original=None, patterns=(), words=()):
    """Sensitive text left in a sanitised document: anything outside the documentation
    ranges, matches of the given patterns, words that should have been replaced, and
    (given the original capture) any original address, ID or domain still present."""
    hits = set()
    for _, s in _strings(clean):
        hits.update(not_documentation(s))
    text = json.dumps(clean, ensure_ascii=False)
    low = text.lower()
    for p in patterns:
        hits.update(m.group(0) for m in re.finditer(p, text, re.I))
    hits.update(w for w in words if re.search(rf"(?<![a-z]){re.escape(w.lower())}(?![a-z])", low))
    if original is not None:
        hits.update(v for v in sensitive_values(original) if v and v in low)
    return sorted(hits)


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1])
        return 2
    src, dst = Path(argv[1]), Path(argv[2])
    words, patterns = load_local()
    original = json.loads(src.read_text(encoding="utf-8"))
    clean = sanitize(original, words)
    bad = leaks(clean, original, patterns, words)
    if bad:
        print("refusing to write: sensitive text survived:", *bad[:20], sep="\n  ")
        return 1
    dst.write_text(json.dumps(clean, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {dst} ({dst.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
