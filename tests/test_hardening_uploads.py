"""Hardening tests for uploads and per-IP limits: bounded decoding, limited multipart parts, parallel uploads.
Fake backend, fake mailer, test databases: no network, no money, no real mail or Telegram."""

import json
import threading
import time
from datetime import timezone

import pytest
from fastapi.testclient import TestClient

from test_app import Env, csrf, png, upload
from web import cabinet
from web import security as sec
from web.jobs import UploadGate
from web.upload import MAX_FIELDS


# ---------- per-IP limits: IPv6 counts by /64 ----------

def test_ip_key_groups_ipv6_by_network():
    a, b = sec.ip_key("2001:db8:1:2::1"), sec.ip_key("2001:db8:1:2:ffff:ffff:ffff:ffff")
    assert a == b == "2001:db8:1:2::/64"
    assert sec.ip_key("2001:db8:1:3::1") != a
    assert sec.ip_key("::ffff:203.0.113.7") == sec.ip_key("203.0.113.7") == "203.0.113.7"
    assert sec.ip_key(None) == "unknown" and sec.ip_key("") == "unknown"
    assert sec.ip_key("not-an-ip") == "not-an-ip"


def test_login_limit_cannot_be_bypassed_by_rotating_ipv6_addresses():
    e = Env()
    statuses = []
    for n in range(22):
        c = TestClient(e.app, client=(f"2001:db8:aa:bb::{n + 1:x}", 4000))
        r = c.post("/login", data={"email": f"user{n}@example.com", "csrf_token": csrf(c, "/login")},
                   follow_redirects=False)
        statuses.append(r.status_code)
    assert statuses[:20] == [303] * 20
    assert statuses[20:] == [429, 429]
    other = TestClient(e.app, client=("2001:db8:aa:cc::1", 4000))  # another /64: its own limit
    r = other.post("/login", data={"email": "x@example.com", "csrf_token": csrf(other, "/login")},
                   follow_redirects=False)
    assert r.status_code == 303


# ---------- uploads: decoding is bounded ----------

@pytest.fixture
def counted_validate(monkeypatch):
    calls = []
    real = cabinet.validate

    def spy(raw, settings):
        calls.append(raw.index)
        return real(raw, settings)

    monkeypatch.setattr(cabinet, "validate", spy)
    return calls


def test_parallel_upload_of_the_same_user_is_refused_before_any_file_is_decoded(counted_validate):
    e = Env()
    e.login()
    uid = e.user().id
    assert e.app.state.uploads.enter(uid) is None  # an upload of this user is being received right now
    r = upload(e.client, [("a.png", png())])
    assert r.status_code == 409 and counted_validate == []
    e.app.state.uploads.leave(uid)
    r = upload(e.client, [("a.png", png())])
    assert r.status_code == 303 and counted_validate == [1]
    assert e.app.state.uploads.enter(uid) is None  # the finished upload left its slot


def test_site_wide_upload_slots_are_limited(counted_validate):
    e = Env()
    e.login()
    gate = e.app.state.uploads
    others = [10_000 + n for n in range(gate.max_uploads)]
    for uid in others:
        assert gate.enter(uid) is None
    r = upload(e.client, [("a.png", png())])
    assert r.status_code == 503 and counted_validate == []
    gate.leave(others[0])
    assert upload(e.client, [("a.png", png())]).status_code == 303


def test_refused_upload_frees_its_slot(counted_validate):
    e = Env()
    e.login()
    r = upload(e.client, [("a.txt", b"not an image")])
    assert r.status_code == 400
    assert e.app.state.uploads.enter(e.user().id) is None


def test_at_most_n_files_are_decoded_at_the_same_time(monkeypatch):
    gate = UploadGate(decoders=2, wait=5)
    now, peak, lock = [0], [0], threading.Lock()

    def slow(raw, settings):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.05)
        with lock:
            now[0] -= 1
        return object(), 1

    monkeypatch.setattr(cabinet, "validate", slow)

    class Raw:
        def __init__(self, i):
            self.index, self.data = i, b"x"

    threads = [threading.Thread(target=cabinet._validate_all, args=([Raw(i) for i in range(3)], None, gate))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 2


def test_decoder_wait_has_a_limit():
    gate = UploadGate(decoders=1, wait=0.05)
    assert gate.acquire_decoder()

    class Raw:
        index, data = 1, b"x"

    with pytest.raises(cabinet.Refusal) as exc:
        cabinet._validate_all([Raw()], None, gate)
    assert exc.value.code == "busy"
    gate.release_decoder()


def test_upload_does_not_hold_a_database_connection_while_files_are_checked(tmp_path, monkeypatch):
    e = Env(db_url=f"sqlite:///{tmp_path / 'web.sqlite3'}")
    e.login()
    seen = []
    real = cabinet.validate

    def spy(raw, settings):
        seen.append(e.app.state.engine.pool.checkedout())
        return real(raw, settings)

    monkeypatch.setattr(cabinet, "validate", spy)
    assert upload(e.client, [("a.png", png()), ("b.png", png())]).status_code == 303
    assert seen == [0, 0]


# ---------- uploads: the number of multipart parts is limited ----------

def _multipart(token: str, parts: int) -> bytes:
    b = b"xYz"
    empty = b"--" + b + b'\r\nContent-Disposition: form-data; name="files"; filename="a"\r\n\r\n\r\n'
    return (b"--" + b + b'\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n' + token.encode() + b"\r\n"
            + b"--" + b + b'\r\nContent-Disposition: form-data; name="fmt"\r\n\r\nxlsx\r\n'
            + b"--" + b + b'\r\nContent-Disposition: form-data; name="doctype"\r\n\r\nreceipt\r\n'
            + empty * parts
            + b"--" + b + b'\r\nContent-Disposition: form-data; name="files"; filename="p.png"\r\n\r\n' + png() + b"\r\n"
            + b"--" + b + b"--\r\n")


def test_a_body_of_many_empty_parts_is_refused():
    e = Env()
    e.login()
    ok = _multipart(csrf(e.client), 2)  # a real file after two empty inputs: accepted
    r = e.client.post("/app/upload", content=ok, headers={"content-type": "multipart/form-data; boundary=xYz"},
                      follow_redirects=False)
    assert r.status_code == 303
    e.clock.advance(minutes=1)
    body = _multipart(csrf(e.client), 5000)
    r = e.client.post("/app/upload", content=body, headers={"content-type": "multipart/form-data; boundary=xYz"},
                      follow_redirects=False)
    assert r.status_code == 400
    assert e.settings.max_files + MAX_FIELDS < 5000


def test_the_real_form_with_an_empty_camera_input_still_works():
    e = Env()
    e.login()
    files = [("files", (f"p{i}.png", png(), "image/png")) for i in range(e.settings.max_files)]
    files.append(("files", ("", b"", "application/octet-stream")))  # the unused camera input
    r = e.client.post("/app/upload", data={"csrf_token": csrf(e.client), "doctype": "receipt", "fmt": "xlsx",
                                           "one_document": "0"}, files=files, follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
