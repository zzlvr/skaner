"""Daily encrypted copy of the databases to the owner's Telegram (run by skaner-backup.service after backup_db.py).

    python deploy/backup_offsite.py [--out FILE]      # --out: write the encrypted file instead of sending it

What is sent: the newest local copies of web.sqlite3 and bot.sqlite3 and the two secrets needed to read them again
(WEB_SECRET, PHONE_VERIFY_SECRET), packed into one archive and encrypted with the owner's PUBLIC key
(a self-signed certificate, /opt/skaner/backup_public.pem, `openssl cms -encrypt`). The matching private key exists only at the owner: a copy
that is stolen from Telegram, or a server that is taken over, cannot open it. Nothing unencrypted touches the disk.
Too large for a Telegram bot (45 MB): the owner is told and the copy is skipped (time to move to object storage).
"""

import io
import os
import subprocess
import sys
import tarfile
import time
from datetime import date
from pathlib import Path

import httpx

DATA = Path(os.environ.get("BACKUP_DATA_DIR", "/opt/skaner/data"))
PUBLIC_KEY = Path(os.environ.get("BACKUP_PUBLIC_KEY", "/opt/skaner/backup_public.pem"))
OPENSSL = os.environ.get("OPENSSL", "openssl")
LIMIT = 45 * 1024 * 1024
SECRET_NAMES = ("WEB_SECRET", "PHONE_VERIFY_SECRET")


def newest_copy(stem: str) -> Path | None:
    copies = sorted((DATA / "backups").glob(f"{stem}-*.sqlite3"))
    return copies[-1] if copies else None


def build_archive(today: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for stem in ("web", "bot"):
            path = newest_copy(stem)
            if path is not None:
                tar.add(path, arcname=f"{stem}-{today}.sqlite3")
        secrets = "".join(f"{k}={os.environ[k]}\n" for k in SECRET_NAMES if os.environ.get(k)).encode()
        info = tarfile.TarInfo("secrets.env")
        info.size, info.mode, info.mtime = len(secrets), 0o600, int(time.time())
        tar.addfile(info, io.BytesIO(secrets))
    return buf.getvalue()


def encrypt(data: bytes) -> bytes:
    if not PUBLIC_KEY.exists():
        raise RuntimeError("the public certificate file is missing")
    done = subprocess.run([OPENSSL, "cms", "-encrypt", "-aes-256-cbc", "-binary", "-outform", "DER", str(PUBLIC_KEY)],
                          input=data, capture_output=True, check=False)
    if done.returncode != 0 or not done.stdout:
        raise RuntimeError("openssl could not encrypt the archive")
    return done.stdout


def send(blob: bytes, name: str, caption: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("BOT_OWNER_ID", "")
    if not token or not chat:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / BOT_OWNER_ID are not set")
    with httpx.Client(timeout=120) as client:
        r = client.post(f"https://api.telegram.org/bot{token}/sendDocument",
                        data={"chat_id": chat, "caption": caption, "disable_notification": "true"},
                        files={"document": (name, blob, "application/octet-stream")})
    if r.status_code != 200:
        raise RuntimeError(f"Telegram answered {r.status_code}")


def notice(text: str) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("BOT_OWNER_ID", "")
    if token and chat:
        with httpx.Client(timeout=30) as client:
            client.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": text})


def main(argv: list[str]) -> int:
    today = date.today().isoformat()
    name = f"skaner-backup-{today}.tar.gz.cms"
    try:
        blob = encrypt(build_archive(today))
        if "--out" in argv:
            Path(argv[argv.index("--out") + 1]).write_bytes(blob)
            return 0
        if len(blob) > LIMIT:
            notice("Skaner: the database copy is over 45 MB and does not fit in Telegram. Time to move the copies "
                   "to cloud storage.")
            return 1
        send(blob, name, f"Database copy {today}. Encrypted with your key.")
    except Exception as exc:  # noqa: BLE001  (a failing copy is reported, never silent)
        print(f"offsite backup failed: {type(exc).__name__}", file=sys.stderr)
        try:
            notice(f"Skaner: the database copy for {today} was not sent ({type(exc).__name__}). Check the log: "
                   "journalctl -u skaner-backup.")
        except Exception:  # noqa: BLE001
            pass
        return 1
    print(f"offsite backup sent: {len(blob)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
