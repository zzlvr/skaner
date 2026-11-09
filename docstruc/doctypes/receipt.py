from pydantic import BaseModel, Field

from docstruc.doctypes.base import DocType
from docstruc.doctypes.common import LineItem, uncertain_fields_field
from docstruc.validation import checks


class Receipt(BaseModel):
    merchant_name: str | None = None
    merchant_address: str | None = None
    merchant_tax_id: str | None = Field(None, description="Merchant tax / registration number")
    receipt_number: str | None = None
    date: str | None = Field(None, description="YYYY-MM-DD")
    time: str | None = Field(None, description="HH:MM")
    currency: str | None = Field(None, description="ISO 4217 code")
    items: list[LineItem] = []
    subtotal: float | None = Field(None, description="Sum before discounts, only if printed")
    discount: float | None = Field(None, description="Total discount as a positive number, only if printed")
    tax_amount: float | None = Field(None, description="Total tax (sum of all tax rates), even if included in prices")
    total: float | None = Field(None, description="Final amount to pay, after discounts")
    payment_method: str | None = Field(None, description="cash, card, ... as printed")
    uncertain_fields: list[str] = uncertain_fields_field()


RECEIPT = DocType(
    name="receipt",
    title="Receipt",
    schema=Receipt,
    instructions="This is a sales receipt. Extract the merchant, every purchased line item and the totals.",
    validators=[
        checks.model_uncertain,
        checks.required("total", "date"),
        checks.valid_date("date"),
        checks.line_totals,
        checks.items_sum,
        checks.total_consistent,
        checks.tax_ids("merchant_tax_id"),
    ],
)
