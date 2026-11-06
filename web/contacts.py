"""Contacts page: a signed-in visitor can write to the owner.

Nobody's phone or email is published. The message goes through the same path as a payment request: stored encrypted
(plan="question"), counted by the same daily limits, and sent to the owner in Telegram (web/payments.py).
Signed-out visitors are asked to sign in first: the code login keeps spam out and gives an account code to answer to.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from . import payments
from . import security as sec
from .deps import csrf_protect, current_user, get_db
from .models import User

router = APIRouter()


def _page(request: Request, user: User | None, status: int = 200, **ctx):
    return request.app.state.render(request, "contacts.html", status, user=user, **ctx)


@router.get("/contacts")
def contacts(request: Request, user: User | None = Depends(current_user)):
    return _page(request, user)


@router.post("/contacts", dependencies=[Depends(csrf_protect)])
def contacts_send(
    request: Request, background: BackgroundTasks, contact: str = Form(""), message: str = Form(""),
    user: User | None = Depends(current_user), db: Session = Depends(get_db),
):
    if user is None:
        return RedirectResponse("/login?next=/contacts", status_code=303)
    st = request.app.state
    now = st.clock()
    # Shown back in the form on an error (escaped by the template); cut so a huge value is not echoed.
    values = {"contact": contact[:payments.CONTACT_MAX * 2], "message": message[:payments.MESSAGE_MAX * 2]}
    try:
        form = payments.parse_question(contact, message)
    except payments.FormError as e:
        return _page(request, user, 400, error=e.code, values=values)
    ipf = sec.ip_fp(st.settings, st.client_ip(request))
    if payments.over_limit(db, user.id, ipf, now):
        return _page(request, user, 429, error="many", values=values)
    request_id = st.payments.submit(db, user, form, ipf, now)
    background.add_task(st.payments.notify, request_id, payments.notice_text(user.account_code or "-", form), now)
    return _page(request, user, sent=True)
