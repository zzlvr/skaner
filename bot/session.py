"""Files a user has sent but not processed yet. Kept in memory only, never written to disk."""

import asyncio
import time
from dataclasses import dataclass, field

from docstruc.core.files import page_count
from docstruc.core.models import Document

# Billing rules are in bot/billing.py. Limits: pages in one PDF, photos of one long receipt.
MAX_PDF_PAGES = 20
LONG_RECEIPT_MAX_PARTS = 10
# Files wait in memory until the user picks an action: cap them per user and in total, and forget idle ones,
# so that nobody can fill the server's memory (the service is limited to 1 GB and would restart).
MAX_BATCH_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 400 * 1024 * 1024
IDLE_SECONDS = 30 * 60

DOC_CHOICES = {
    "receipt": ("receipt", False),
    "invoice": ("invoice", False),
    "act": ("act", False),
    "waybill": ("waybill", False),
    "receipt_long": ("receipt", True),
}


@dataclass
class Batch:
    files: list[Document] = field(default_factory=list)
    choice: str | None = None  # key of DOC_CHOICES
    status_message_id: int | None = None
    feedback_for: str | None = None  # result kind the user said 👎 to; their next text message describes the error
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    touched: float = field(default_factory=time.monotonic)

    @property
    def size(self) -> int:
        return sum(len(f.data) for f in self.files)

    @property
    def pages(self) -> int:
        return sum(page_count(f) for f in self.files)

    @property
    def doc_count(self) -> int:
        """The least the batch will be billed: one document per photo or PDF page."""
        return self.pages

    def clear(self) -> None:
        self.files.clear()
        self.choice = None
        self.status_message_id = None


class Sessions:
    def __init__(self):
        self._batches: dict[int, Batch] = {}

    def get(self, user_id: int) -> Batch:
        self._forget_idle()
        batch = self._batches.setdefault(user_id, Batch())
        batch.touched = time.monotonic()
        return batch

    def total_size(self) -> int:
        return sum(b.size for b in self._batches.values())

    def _forget_idle(self) -> None:
        cutoff = time.monotonic() - IDLE_SECONDS
        for user_id, batch in list(self._batches.items()):
            if batch.touched < cutoff and not batch.lock.locked():
                del self._batches[user_id]
