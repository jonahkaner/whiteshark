from __future__ import annotations

from datetime import date
from decimal import Decimal
from pydantic import BaseModel, Field


class Supplier(BaseModel):
    supplier_id: str
    name: str
    contact_name: str = ""
    address: str = ""
    email: str = ""
    phone: str = ""
    payment_terms: str = "Net 30"
    default_currency: str = "USD"


class Product(BaseModel):
    product_id: str
    product_name: str
    supplier_id: str
    unit: str = "ea"
    default_price: float = 0.0
    currency: str = ""
    category: str = ""


class LineItem(BaseModel):
    product_name: str
    description: str = ""
    quantity: float
    unit: str = "ea"
    unit_price: float
    total: float = 0.0

    def compute_total(self) -> float:
        self.total = round(self.quantity * self.unit_price, 2)
        return self.total


class PurchaseOrder(BaseModel):
    po_number: str
    date_created: str = Field(default_factory=lambda: date.today().isoformat())
    supplier: Supplier
    line_items: list[LineItem] = []
    subtotal: float = 0.0
    tax: float = 0.0
    total: float = 0.0
    notes: str = ""
    status: str = "Draft"
    source_email_id: str = ""

    def compute_totals(self) -> None:
        for item in self.line_items:
            item.compute_total()
        self.subtotal = round(sum(item.total for item in self.line_items), 2)
        self.total = round(self.subtotal + self.tax, 2)


class ParsedRequest(BaseModel):
    """Output from Claude after parsing a natural-language PO request."""

    supplier_name: str = ""
    supplier_id: str = ""
    items: list[ParsedLineItem] = []
    notes: str = ""
    ambiguities: list[str] = []
    confidence: float = 1.0


class ParsedLineItem(BaseModel):
    product_name: str
    quantity: float | None = None
    unit: str | None = None
    unit_price: float | None = None


# Rebuild ParsedRequest so the forward ref to ParsedLineItem resolves
ParsedRequest.model_rebuild()
