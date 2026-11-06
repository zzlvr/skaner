"""Offsite backup: encrypted with the owner's public key, restorable only with the private one, secrets inside."""

import io
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "deploy"))
import backup_offsite as bo  # noqa: E402

GIT_OPENSSL = Path("C:/Program Files/Git/usr/bin/openssl.exe")
OPENSSL = shutil.which("openssl") or (str(GIT_OPENSSL) if GIT_OPENSSL.exists() else None)
pytestmark = pytest.mark.skipif(OPENSSL is None, reason="openssl is not installed")


@pytest.fixture
def keys(tmp_path):
    priv, pub = tmp_path / "private.pem", tmp_path / "public.pem"
    subprocess.run([OPENSSL, "genrsa", "-out", str(priv), "2048"], check=True, capture_output=True)
    subprocess.run([OPENSSL, "req", "-x509", "-new", "-key", str(priv), "-days", "36500", "-subj", "/CN=skaner-backup",
                    "-out", str(pub)], check=True, capture_output=True)
    return priv, pub


@pytest.fixture
def data(tmp_path, monkeypatch, keys):
    d = tmp_path / "data"
    (d / "backups").mkdir(parents=True)
    for stem in ("web", "bot"):
        c = sqlite3.connect(d / "backups" / f"{stem}-2026-10-24.sqlite3")
        c.execute("create table t (v text)")
        c.execute("insert into t values (?)", (f"{stem}-row",))
        c.commit()
        c.close()
    monkeypatch.setattr(bo, "DATA", d)
    monkeypatch.setattr(bo, "PUBLIC_KEY", keys[1])
    monkeypatch.setattr(bo, "OPENSSL", OPENSSL)
    monkeypatch.setenv("WEB_SECRET", "web-secret-value")
    monkeypatch.setenv("PHONE_VERIFY_SECRET", "phone-secret-value")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:tok")
    monkeypatch.setenv("BOT_OWNER_ID", "42")
    return d


def decrypt(blob: bytes, private: Path) -> bytes:
    return subprocess.run([OPENSSL, "cms", "-decrypt", "-binary", "-inform", "DER", "-inkey", str(private)], input=blob,
                          capture_output=True, check=True).stdout


def test_encrypted_copy_restores_with_the_private_key_and_holds_databases_and_secrets(data, keys, tmp_path):
    out = tmp_path / "x.cms"
    assert bo.main(["--out", str(out)]) == 0
    blob = out.read_bytes()
    assert b"web-secret-value" not in blob and b"SQLite" not in blob  # nothing readable
    tar = tarfile.open(fileobj=io.BytesIO(decrypt(blob, keys[0])), mode="r:gz")
    names = tar.getnames()
    assert "secrets.env" in names and any(n.startswith("web-") for n in names) and any(n.startswith("bot-") for n in names)
    secrets = tar.extractfile("secrets.env").read().decode()
    assert "WEB_SECRET=web-secret-value" in secrets and "PHONE_VERIFY_SECRET=phone-secret-value" in secrets
    restored = tmp_path / "web.sqlite3"
    restored.write_bytes(tar.extractfile("web-2026-10-24.sqlite3").read() if "web-2026-10-24.sqlite3" in names
                         else tar.extractfile(next(n for n in names if n.startswith("web-"))).read())
    assert sqlite3.connect(restored).execute("select v from t").fetchone() == ("web-row",)


def test_other_key_cannot_open_it(data, tmp_path):
    out = tmp_path / "x.cms"
    bo.main(["--out", str(out)])
    other = tmp_path / "other.pem"
    subprocess.run([OPENSSL, "genrsa", "-out", str(other), "2048"], check=True, capture_output=True)
    with pytest.raises(subprocess.CalledProcessError):
        decrypt(out.read_bytes(), other)


def test_sends_a_document_to_the_owner_silently(data, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(request.url), request.content
        return httpx.Response(200, json={"ok": True})

    real = httpx.Client
    monkeypatch.setattr(bo.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert bo.main([]) == 0
    assert seen["url"].endswith("/sendDocument") and b"disable_notification" in seen["body"]
    assert b"web-secret-value" not in seen["body"]  # only the encrypted file travels


def test_failures_are_reported_to_the_owner_and_exit_nonzero(data, monkeypatch, tmp_path):
    notes = []
    monkeypatch.setattr(bo, "PUBLIC_KEY", tmp_path / "missing.pem")
    monkeypatch.setattr(bo, "notice", lambda text: notes.append(text))
    assert bo.main([]) == 1 and notes and "was not sent" in notes[0] and "missing" not in notes[0]


def test_too_large_copy_is_reported_not_sent(data, monkeypatch):
    notes = []
    monkeypatch.setattr(bo, "LIMIT", 10)
    monkeypatch.setattr(bo, "notice", lambda text: notes.append(text))
    monkeypatch.setattr(bo, "send", lambda *a, **k: pytest.fail("must not send"))
    assert bo.main([]) == 1 and "45 MB" in notes[0]
