"""Test defaults.

The product has no free trial (Settings.trial_pages defaults to 0). Most tests exercise the page
balance and the trial machinery, so they run with a trial unless a test sets its own values. Tests marked
`product_defaults` (and tests that read the real defaults, which build Settings through Settings.load or pass the values
explicitly) see the product defaults.
"""

import pytest

from web.config import Settings


@pytest.fixture(autouse=True)
def trial_for_old_tests(request, monkeypatch):
    if request.node.get_closest_marker("product_defaults"):
        return
    original = Settings.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("trial_pages", 50)
        kwargs.setdefault("paid_plans", True)
        original(self, *args, **kwargs)

    monkeypatch.setattr(Settings, "__init__", init)
