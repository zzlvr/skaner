"""What to charge for a batch, so that we never spend more on recognition than we charge.

Rules (shown to users in /plans):
- one photo or one PDF page = one document; several documents found on one page count as several;
  a long receipt in parts counts by photos;
- a job is never charged less than its actual recognition cost divided by COST_PER_DOC_KZT
  (a very dense page, a corrective retry or anything else unexpected becomes extra documents);
- a failed job that cost us nothing (the recognition service did not answer) is free;
  a failed job that did cost money is free up to FREE_FAILURES_PER_DAY times a day per user, then charged.
"""

import math
from dataclasses import dataclass

# Conservative exchange rate (the official one was ≈449 ₸ on 2026-10-09); raise it if the tenge weakens.
USD_KZT = 470
# Recognition cost covered by one document of the allowance: below our cheapest paid price (50 ₸ in the
# 100-document pack), so a document never costs more than it brings. Measured: a receipt with 5 lines ≈12 ₸,
# with 21 lines ≈32 ₸ (Russian text makes long answers); about 50+ lines on one page cost more than 45 ₸.
COST_PER_DOC_KZT = 45
FREE_FAILURES_PER_DAY = 3


def cost_units(cost_usd: float | None) -> int:
    """Documents of the allowance that cover an actual recognition cost."""
    if not cost_usd:
        return 0
    return math.ceil(cost_usd * USD_KZT / COST_PER_DOC_KZT - 1e-9)


@dataclass
class Job:
    """One recognition job: a file, or all parts of a long receipt."""

    units: int  # documents by the page rule
    cost_units: int  # documents that cover what the job actually cost us
    ok: bool


@dataclass
class Settlement:
    charged: int  # documents to take from the user's allowance
    free_failures_used: int
    work_units: int  # everything the batch cost us, in documents: counts toward the global daily cap


def settle(jobs: list[Job], free_failures_left: int) -> Settlement:
    charged = used = work = 0
    for job in jobs:
        if job.ok:
            units = max(job.units, job.cost_units)
            charged += units
            work += units
            continue
        work += job.cost_units
        if job.cost_units == 0:
            continue
        if used < free_failures_left:
            used += 1
        else:
            charged += job.cost_units
    return Settlement(charged=charged, free_failures_used=used, work_units=work)
