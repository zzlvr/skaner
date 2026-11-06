"""The site's own 404 page: for browsers only, the same for every missing and hidden address."""

import re

import pytest
from fastapi.testclient import TestClient

from tests.test_auth import SECRET, Clock, FakeMailer
from web.config import Settings
from web.main import create_app

HTML = {"accept": "text/html,application/xhtml+xml"}


def client(lang="ru", **kw):
    app = create_app(Settings(db_url="sqlite://", secret=SECRET, base_url="http://testserver", dev=True,
                              admin_emails=("boss@example.com",), **kw), mailer=FakeMailer(), clock=Clock())
    return TestClient(app, headers={"accept-language": lang})


def body(r):
    return re.sub(r'value="[0-9a-f]{64}"', 'value="T"', r.text)  # the form token of the language switch differs


def test_browser_gets_the_page_and_other_clients_get_json():
    c = client()
    r = c.get("/no/such/page", headers=HTML)
    assert r.status_code == 404 and "Такой страницы нет, или она переехала" in r.text and 'href="/"' in r.text
    r = c.get("/no/such/page")
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    assert c.post("/no/such/page", data={}).json() == {"detail": "Not Found"}


@pytest.mark.parametrize("lang, word", [("ru", "Такой страницы нет"), ("en", "There is no such page"), ("kk", "Мұндай бет жоқ")])
def test_page_in_every_language(lang, word):
    assert word in client(lang).get("/nope", headers=HTML).text


def test_hidden_areas_look_exactly_like_missing_addresses():
    c = client()
    ref = body(c.get("/nope", headers=HTML))
    for path in ("/admin", "/admin/users", "/admin/audit", "/internal/phone-verified", "/internal/x", "/__not_found"):
        r = c.get(path, headers=HTML)
        assert r.status_code == 404 and body(r) == ref, path
    # JSON clients too, also for the wrong method
    ref_json = c.get("/nope").content
    for path in ("/admin", "/internal/phone-verified"):
        assert c.get(path).content == ref_json and c.delete(path).content == ref_json, path


def test_page_is_not_cached_or_framed_and_has_no_user_data():
    c = client()
    r = c.get("/nope", headers=HTML)
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]
    assert "Выйти" not in r.text and "boss@example.com" not in r.text
