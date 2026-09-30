from pathlib import Path

from ams.mapping import build_plan, choose_pair, name_similarity, tokens
from ams.parsers import parse_service

EXAMPLES = Path(__file__).parent.parent / "examples"


class FakeLLM:
    name = "fake"

    def __init__(self, answer):
        self.answer = answer

    def complete_json(self, system, user):
        return self.answer

    def complete_text(self, system, user):
        return None


def services():
    return parse_service(EXAMPLES / "order-service"), parse_service(EXAMPLES / "billing-service")


def test_tokens_split_camel_and_snake():
    assert tokens("customerId") == ["customer", "id"]
    assert tokens("amount_due") == ["amount", "due"]


def test_synonyms_score_higher_than_unrelated():
    assert name_similarity("client_id", "customerId") > name_similarity("client_id", "currencyCode")


def test_heuristic_maps_every_field_of_the_example():
    prov, cons = services()
    p_ent, c_ent = choose_pair(prov, cons)
    assert (p_ent.name, c_ent.name) == ("Order", "OrderRecord")
    plan = build_plan(prov, cons, p_ent, c_ent, llm=None)
    got = {p.consumer_field: p.provider_field for p in plan.pairs}
    assert got == {
        "order_ref": "orderId", "client_id": "customerId", "amount_due": "totalAmount",
        "currency": "currencyCode", "placed_at": "createdAt", "status": "orderStatus", "city": "shippingCity",
    }
    assert plan.coverage == 1.0
    assert plan.unused_provider == ["items"]


def test_llm_answer_is_validated_and_gaps_filled():
    prov, cons = services()
    p_ent, c_ent = prov.entity("Order"), cons.entity("OrderRecord")
    llm = FakeLLM({"pairs": [
        {"consumer_field": "client_id", "provider_field": "customerId", "confidence": 0.97},
        {"consumer_field": "order_ref", "provider_field": "invented", "confidence": 0.9},   # hallucinated field
        {"consumer_field": "placed_at", "provider_field": "totalAmount", "confidence": 0.9},  # type clash
    ]})
    plan = build_plan(prov, cons, p_ent, c_ent, llm)
    by_field = {p.consumer_field: p for p in plan.pairs}
    assert by_field["client_id"].source == "llm"
    assert by_field["order_ref"].provider_field == "orderId" and by_field["order_ref"].source == "heuristic"
    assert by_field["placed_at"].provider_field == "createdAt"
    assert len(plan.notes) == 2


def test_unusable_llm_answer_falls_back():
    prov, cons = services()
    plan = build_plan(prov, cons, prov.entity("Order"), cons.entity("OrderRecord"), FakeLLM(None))
    assert plan.coverage == 1.0
    assert all(p.source == "heuristic" for p in plan.pairs)
