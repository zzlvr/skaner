"""'Something is wrong with this result': stored encrypted, owner notified, limits, access rules, admin view."""

import re

from sqlalchemy import func, select

from test_app import batch_url, png, upload
from test_plans_admin import ADMIN, Env, summary
from web import payments
from web.models import PaymentRequest


def csrf_of(client, path):
    return re.search(r'name="csrf_token" value="([0-9a-f]{64})"', client.get(path).text).group(1)


def send(client, url, kind="amount", comment="Итог 1000, а на фото 10000"):
    return client.post(url + "/feedback", data={"csrf_token": csrf_of(client, url), "kind": kind, "comment": comment},
                       follow_redirects=False)


def setup():
    e = Env()
    c = e.login()
    url = batch_url(upload(c, [("a.png", png())]))
    return e, c, url


def test_feedback_is_stored_encrypted_and_the_owner_is_told_without_the_document():
    e, c, url = setup()
    r = send(c, url)
    assert r.status_code == 303 and r.headers["location"] == url + "?fb=sent"
    page = c.get(url + "?fb=sent").text
    assert "Спасибо, сообщение отправлено." in page
    with e.db() as db:
        row = db.scalar(select(PaymentRequest).where(PaymentRequest.plan == "feedback"))
        assert row is not None and b"10000" not in row.data_enc and b"amount" not in row.data_enc
        data = payments.read(e.settings, row)
        assert data["kind"] == "amount" and data["batch"] == url.rsplit("/", 1)[1] and "10000" in data["comment"]
    note = e.notifier.sent[-1]
    assert "problem with a result" in note and "wrong amount or VAT" in note and url.rsplit("/", 1)[1] in note
    assert "SK-" in note and "admin: /admin/users/SK-" in note


def test_form_is_on_finished_batches_and_refuses_bad_input():
    e, c, url = setup()
    assert 'action="' + url + '/feedback"' in c.get(url).text
    for kind, comment in (("nope", "x"), ("amount", "write me at a@b.co"), ("amount", "x" * 3000), ("amount", "bad‮text")):
        r = send(c, url, kind, comment)
        assert r.status_code == 303 and r.headers["location"].endswith("?fb=bad"), (kind, comment[:10])
    with e.db() as db:
        assert db.scalar(select(func.count()).select_from(PaymentRequest).where(PaymentRequest.plan == "feedback")) == 0
    assert send(c, url, "other", "").headers["location"].endswith("?fb=sent")  # the comment is optional


def test_only_people_who_may_see_the_batch_can_send_and_csrf_is_required():
    e, c, url = setup()
    stranger = e.login(e.new_client(), "stranger@example.com")
    r = stranger.post(url + "/feedback", data={"csrf_token": csrf_of(stranger, "/app"), "kind": "amount", "comment": "x"},
                      follow_redirects=False)
    assert r.status_code == 404
    r = c.post(url + "/feedback", data={"kind": "amount", "comment": "x"}, follow_redirects=False)
    assert r.status_code == 403
    r = c.post(url + "/feedback", data={"csrf_token": csrf_of(c, url), "kind": "amount"}, headers={"origin": "https://evil.example"},
               follow_redirects=False)
    assert r.status_code == 403


def test_daily_limit():
    e, c, url = setup()
    for _ in range(payments.FB_USER_PER_DAY):
        assert send(c, url).headers["location"].endswith("?fb=sent")
    assert send(c, url).headers["location"].endswith("?fb=many")


def test_admin_lists_feedback_and_the_summary_shows_quality_without_the_owner():
    e, c, url = setup()
    send(c, url, "date", "дата неверная")
    owner = e.login(e.new_client(), ADMIN)
    own_url = batch_url(upload(owner, [("o.png", png())]))
    send(owner, own_url, "other", "my own test")
    a = e.admin()
    page = a.get("/admin/requests").text
    assert "Problem with a result" in page and "wrong date" in page and "дата неверная" in page
    home = a.get("/admin").text
    assert "Quality over 30 days" in home
    s = summary(home)
    assert "Reports of a problem with a result in 30 days" in home
    assert re.search(r'Reports of a problem with a result in 30 days: <a href="/admin/requests">1</a>', home)  # not 2
    assert re.search(r"<th scope=\"row\">Receipts</th><td>1</td>", home)  # one customer upload; the owner's is left out
