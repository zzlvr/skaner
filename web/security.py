"""Small security primitives: email normalization, fingerprints, one-time codes, tokens, CSRF, masking."""

import hashlib
import hmac
import ipaddress
import re
import secrets
import unicodedata

from .config import Settings

MAX_EMAIL = 254
_LOCAL_EXTRA = set("!#$%&'*+/=?^_`{|}~.-")
# Control, format, separator, surrogate, private and unassigned characters: never valid in an address.
_BAD_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs")


def normalize_email(raw: str) -> str | None:
    """NFKC + strip + lowercase, then a strict RFC-ish check. Returns None when the address is not acceptable.

    The normalized value is both fingerprinted and used as the delivery address, so a fingerprint always
    belongs to the mailbox that received the code. Quoted local parts and IP-literal domains are rejected.
    """
    if not isinstance(raw, str) or len(raw) > 4 * MAX_EMAIL:
        return None
    s = unicodedata.normalize("NFKC", raw).strip(" ").lower()
    if not s or len(s) > MAX_EMAIL or any(unicodedata.category(c) in _BAD_CATEGORIES for c in s):
        return None
    if s.count("@") != 1:
        return None
    local, domain = s.split("@")
    if not 1 <= len(local) <= 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    if not all(c.isalnum() or c in _LOCAL_EXTRA for c in local):
        return None
    labels = domain.split(".")
    if len(domain) > 253 or len(labels) < 2:
        return None
    for label in labels:
        if not 1 <= len(label) <= 63 or label.startswith("-") or label.endswith("-"):
            return None
        if not all(c.isalnum() or c == "-" for c in label):
            return None
    if len(labels[-1]) < 2 or labels[-1].isdigit():
        return None
    return s


def _mac(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def email_fp(settings: Settings, normalized_email: str) -> str:
    return _mac(settings.key("fp:email"), normalized_email)


def ip_key(ip: str | None) -> str:
    """The unit every per-IP limit counts. IPv4: the address. IPv6: its /64 network, because one home or one cheap
    server gets a whole /64 (2^64 addresses), so counting single IPv6 addresses would make every per-IP limit (login
    codes, failed codes, trials, payment requests) free to bypass. IPv4-mapped IPv6 counts as the IPv4 address."""
    if not ip:
        return "unknown"
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network((addr, 64), strict=False))
    return str(addr)


def ip_fp(settings: Settings, ip: str | None) -> str:
    return _mac(settings.key("fp:ip"), ip_key(ip))


def ua_fp(settings: Settings, ua: str | None) -> str | None:
    return _mac(settings.key("fp:ua"), ua[:512]) if ua else None


def new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def code_hash(settings: Settings, row_id: int, fp: str, code: str) -> str:
    """Bound to the row and the email fingerprint: a hash copied to another row or address is useless."""
    return _mac(settings.key("code"), f"{row_id}|{fp}|{code}")


def clean_code(raw: str) -> str | None:
    """Accept '123 456', '123-456' and full-width digits; otherwise exactly six ASCII digits."""
    if not isinstance(raw, str) or len(raw) > 32:
        return None
    s = unicodedata.normalize("NFKC", raw).replace(" ", "").replace("-", "")
    return s if re.fullmatch(r"[0-9]{6}", s) else None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_token_shape(value: str | None) -> bool:
    return bool(value) and re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is not None


def csrf_token(settings: Settings, csrf_id: str) -> str:
    return _mac(settings.key("csrf"), csrf_id)


def csrf_ok(settings: Settings, csrf_id: str | None, submitted: str | None) -> bool:
    if not valid_token_shape(csrf_id) or not isinstance(submitted, str) or len(submitted) != 64:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), csrf_token(settings, csrf_id).encode("ascii"))


def mask_email(address: str) -> str:
    """a***@d***.com: enough to tell test mailboxes apart, not enough to identify a person."""
    local, _, domain = address.partition("@")
    name, _, tld = domain.rpartition(".")
    return f"{local[:1]}***@{name[:1]}***.{tld}" if name else f"{local[:1]}***@***"
