"""Free mode needs no sign-in: a visitor gets a guest account, keeps results for a few days, and can sign in later to
keep them under an email. Paid mode still requires the sign-in."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from test_app import Env, batch_url, csrf, latest
from test_byok import post, seen  # noqa: F401  (seen is a fixture)
from web import auth
from web.models import Batch, User, UserSession


def guests(e):
    with e.db() as s:
        return s.scalars(select(User).where(User.is_guest.is_(True))).all()


@pytest.mark.product_defaults
def test_a_visitor_gets_a_guest_account_and_the_upload_page():
    e = Env()
    c = e.new_client("en")
    r = c.get("/app")
    assert r.status_code == 200 and 'name="byok_key"' in r.text
    assert "sk_session" in c.cookies and len(guests(e)) == 1
    g = guests(e)[0]
    assert g.account_code and g.account_code.startswith("SK-") and len(g.email_fp) == 64
    assert c.get("/app").status_code == 200 and len(guests(e)) == 1  # the same guest on the next visit


@pytest.mark.product_defaults
def test_guest_uploads_with_a_key_and_the_results_are_kept_for_a_week(seen):  # noqa: F811
    calls, holder = seen
    e = holder["env"] = Env()
    c = e.new_client("en")
    c.get("/app")
    r = post(c)
    assert c.get(batch_url(r)).status_code == 200
    b = latest(e)
    assert b.status == "done" and b.byok and b.user_id == guests(e)[0].id
    assert b.expires_at - b.created_at == timedelta(days=e.settings.guest_retention_days) == timedelta(days=7)
    assert len(calls) == 1


@pytest.mark.product_defaults
def test_the_guest_page_offers_sign_in_not_company_or_logout():
    e = Env()
    c = e.new_client("en")
    html = c.get("/app").text
    assert "/login?next=/app" in html and "Sign in to keep a history" in html
    assert "/app/company" not in html and 'action="/logout"' not in html


@pytest.mark.product_defaults
def test_guest_sessions_per_network_are_limited_then_the_sign_in_page_is_shown():
    e = Env(guests_per_ip_day=2)
    codes = [e.new_client("en").get("/app", follow_redirects=False).status_code for _ in range(3)]
    assert codes == [200, 200, 303] and len(guests(e)) == 2
    e.clock.advance(hours=25)
    assert e.new_client("en").get("/app", follow_redirects=False).status_code == 200  # a new day


@pytest.mark.product_defaults
def test_no_guest_is_created_by_a_post_or_for_the_admin_area():
    e = Env()
    c = e.new_client("en")
    r = c.post("/app/upload", data={"csrf_token": "x"}, follow_redirects=False)
    assert r.status_code in (303, 403) and not guests(e)
    assert c.get("/admin").status_code == 404 and not guests(e)


def test_paid_mode_still_requires_the_sign_in():
    e = Env(paid_plans=True)
    r = e.new_client("en").get("/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login") and not guests(e)


@pytest.mark.product_defaults
def test_signing_in_moves_the_guests_uploads_to_the_account(seen):  # noqa: F811
    calls, holder = seen
    e = holder["env"] = Env()
    c = e.new_client("en")
    c.get("/app")
    batch_url(post(c))
    guest_id = guests(e)[0].id
    e.login(c, "anna@example.com")
    with e.db() as s:
        assert s.get(User, guest_id) is None and not guests(e)
        user = s.scalar(select(User).where(User.is_guest.is_(False)))
        assert s.scalar(select(Batch)).user_id == user.id
    assert c.get("/app/history").status_code == 200


@pytest.mark.product_defaults
def test_finished_guest_sessions_are_deleted_with_their_uploads(seen):  # noqa: F811
    calls, holder = seen
    e = holder["env"] = Env()
    old, fresh = e.new_client("en"), e.new_client("en")
    old.get("/app")
    batch_url(post(old))
    e.clock.advance(days=40)  # the first guest's session has run out
    fresh.get("/app")
    with e.db() as s:
        assert auth.purge_guests(s, e.clock()) == 1
    assert len(guests(e)) == 1 and latest(e) is None
    with e.db() as s:
        assert s.scalar(select(UserSession.id)) is not None  # the live guest keeps working


@pytest.mark.product_defaults
def test_guest_sessions_for_the_whole_site_are_limited_too():
    e = Env(guests_per_day=2, guests_per_ip_day=100)
    codes = [e.new_client("en").get("/app", follow_redirects=False).status_code for _ in range(3)]
    assert codes == [200, 200, 303]
