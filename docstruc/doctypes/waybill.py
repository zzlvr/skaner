"""Waybill / delivery note (накладная на отпуск запасов, e.g. Kazakh form З-2, товарная накладная)."""

from pydantic import BaseModel, Field

from docstruc.doctypes.base import DocType
from docstruc.doctypes.common import Party, UnitLineItem, uncertain_fields_field
from docstruc.validation import checks


class Waybill(BaseModel):
    waybill_number: str | None = None
    waybill_date: str | None = Field(None, description="YYYY-MM-DD")
    contract_number: str | None = Field(None, description="Contract or other basis document number, if printed")
    contract_date: str | None = Field(None, description="YYYY-MM-DD")
    sender: Party = Field(Party(), description="Supplier who releases the goods (отправитель, поставщик, жіберуші)")
    recipient: Party = Field(Party(), description="Who receives the goods (получатель, покупатель, алушы)")
    currency: str | None = Field(None, description="ISO 4217 code")
    items: list[UnitLineItem] = []
    subtotal: float | None = Field(None, description="Total without VAT, only if printed")
    tax_rate: float | None = Field(None, description="VAT rate in percent, e.g. 12 for 12%")
    tax_amount: float | None = Field(None, description="VAT amount, even if included in prices, only if printed")
    total: float | None = Field(None, description="Total amount of the waybill, including VAT if any")
    uncertain_fields: list[str] = uncertain_fields_field()


WAYBILL = DocType(
    name="waybill",
    title="Waybill / delivery note",
    schema=Waybill,
    instructions=(
        "This is a waybill or delivery note (накладная на отпуск запасов на сторону, e.g. Kazakh form З-2; "
        "товарная накладная, жүкқұжат). Extract the sender and the recipient with their BIN/IIN, the basis "
        "contract, every goods line with its unit, quantity, price and amount, and the totals. "
        "If there are two quantity columns (to be released / released), use the released quantity. "
        "Ignore signatures, stamps and the names of signing persons."
    ),
    validators=[
        checks.model_uncertain,
        checks.required("waybill_number", "waybill_date", "total"),
        checks.valid_date("waybill_date", "contract_date"),
        checks.line_totals,
        checks.items_sum,
        checks.total_consistent,
        checks.tax_consistent,
        checks.tax_ids("sender.tax_id", "recipient.tax_id"),
    ],
)
