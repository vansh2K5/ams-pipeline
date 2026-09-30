from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class OrderRecord(BaseModel):
    """What billing needs to know about an order (owned by order-service)."""

    order_ref: int
    client_id: int
    amount_due: Decimal
    currency: str
    placed_at: datetime
    status: str
    city: str | None = None


class Invoice(BaseModel):
    invoice_id: str
    order_ref: int
    client_id: int
    amount_due: Decimal
    currency: str
    issued_at: datetime
