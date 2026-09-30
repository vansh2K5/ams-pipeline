import os
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI

from app.models import Invoice, OrderRecord

app = FastAPI(title="billing-service")
PORT = int(os.getenv("PORT", "8000"))


def fetch_order(order_id: int) -> OrderRecord:
    # the integration layer AMS generates (ams_generated/order_service_client.py) goes here
    raise NotImplementedError("no client for order-service yet")


@app.post("/invoices/{order_id}", response_model=Invoice)
def create_invoice(order_id: int) -> Invoice:
    order = fetch_order(order_id)
    return Invoice(
        invoice_id=str(uuid.uuid4()),
        order_ref=order.order_ref,
        client_id=order.client_id,
        amount_due=order.amount_due,
        currency=order.currency,
        issued_at=datetime.now(timezone.utc),
    )
