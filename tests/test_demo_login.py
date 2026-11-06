"""The permanent demo login: local development only, never in production."""

import pytest
from sqlalchemy import select

from test_app import csrf
from test_plans_admin import Env
from web.config import Settings
from web.main import create_app
from web.models import User

DEMO, CODE = "demo@skaner.test", "482915"


def demo_env(**kw):
    return Env(demo_email=DEMO, demo_code=CODE, **kw)


def sign_in(env, email, code):
    c = env.new_client()
    c.post("/login", data={"email": email, "csrf_token": csrf(c, "/login")}, follow_redirects=False)
    return c, c.post("/login/code", data={"code": code, "csrf_token": csrf(c, "/")}, follow_redirects=False)


def test_demo_account_always_signs_in_with_the_fixed_code():
    e = demo_env(admins=())
    for _ in range(3):  # no cooldown or attempt limit matters: the code is permanent
        c, r = sign_in(e, DEMO, CODE)
        assert r.status_code == 303 and r.headers["location"] == "/account"
        assert c.get("/app").status_code == 200


def test_demo_code_does_not_work_for_other_addresses():
    e = demo_env(admins=())
    _, r = sign_in(e, "someone@example.com", CODE)
    assert r.status_code == 400


def test_wrong_code_for_the_demo_address_is_refused():
    e = demo_env(admins=())
    _, r = sign_in(e, DEMO, "000000")
    assert r.status_code == 400


def test_demo_trial_is_restored_when_it_has_run_out():
    e = demo_env(admins=())
    c, _ = sign_in(e, DEMO, CODE)
    c.get("/app")
    with e.db() as s:
        u = s.scalar(select(User))
        u.trial_pages_left = 0
        s.commit()
    e.clock.advance(days=30)  # the 14-day trial is over too
    c, r = sign_in(e, DEMO, CODE)
    assert r.status_code == 303
    with e.db() as s:
        u = s.scalar(select(User))
        assert u.trial_pages_left == 50 and u.trial_expires_at > e.clock()


def test_demo_account_can_be_the_owner_in_admin():
    e = demo_env(admins=(DEMO,))
    c, r = sign_in(e, DEMO, CODE)
    assert r.status_code == 303 and c.get("/admin").status_code == 200


def test_site_refuses_to_start_with_demo_settings_outside_dev():
    s = Settings(db_url="sqlite://", secret="x" * 40, base_url="http://t", dev=False, demo_email=DEMO, demo_code=CODE)
    with pytest.raises(RuntimeError):
        create_app(s)


def test_env_with_demo_settings_is_refused_without_dev(monkeypatch):
    monkeypatch.delenv("WEB_DEV", raising=False)
    monkeypatch.setenv("WEB_SECRET", "x" * 40)
    monkeypatch.setenv("WEB_DEMO_EMAIL", DEMO)
    monkeypatch.setenv("WEB_DEMO_CODE", CODE)
    with pytest.raises(RuntimeError):
        Settings.load()


@pytest.mark.parametrize("email,code", [("not-an-address", CODE), (DEMO, "12345"), (DEMO, "abcdef"), (DEMO, "")])
def test_bad_demo_configuration_stops_the_app(email, code):
    s = Settings(db_url="sqlite://", secret="x" * 40, base_url="http://t", dev=True, demo_email=email, demo_code=code)
    with pytest.raises(RuntimeError):
        create_app(s)


def test_no_demo_login_by_default():
    e = Env()
    _, r = sign_in(e, DEMO, CODE)
    assert r.status_code == 400


def test_admin_link_in_the_menu_only_for_the_owner():
    e = demo_env(admins=(DEMO,))
    owner, _ = sign_in(e, DEMO, CODE)
    assert 'href="/admin"' in owner.get("/app").text
    customer = e.login(e.new_client(), "client@example.com")
    assert 'href="/admin"' not in customer.get("/app").text
    assert customer.get("/admin").status_code == 404
