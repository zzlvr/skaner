from pydantic import BaseModel, Field

from docstruc.doctypes.base import DocType
from docstruc.doctypes.common import LineItem, Party, uncertain_fields_field
from docstruc.validation import checks


class Invoice(BaseModel):
    invoice_number: str | None = None
    invoice_date: str | None = Field(None, description="YYYY-MM-DD")
    due_date: str | None = Field(None, description="YYYY-MM-DD")
    seller: Party = Party()
    buyer: Party = Party()
    currency: str | None = Field(None, description="ISO 4217 code")
    items: list[LineItem] = []
    subtotal: float | None = Field(None, description="Total before discounts and tax")
    discount: float | None = Field(None, description="Total discount as a positive number, only if printed")
    tax_rate: float | None = Field(None, description="Tax rate in percent, e.g. 12 for 12%")
    tax_amount: float | None = None
    total: float | None = Field(None, description="Total amount due, including tax")
    uncertain_fields: list[str] = uncertain_fields_field()


INVOICE = DocType(
    name="invoice",
    title="Invoice",
    schema=Invoice,
    instructions=(
        "This is an invoice. Extract the seller, the buyer, every line item and the totals. "
        "If a line shows its amount both without and with VAT (e.g. счёт-фактура columns «Стоимость без косвенных "
        "налогов» and «Всего стоимость реализации»), the line total is the amount without VAT, matching quantity × "
        "unit price."
    ),
    validators=[
        checks.model_uncertain,
        checks.required("invoice_number", "invoice_date", "total"),
        checks.valid_date("invoice_date", "due_date"),
        checks.line_totals,
        checks.items_sum,
        checks.total_consistent,
        checks.tax_consistent,
        checks.tax_ids("seller.tax_id", "buyer.tax_id"),
    ],
)
