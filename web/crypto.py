"""Extraction results at rest: AES-256-GCM (authenticated encryption) with a key derived from WEB_SECRET.

The batch's public id is bound as associated data, so a blob copied to another batch (or another user's
batch) does not decrypt. Any change to the stored bytes is detected (InvalidTag -> ResultsUnreadable).
Layout: version byte | 12-byte random nonce | ciphertext+tag. The plaintext is zlib-compressed JSON.
Files (a free scan's PDF) use the same key and layout with their own associated-data label, so a file blob
and a results blob can never be swapped for each other; they are not compressed (a PDF already is).
Payment requests use a separate key (see encrypt_request).
"""

import json
import os
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import Settings

VERSION = b"\x01"
NONCE = 12


class ResultsUnreadable(Exception):
    """Wrong key, tampered or truncated blob. Carries no details on purpose."""


def _aad(public_id: str) -> bytes:
    return b"skaner-results/v1|" + public_id.encode("ascii")


def _file_aad(public_id: str) -> bytes:
    return b"skaner-file/v1|" + public_id.encode("ascii")


def encrypt(settings: Settings, public_id: str, payload: dict) -> bytes:
    plain = zlib.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)
    nonce = os.urandom(NONCE)
    return VERSION + nonce + AESGCM(settings.key("results")).encrypt(nonce, plain, _aad(public_id))


def decrypt(settings: Settings, public_id: str, blob: bytes | None) -> dict:
    if not blob or len(blob) < 1 + NONCE + 16 or blob[:1] != VERSION:
        raise ResultsUnreadable()
    nonce, body = blob[1:1 + NONCE], blob[1 + NONCE:]
    try:
        plain = AESGCM(settings.key("results")).decrypt(nonce, body, _aad(public_id))
        return json.loads(zlib.decompress(plain).decode("utf-8"))
    except (InvalidTag, zlib.error, ValueError, UnicodeDecodeError):
        raise ResultsUnreadable() from None


def encrypt_file(settings: Settings, public_id: str, data: bytes) -> bytes:
    nonce = os.urandom(NONCE)
    return VERSION + nonce + AESGCM(settings.key("results")).encrypt(nonce, data, _file_aad(public_id))


def decrypt_file(settings: Settings, public_id: str, blob: bytes | None) -> bytes:
    if not blob or len(blob) < 1 + NONCE + 16 or blob[:1] != VERSION:
        raise ResultsUnreadable()
    try:
        return AESGCM(settings.key("results")).decrypt(blob[1:1 + NONCE], blob[1 + NONCE:], _file_aad(public_id))
    except InvalidTag:
        raise ResultsUnreadable() from None


# Payment requests (company, contact, comment): their own key ("requests"), the row id bound as associated data,
# so a blob cannot be moved to another request, and a results key leak does not open contacts (and vice versa).

def _request_aad(request_id: int) -> bytes:
    return b"skaner-request/v1|" + str(int(request_id)).encode("ascii")


def encrypt_request(settings: Settings, request_id: int, payload: dict) -> bytes:
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(NONCE)
    return VERSION + nonce + AESGCM(settings.key("requests")).encrypt(nonce, plain, _request_aad(request_id))


def decrypt_request(settings: Settings, request_id: int, blob: bytes | None) -> dict:
    if not blob or len(blob) < 1 + NONCE + 16 or blob[:1] != VERSION:
        raise ResultsUnreadable()
    try:
        plain = AESGCM(settings.key("requests")).decrypt(blob[1:1 + NONCE], blob[1 + NONCE:], _request_aad(request_id))
        data = json.loads(plain.decode("utf-8"))
    except (InvalidTag, ValueError, UnicodeDecodeError):
        raise ResultsUnreadable() from None
    if not isinstance(data, dict):
        raise ResultsUnreadable()
    return data
