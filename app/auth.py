"""NOC login password: PBKDF2 hashes in secrets.env, shared by the web app and noc-passwd.

secrets.env holds NOC_ADMIN_PASS_HASH=pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>. An older
install may still have a plain NOC_ADMIN_PASS; it keeps working until the password is changed,
which writes the hash and empties the plain value.
"""
import hashlib
import hmac
import secrets

ITERATIONS = 240_000
MIN_LENGTH = 10


def hash_password(pw, iterations=ITERATIONS):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${h.hex()}"


def verify(pw, env):
    """True if pw is the NOC password in env (the hash if set, else the legacy plain value)."""
    stored = env.get("NOC_ADMIN_PASS_HASH", "")
    if stored:
        try:
            algo, it, salt, want = stored.split("$")
            if algo != "pbkdf2_sha256":
                return False
            got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it)).hex()
            return hmac.compare_digest(got, want)
        except ValueError:
            return False
    plain = env.get("NOC_ADMIN_PASS", "")
    return bool(plain) and hmac.compare_digest(pw, plain)


def check_new(pw, user=""):
    """None if pw is acceptable as a new password, else the reason."""
    if len(pw) < MIN_LENGTH:
        return f"use at least {MIN_LENGTH} characters"
    if len(pw) > 256:
        return "too long (256 characters at most)"
    if "\n" in pw or "\r" in pw:
        return "line breaks are not allowed"
    if user and pw.lower() == user.lower():
        return "must not be the user name"
    return None


def updates(pw):
    """secrets.env changes that set pw as the password."""
    return {"NOC_ADMIN_PASS_HASH": hash_password(pw), "NOC_ADMIN_PASS": ""}


def session_salt(env):
    """Mixed into session signatures, so changing the password signs every old session out."""
    cred = env.get("NOC_ADMIN_PASS_HASH") or env.get("NOC_ADMIN_PASS", "")
    return hashlib.sha256(cred.encode()).hexdigest()[:16]
