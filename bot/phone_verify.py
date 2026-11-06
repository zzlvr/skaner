"""Phone confirmation for the website trial (decision 43): the protocol shared by the bot and the website.

Standard library only, so the website can import it without the Telegram library.

- The website gives the user a link https://t.me/<bot>?start=v_<token> (the token is random, the site stores only
  its hash, 15 minutes, one use).
- The bot asks for the user's OWN contact (Telegram "share my number" button), turns the number into digits and
  computes phone_fp = HMAC-SHA256(key derived from PHONE_VERIFY_SECRET with the label "phone-fp", digits). The number
  itself is never stored, logged or sent anywhere: only the fingerprint leaves the bot.
- The bot POSTs {token, phone_fp, ts} to the website's internal endpoint with the header
  X-Skaner-Signature = hex HMAC-SHA256(PHONE_VERIFY_SECRET, f"{token}|{phone_fp}|{ts}").
"""

import hashlib
import hmac
import re
import time
import unicodedata

SIGNATURE_HEADER = "X-Skaner-Signature"
PATH = "/internal/phone-verified"
START_PREFIX = "v_"
MIN_SECRET = 32
MAX_SKEW = 300  # seconds: the website refuses a request whose ts is further than this from its clock

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,60}")  # Telegram start parameters: up to 64 characters, A-Z a-z 0-9 _ -
FP_RE = re.compile(r"[0-9a-f]{64}")
SIG_RE = re.compile(r"[0-9a-f]{64}")
_PHONE_JUNK = str.maketrans("", "", " -(). ")

# What the website answers (JSON {"status": ...}); the bot turns these into plain sentences.
OK = "ok"  # number confirmed (the trial is granted now, or as soon as the site allows it)
PHONE_USED = "phone_used"  # this number already received a trial (this or another account)
OTHER_PHONE = "other_phone"  # the account already has a different confirmed number
INVALID = "invalid"  # unknown, expired or already used link: one answer for all, no oracle
DENIED = "denied"  # bad signature or stale time: a configuration problem, not the user's fault
LIMITED = "limited"  # too many requests


def valid_secret(secret: str | None) -> bool:
    return isinstance(secret, str) and len(secret) >= MIN_SECRET


def fp_key(secret: str) -> bytes:
    """HKDF-SHA256 style derivation (fixed salt, info "phone-fp"): the fingerprint key is never the raw secret."""
    prk = hmac.new(b"skaner-phone/v1", secret.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(prk, b"phone-fp\x01", hashlib.sha256).digest()


def normalize_phone(raw: str | None) -> str | None:
    """Digits of an international number (E.164 without the plus), or None. Telegram sends contacts in
    international form, with or without "+"; spaces, dashes, dots and brackets are tolerated."""
    if not isinstance(raw, str) or len(raw) > 40:
        return None
    s = unicodedata.normalize("NFKC", raw).strip().translate(_PHONE_JUNK)
    if s.startswith("+"):
        s = s[1:]
    elif s.startswith("00"):
        s = s[2:]
    if not s.isascii() or not s.isdigit() or not 8 <= len(s) <= 15 or s[0] == "0":
        return None
    return s


def phone_fp(secret: str, digits: str) -> str:
    return hmac.new(fp_key(secret), digits.encode("ascii"), hashlib.sha256).hexdigest()


def sign(secret: str, token: str, fp: str, ts: int) -> str:
    return hmac.new(secret.encode("utf-8"), f"{token}|{fp}|{ts}".encode("utf-8"), hashlib.sha256).hexdigest()


def signature_ok(secret: str, token: str, fp: str, ts: int, signature: str | None) -> bool:
    """Constant-time comparison. Shapes are checked by the caller before the token or fingerprint are used."""
    if not valid_secret(secret) or not isinstance(signature, str) or not SIG_RE.fullmatch(signature):
        return False
    return hmac.compare_digest(sign(secret, token, fp, ts).encode("ascii"), signature.encode("ascii"))


def fresh(ts: int, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return abs(now - ts) <= MAX_SKEW


def token_from_start(arg: str | None) -> str | None:
    """The token of a "/start v_<token>" deep link, or None."""
    if not isinstance(arg, str) or not arg.startswith(START_PREFIX):
        return None
    token = arg[len(START_PREFIX):]
    return token if TOKEN_RE.fullmatch(token) else None
