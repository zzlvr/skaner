"""The nightly retention run deletes expired results even when nobody visits the site."""

from datetime import timedelta

from sqlalchemy import func, select, text

from test_app import png, upload
from test_plans_admin import Env
from web import purge
from web.models import Batch


def test_nightly_run_deletes_expired_results_and_keeps_fresh_ones(tmp_path, monkeypatch, capsys):
    url = f"sqlite:///{tmp_path / 'web.sqlite3'}"
    e = Env(db_url=url)
    c = e.login()
    assert upload(c, [("a.png", png())]).status_code == 303
    assert upload(c, [("b.png", png())]).status_code == 303
    with e.db() as db:
        old = db.scalars(select(Batch).order_by(Batch.id)).first()
        db.execute(text("update batches set expires_at = :t where id = :i"),
                   {"t": old.created_at - timedelta(days=1), "i": old.id})
        db.commit()
    monkeypatch.setenv("WEB_DB_URL", url)
    monkeypatch.setenv("WEB_DEV", "1")
    monkeypatch.setenv("WEB_SECRET", "x" * 40)
    assert purge.main() == 0
    assert "1 expired batches and 0 finished guest sessions deleted" in capsys.readouterr().out
    with e.db() as db:
        assert db.scalar(select(func.count()).select_from(Batch)) == 1
