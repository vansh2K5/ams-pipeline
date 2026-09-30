from pathlib import Path

from ams.parsers import detect_language, parse_service
from ams.parsers import java as java_parser
from ams.parsers import python as python_parser

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_detects_languages():
    assert detect_language(EXAMPLES / "order-service") == "java"
    assert detect_language(EXAMPLES / "billing-service") == "python"


def test_java_entities_enums_and_wire_names():
    svc = parse_service(EXAMPLES / "order-service")
    assert svc.port == 8080
    order = svc.entity("Order")
    assert order is not None
    types = {f.name: f.type for f in order.fields}
    assert types["totalAmount"] == "decimal"
    assert types["createdAt"] == "datetime"
    assert types["orderStatus"] == "ref:OrderStatus"
    assert types["items"] == "list<ref:OrderItem>"
    assert order.field("shippingCity").json_name == "shipping_city"  # @JsonProperty
    assert svc.entity("OrderStatus").values == ["PENDING", "PAID", "SHIPPED", "CANCELLED"]


def test_java_endpoints():
    svc = parse_service(EXAMPLES / "order-service")
    eps = {(e.method, e.path): e for e in svc.endpoints}
    assert eps[("GET", "/api/orders/{orderId}")].returns == "Order"
    assert eps[("GET", "/api/orders")].returns == "list<Order>"
    assert [p.location for p in eps[("GET", "/api/orders")].params] == ["query"]
    assert eps[("POST", "/api/orders")].params[0].location == "body"


def test_java_record_and_ignored_fields(tmp_path):
    (tmp_path / "Dto.java").write_text(
        "import com.fasterxml.jackson.annotation.*;\n"
        "public record Payment(@JsonProperty(\"payment_id\") String id, java.math.BigDecimal amount) {}\n"
        "class Account { private static final long serialVersionUID = 1L; @JsonIgnore private String secret;"
        " private Optional<String> nickname; }\n"
    )
    svc = java_parser.parse(tmp_path)
    payment = svc.entity("Payment")
    assert payment.kind == "record"
    assert payment.field("id").json_name == "payment_id"
    account = svc.entity("Account")
    assert [f.name for f in account.fields] == ["nickname"]
    assert account.fields[0].type == "string" and account.fields[0].optional


def test_python_models_and_routes():
    svc = parse_service(EXAMPLES / "billing-service")
    rec = svc.entity("OrderRecord")
    assert {f.name: f.type for f in rec.fields}["amount_due"] == "decimal"
    assert rec.field("city").optional
    (ep,) = svc.endpoints
    assert (ep.method, ep.path, ep.returns) == ("POST", "/invoices/{order_id}", "Invoice")


def test_python_alias_enum_and_nested(tmp_path):
    (tmp_path / "m.py").write_text(
        "from enum import Enum\nfrom pydantic import BaseModel, Field\n"
        "class Kind(str, Enum):\n    A = 'a'\n    B = 'b'\n"
        "class Line(BaseModel):\n    sku: str\n"
        "class Cart(BaseModel):\n    cart_id: int = Field(alias='cartId')\n    lines: list[Line]\n    kind: Kind\n"
    )
    svc = python_parser.parse(tmp_path)
    cart = svc.entity("Cart")
    assert cart.field("cart_id").json_name == "cartId"
    assert cart.field("lines").type == "list<ref:Line>"
    assert svc.entity("Kind").values == ["A", "B"]
